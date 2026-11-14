"""Environment and Climate Change Canada daily observations, summarised per winter season. Freeze-thaw
cycles and precipitation are the main innocent explanation for pothole complaints, so the indicator engine
and the AI get them as numbers rather than hand-waving."""
import csv
import io
from collections import defaultdict
from datetime import date

from .common import dataset_result, fetch_cached

BULK = "https://climate.weather.gc.ca/climate_data/bulk_data_e.html"


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def daily_rows(station_id, year):
    ttl = 86400 if year >= date.today().year else 365 * 86400
    raw = fetch_cached(f"climate-{station_id}-{year}.csv", BULK, ttl=ttl, params={
        "format": "csv", "stationID": station_id, "Year": year, "Month": 1, "Day": 1, "timeframe": 2, "submit": "Download Data"})
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def season_of(day):
    """Winter season label: Nov-Apr belong to the season named by its ending year (2024-11 -> '2024-25')."""
    year = day.year + 1 if day.month >= 11 else day.year
    return f"{year - 1}-{str(year)[2:]}"


def seasons(station_id, station_name, since_year, source_url):
    totals = defaultdict(lambda: {"freeze_thaw_days": 0, "days_below_minus10": 0, "precip_mm": 0.0, "snow_cm": 0.0,
                                  "rain_on_frozen_days": 0, "days": 0})
    today = date.today()
    for year in range(since_year, today.year + 1):
        try:
            rows = daily_rows(station_id, year)
        except Exception:
            continue
        for r in rows:
            try:
                day = date.fromisoformat(r.get("Date/Time", "")[:10])
            except ValueError:
                continue
            if day > today or day.month not in (11, 12, 1, 2, 3, 4):
                continue
            tmax, tmin = _float(r.get("Max Temp (°C)")), _float(r.get("Min Temp (°C)"))
            precip, snow, rain = _float(r.get("Total Precip (mm)")) or 0.0, _float(r.get("Total Snow (cm)")) or 0.0, _float(r.get("Total Rain (mm)")) or 0.0
            t = totals[season_of(day)]
            t["days"] += 1
            if tmax is not None and tmin is not None:
                t["freeze_thaw_days"] += tmax > 0 > tmin
                t["days_below_minus10"] += tmin < -10
                t["rain_on_frozen_days"] += rain > 2 and tmin < 0
            t["precip_mm"] += precip
            t["snow_cm"] += snow
    rows = [{"season": s, "year": int(s[:4]) + 1, **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in t.items()}}
            for s, t in sorted(totals.items())]
    if rows and all(r["snow_cm"] == 0 for r in rows):
        for r in rows:
            r["snow_cm"] = None  # this station does not report snowfall
    complete = [r for r in rows if r["days"] >= 150]
    baseline = sum(r["freeze_thaw_days"] for r in complete) / len(complete) if complete else None
    for r in rows:
        r["freeze_thaw_vs_average_pct"] = round((r["freeze_thaw_days"] / baseline - 1) * 100) if baseline and r["days"] >= 150 else None
    return dataset_result(
        key="climate", title=f"Winter severity - {station_name} (Environment Canada)", source_url=source_url, since=since_year,
        records=rows,
        description="Per winter season (November to April): days with a freeze-thaw cycle (max above 0 °C and min below 0 °C), "
                    "very cold days, rain on frozen ground, total precipitation and snowfall, from the nearest Environment "
                    "and Climate Change Canada station. Freeze-thaw cycles are the dominant natural driver of potholes; "
                    "compare them with complaint counts before attributing damage to construction quality.",
        fields=[("Season", "season"), ("Freeze-thaw days", "freeze_thaw_days"), ("Freeze-thaw vs average percent", "freeze_thaw_vs_average_pct"),
                ("Days below -10 C", "days_below_minus10"), ("Rain on frozen days", "rain_on_frozen_days"),
                ("Precipitation mm", "precip_mm"), ("Snow cm", "snow_cm"), ("Days observed", "days")])
