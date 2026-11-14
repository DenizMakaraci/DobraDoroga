"""City of Toronto open data for one street.

Datasets (all Open Government Licence - Toronto):
  awarded          Toronto Bids - Awarded Contracts (CKAN datastore, daily)
  non_competitive  Toronto Bids - Non-Competitive Contracts (monthly) - no location; matched by supplier
  road_program     Road Reconstruction / Resurfacing Program map layer (T.O.INview)
  restrictions     Road Restrictions live feed (permits, closures, contractor on site)
  requests_311     311 Service Requests (yearly ZIP/CSV) - road-related request types only

CKAN's SQL endpoint is disabled and its full-text search is fuzzy, so datasets are downloaded
whole (0.3-6 MB each), cached for a day and filtered locally with exact street matching.
"""
import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import date

from . import climate
from .common import canonical_company, dataset_result, fetch_cached, load_json, matching_street, post_json_cached, street_key

NAME = "Toronto"
KEY = "toronto"
LANGUAGE = "en"
CENTRE = (43.6532, -79.3832)
BBOX = (-79.64, 43.58, -79.11, 43.86)  # west, south, east, north
PORTAL = "https://open.toronto.ca/"
CLIMATE_STATION = (31688, "Toronto City")
CKAN = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
RESOURCES = {
    "building_permits": "6d0229af-bc54-46de-9c2b-26759b01dd05",
    "development_applications": "8907d8ed-c515-4ce9-b674-9f8c6eefcf0d",
    "traffic": "6afa3b1f-f6a5-4235-8bd6-7568411c19f4",
    "capital_pipeline": "e33e036f-a898-4b90-b626-84abba557bbd",
    "solicitations": "f676f46c-de76-4e94-bba7-0a3951aa0603",
    "resurfacing_program": "86410703-6caa-4bed-b8ae-e42622e0fc62",
    "awarded": "e211f003-5909-4bea-bd96-d75899d8e612",
    "non_competitive": "a11b18b4-72e6-47e3-a4e9-05dcc8abd697",
    "road_program": "40d94b5a-cbf2-4e04-8d48-24038d163d0c",
}
PAGES = {
    "awarded": "https://open.toronto.ca/dataset/tobids-awarded-contracts/",
    "non_competitive": "https://open.toronto.ca/dataset/tobids-non-competitive-contracts/",
    "road_program": "https://open.toronto.ca/dataset/road-reconstruction-program/",
    "restrictions": "https://open.toronto.ca/dataset/road-restrictions/",
    "requests_311": "https://open.toronto.ca/dataset/311-service-requests-customer-initiated/",
    "lobbying": "https://open.toronto.ca/dataset/lobbyist-registry/",
    "planning": "https://open.toronto.ca/dataset/capital-project-pipeline/",
    "solicitations": "https://open.toronto.ca/dataset/tobids-all-open-solicitations/",
    "development": "https://open.toronto.ca/dataset/building-permits-active-permits/",
    "traffic": "https://open.toronto.ca/dataset/traffic-volumes-at-intersections-for-all-modes/",
    "integrity": "https://www.torontoauditor.ca/reports/",
    "climate": "https://climate.weather.gc.ca/historical_data/search_historic_data_e.html",
}
AUDITOR_API = "https://www.torontoauditor.ca/wp-json/wp/v2/report"
MAJOR_PERMITS = ("new building", "demolition")
SIDEWALK_PACKAGE = "sidewalk-construction-program"
RESTRICTIONS_URL = "https://secure.toronto.ca/opendata/cart/road_restrictions/v3"
ROAD_WORK_WORDS = ("road", "resurfac", "reconstruct", "pavement", "asphalt", "sidewalk", "bikeway",
                   "cycle track", "watermain", "sewer", "streetscape", "curb", "bridge")
LOBBY_ROAD_WORDS = ("resurfac", "reconstruct", "pavement", "asphalt", "sidewalk", "bikeway", "watermain", "sewer",
                    "streetscape", "curb", "bridge", "roadway", "road work", "paving", "tender", "rfq", "rft")
ROAD_311_TYPES = ("pot hole", "pothole", "road - damaged", "road damage", "road - sinking", "utility cut",
                  "sidewalk - damaged", "catch basin", "road - cleaning", "complaint - road operations",
                  "curb - damaged", "pavement")


def _csv(name, resource):
    raw = fetch_cached(f"toronto-{name}.csv", f"{CKAN}/datastore/dump/{resource}", params={"format": "csv"})
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def _amount(value):
    try:
        return float(str(value).replace(",", "").replace("$", "") or 0)
    except ValueError:
        return 0.0


canonical_supplier = canonical_company


def _year(value):
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def _label(streets):
    return ", ".join(streets)


def awarded_contracts(streets, since_year):
    rows, city_road = [], Counter()
    for r in _csv("awarded", RESOURCES["awarded"]):
        text = r.get("Solicitation Document Description", "")
        year = _year(r.get("Award Authority Obtained Date"))
        if (year or since_year) >= since_year and any(w in text.lower() for w in ROAD_WORK_WORDS):
            city_road[canonical_supplier(r.get("Successful Supplier"))] += 1
        street = matching_street(text, streets)
        if street and (year is None or year >= since_year):
            rows.append({
                "street": street,
                "document_number": r.get("Document Number"), "type": r.get("RFx (Solicitation) Type"),
                "supplier": canonical_supplier(r.get("Successful Supplier")), "amount": _amount(r.get("Award")),
                "award_date": r.get("Award Authority Obtained Date"), "division": r.get("Division"),
                "description": " ".join(text.split())[:1200], "wards": r.get("Wards"),
                "road_work": any(w in text.lower() for w in ROAD_WORK_WORDS),
            })
    rows.sort(key=lambda r: r["award_date"] or "", reverse=True)
    total_city = sum(city_road.values()) or 1
    result = dataset_result(
        key="awarded", title=f"Toronto Bids - awarded contracts mentioning {_label(streets)}",
        source_url=PAGES["awarded"], since=since_year, records=rows,
        description="Competitive contracts awarded by the City of Toronto whose solicitation description names "
                    "one of the case's streets. Amounts are award values, not payments.",
        fields=[("Street", "street"), ("Document number", "document_number"), ("Solicitation type", "type"),
                ("Successful supplier", "supplier"), ("Award amount", "amount"), ("Award date", "award_date"),
                ("Division", "division"), ("Wards", "wards"), ("Description", "description")])
    result["baselines"] = {"city_road_awards": total_city, "city_supplier_shares": {
        r["supplier"]: {"awards": city_road[r["supplier"]], "share_pct": round(city_road[r["supplier"]] / total_city * 100, 1)}
        for r in rows if r.get("road_work") and r["supplier"]}}
    return result


def non_competitive(suppliers, since_year):
    wanted = {canonical_supplier(s) for s in suppliers if s}
    rows = []
    for r in _csv("non_competitive", RESOURCES["non_competitive"]):
        supplier = canonical_supplier(r.get("Supplier Name"))
        year = _year(r.get("Contract Date"))
        if supplier in wanted and (year is None or year >= since_year):
            rows.append({"workspace": r.get("Workspace Number"), "reason": r.get("Reason"),
                         "contract_date": r.get("Contract Date"), "supplier": supplier,
                         "amount": _amount(r.get("Contract Amount")), "division": r.get("Division")})
    rows.sort(key=lambda r: r["contract_date"] or "", reverse=True)
    return dataset_result(
        key="non_competitive", title="Toronto Bids - non-competitive contracts to the same suppliers",
        source_url=PAGES["non_competitive"], since=since_year, records=rows,
        description="Contracts awarded without competition to suppliers that also won competitive awards on "
                    "the case's streets. The dataset has no location field; this is a supplier match, not a road match.",
        fields=[("Workspace number", "workspace"), ("Reason", "reason"), ("Contract date", "contract_date"),
                ("Supplier", "supplier"), ("Contract amount", "amount"), ("Division", "division")])


def road_program(streets):
    rows = []
    for r in _csv("road_program", RESOURCES["road_program"]):
        location = re.sub(r"(?i)(?:&lt;|<)br\s*/?(?:&gt;|>)", " ", r.get("LOCATION", "") or "")
        street = matching_street(location, streets)
        if street:
            rows.append({"street": street, "project": r.get("PROJECT"), "location": " ".join(location.split()),
                         "details": r.get("DETAILS"), "timeline": r.get("DURATION/ Construction Timeline"),
                         "status": r.get("STATUS"), "start_year": _year(r.get("START_YEAR")),
                         "owner": r.get("OWNER"), "contract_number": r.get("CONTR_NUMBER"),
                         "updated": r.get("LAST_UPDATED_DATE")})
    rows.sort(key=lambda r: r["start_year"] or 0, reverse=True)
    return dataset_result(
        key="road_program", title=f"Toronto road reconstruction and resurfacing program - {_label(streets)}",
        source_url=PAGES["road_program"], records=rows,
        description="Planned, active and recently completed capital road projects on the case's streets from the "
                    "City's T.O.INview construction map layer.",
        fields=[("Street", "street"), ("Project", "project"), ("Location", "location"), ("Details", "details"),
                ("Timeline", "timeline"), ("Status", "status"), ("Start year", "start_year"),
                ("Owner", "owner"), ("Contract number", "contract_number"), ("Last updated", "updated")])


def _epoch_date(ms):
    try:
        return date.fromtimestamp(int(ms) / 1000).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def road_restrictions(streets):
    data = load_json(fetch_cached("toronto-restrictions.json", RESTRICTIONS_URL, params={"format": "json"},
                                  ttl=6 * 3600))
    closures = data.get("Closure", data) if isinstance(data, dict) else data
    rows = []
    for c in closures:
        street = matching_street(c.get("road", ""), streets) or matching_street(c.get("name", ""), streets)
        if street:
            rows.append({"street": street, "id": c.get("id"), "road": c.get("road"), "extent": c.get("name"),
                         "type": c.get("type"), "work_type": c.get("workEventType"),
                         "permit_type": c.get("permitType"), "contractor": c.get("contractor"),
                         "source": c.get("source"), "start": _epoch_date(c.get("startTime")),
                         "end": _epoch_date(c.get("endTime")), "impact": c.get("maxImpact"),
                         "description": " ".join(str(c.get("description") or "").split())[:600],
                         "latitude": c.get("latitude"), "longitude": c.get("longitude")})
    rows.sort(key=lambda r: r["start"] or "", reverse=True)
    return dataset_result(
        key="restrictions", title=f"Toronto road restrictions and permits - {_label(streets)}",
        source_url=PAGES["restrictions"], records=rows,
        description="Current and scheduled closures and work permits on the case's streets from the City's live "
                    "RoDARS feed, including the contractor named on the permit. Past permits drop off the feed.",
        fields=[("Street", "street"), ("Permit id", "id"), ("Road", "road"), ("Extent", "extent"), ("Type", "type"),
                ("Work type", "work_type"), ("Permit type", "permit_type"), ("Contractor", "contractor"),
                ("Source", "source"), ("Start", "start"), ("End", "end"), ("Impact", "impact"),
                ("Description", "description")])


def _311_resources():
    package = load_json(fetch_cached("toronto-311-package.json",
                                     f"{CKAN}/api/3/action/package_show", params={"id": "311-service-requests-customer-initiated"}))
    out = {}
    for r in package["result"]["resources"]:
        year = _year(r.get("name"))
        if r.get("format") == "ZIP" and year:
            out[year] = r["url"]
    return out


def requests_311(streets, since_year):
    """Road-related 311 requests at intersections on the case's streets, aggregated by year and type."""
    rows, counts, city_counts = [], Counter(), Counter()
    for year, url in sorted(_311_resources().items()):
        if year < since_year:
            continue
        archive = zipfile.ZipFile(io.BytesIO(fetch_cached(f"toronto-311-{year}.zip", url, ttl=7 * 86400)))
        for member in archive.namelist():
            if not member.lower().endswith(".csv"):
                continue
            with archive.open(member) as handle:
                for r in csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace")):
                    kind = (r.get("Service Request Type") or "")
                    if not any(k in kind.lower() for k in ROAD_311_TYPES):
                        continue
                    if any(k in kind.lower() for k in ("pot hole", "pothole", "damaged", "sinking", "utility cut")):
                        city_counts[str(year)] += 1
                    s1, s2 = r.get("Intersection Street 1") or "", r.get("Intersection Street 2") or ""
                    street = matching_street(s1, streets) or matching_street(s2, streets)
                    if street:
                        created = (r.get("Creation Date") or "")[:10]
                        counts[(created[:4], kind)] += 1
                        rows.append({"street": street, "created": created, "type": kind, "status": r.get("Status"),
                                     "intersection": f"{s1} & {s2}".strip(" &"), "ward": r.get("Ward"),
                                     "division": r.get("Division")})
    rows.sort(key=lambda r: r["created"], reverse=True)
    summary = [{"year": y, "type": t, "count": n} for (y, t), n in sorted(counts.items())]
    result = dataset_result(
        key="requests_311", title=f"Toronto 311 road-related service requests - {_label(streets)}",
        source_url=PAGES["requests_311"], since=since_year, records=rows[:400],
        description="Pothole, road damage, sinking, utility-cut and similar 311 requests logged at "
                    "intersections on the case's streets. Counts by year and type are listed first; individual "
                    "requests follow (up to 400 most recent).",
        fields=[("Street", "street"), ("Created", "created"), ("Type", "type"), ("Status", "status"),
                ("Intersection", "intersection"), ("Ward", "ward"), ("Division", "division")])
    lines = [f"{s['year']} | {s['type']}: {s['count']}" for s in summary]
    result["pages"][0]["text"] = result["pages"][0]["text"].replace(
        "\n\nRecord 1", "\n\nCounts by year and type\n" + "\n".join(lines) + "\n\nRecord 1", 1) if lines else result["pages"][0]["text"]
    result["summary"] = summary
    result["total"] = len(rows)
    result["baselines"] = {"city_road_damage_by_year": dict(sorted(city_counts.items()))}
    return result


CONDITION_NOTE = ("Toronto publishes no pavement-condition index. This layer shows the City's own road "
                  "resurfacing and reconstruction programs: segments the City has scheduled for work, coloured by "
                  "the kind of work and how soon. It is a proxy for condition, not a measurement.")


def condition_layer():
    """GeoJSON features for the road-condition map layer (proxy: scheduled road work)."""
    features = []
    for name, kind in (("road_program", "reconstruction"), ("resurfacing_program", "resurfacing")):
        for r in _csv(name, RESOURCES[name]):
            try:
                geometry = json.loads(r.get("geometry") or "null")
            except ValueError:
                geometry = None
            if not geometry:
                continue
            year = _year(r.get("START_YEAR")) or _year(r.get("DURATION/ Construction Timeline"))
            status = (r.get("STATUS") or "").strip()
            if status == "Completed":
                grade, label = 1, "Recently rebuilt or resurfaced"
            elif kind == "reconstruction":
                grade, label = 5, "Reconstruction scheduled"
            else:
                grade, label = 4 if (year or 9999) <= 2026 else 3, "Resurfacing scheduled"
            location = " ".join(re.sub(r"(?i)(?:&lt;|<)br\s*/?(?:&gt;|>)", " ", r.get("LOCATION", "") or "").split())
            features.append({"type": "Feature", "geometry": geometry, "properties": {
                "grade": grade, "label": label, "detail": f"{r.get('PROJECT')} · {location} · {r.get('DURATION/ Construction Timeline') or year or ''}".strip(" ·"),
                "year": year, "status": status, "source": "City of Toronto T.O.INview road programs"}})
    return {"type": "FeatureCollection", "features": features,
            "legend": {"title": "Scheduled road work (condition proxy)", "note": CONDITION_NOTE,
                       "source_url": PAGES["road_program"],
                       "classes": [(1, "Recently completed"), (3, "Resurfacing planned later"),
                                   (4, "Resurfacing planned by 2026"), (5, "Reconstruction planned")]}}


def _datastore(name, resource, params, ttl=24 * 3600):
    """datastore_search (POST) with exact `filters` or full-text `q`, paged to 20k rows."""
    rows, offset = [], 0
    while True:
        page = post_json_cached(f"toronto-{name}-{offset}", f"{CKAN}/api/3/action/datastore_search",
                                {"resource_id": resource, "limit": 5000, "offset": offset, **params}, ttl=ttl)["result"]
        rows.extend(page["records"])
        offset += 5000
        if len(page["records"]) < 5000 or offset >= 20000:
            return rows


def _street_matches(record, street, name_key="STREET_NAME", type_key="STREET_TYPE"):
    base, kind = street_key(street)
    rec_base, rec_kind = street_key(f"{record.get(name_key) or ''} {record.get(type_key) or ''}")
    return rec_base == base and (kind is None or rec_kind is None or rec_kind == kind)


def development(streets, since_year):
    """Building permits (major work only) and development applications addressed on the case's streets:
    adjacent construction is a common innocent explanation for pavement damage and extra permits."""
    rows = []
    for street in streets:
        base, _ = street_key(street)
        if not base:
            continue
        for r in _datastore(f"permits-{base}", RESOURCES["building_permits"], {"filters": json.dumps({"STREET_NAME": base.upper()})}, ttl=7 * 86400):
            if not _street_matches(r, street):
                continue
            issued, kind = str(r.get("ISSUED_DATE") or r.get("APPLICATION_DATE") or "")[:10], (r.get("PERMIT_TYPE") or "")
            cost = _amount(r.get("EST_CONST_COST")) if str(r.get("EST_CONST_COST") or "").replace(",", "").replace(".", "").isdigit() else 0.0
            if (_year(issued) or since_year) < since_year or not (any(w in kind.lower() for w in MAJOR_PERMITS) or cost >= 500_000):
                continue
            rows.append({"street": street, "kind": "building_permit", "date": issued, "reference": r.get("PERMIT_NUM"),
                         "address": f"{r.get('STREET_NUM')} {r.get('STREET_NAME')} {r.get('STREET_TYPE')}".strip(),
                         "type": kind, "work": r.get("WORK"), "status": r.get("STATUS"), "estimated_cost": cost or None,
                         "description": " ".join(str(r.get("DESCRIPTION") or "").split())[:300], "builder": r.get("BUILDER_NAME")})
        for r in _datastore(f"devapps-{base}", RESOURCES["development_applications"], {"filters": json.dumps({"STREET_NAME": base.upper()})}, ttl=7 * 86400):
            if not _street_matches(r, street):
                continue
            submitted = str(r.get("DATE_SUBMITTED") or "")[:10]
            if (_year(submitted) or since_year) < since_year:
                continue
            rows.append({"street": street, "kind": "development_application", "date": submitted, "reference": r.get("APPLICATION#"),
                         "address": f"{r.get('STREET_NUM')} {r.get('STREET_NAME')} {r.get('STREET_TYPE')}".strip(),
                         "type": r.get("APPLICATION_TYPE"), "status": r.get("STATUS"),
                         "description": " ".join(str(r.get("DESCRIPTION") or "").split())[:300], "url": r.get("APPLICATION_URL")})
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return dataset_result(
        key="development", title=f"Toronto - major building permits and development applications - {_label(streets)}",
        source_url=PAGES["development"], since=since_year, records=rows,
        description="New buildings, demolitions, additions and permits over $500k, plus planning applications, addressed on the "
                    "case's streets. Heavy construction next to a road is a frequent cause of damage unrelated to the paving contract.",
        fields=[("Street", "street"), ("Kind", "kind"), ("Date", "date"), ("Reference", "reference"), ("Address", "address"),
                ("Type", "type"), ("Work", "work"), ("Status", "status"), ("Estimated cost", "estimated_cost"),
                ("Description", "description"), ("Builder", "builder"), ("Link", "url")])


def traffic(streets):
    """Latest intersection counts (vehicles, heavy-vehicle share, pedestrians) at intersections on the case's streets."""
    rows = []
    for street in streets:
        base, _ = street_key(street)
        if not base:
            continue
        for r in _datastore(f"traffic-{base}", RESOURCES["traffic"], {"q": json.dumps({"location_name": base})}, ttl=7 * 86400):
            if not matching_street(r.get("location_name") or "", [street]):
                continue
            rows.append({"street": street, "location": r.get("location_name"), "date": str(r.get("latest_count_date") or "")[:10],
                         "hours": r.get("count_duration"), "vehicles": _amount(r.get("total_vehicle")),
                         "heavy_vehicle_pct": round(_amount(r.get("total_heavy_pct")) * 100, 1), "pedestrians": _amount(r.get("total_pedestrian")),
                         "bikes": _amount(r.get("total_bike")), "pm_peak_vehicles": _amount(r.get("pm_peak_vehicle")),
                         "latitude": r.get("latitude"), "longitude": r.get("longitude")})
    rows.sort(key=lambda r: r["vehicles"], reverse=True)
    return dataset_result(
        key="traffic", title=f"Toronto - traffic volumes at intersections on {_label(streets)}",
        source_url=PAGES["traffic"], records=rows,
        description="Most recent turning-movement count per intersection: total vehicles over the count period, share of heavy "
                    "vehicles, pedestrians and bikes. Heavy-vehicle share is a key driver of pavement wear.",
        fields=[("Street", "street"), ("Location", "location"), ("Count date", "date"), ("Hours", "hours"), ("Vehicles", "vehicles"),
                ("Heavy vehicle percent", "heavy_vehicle_pct"), ("Pedestrians", "pedestrians"), ("Bikes", "bikes"), ("PM peak vehicles", "pm_peak_vehicles")])


def integrity(suppliers, streets):
    """Toronto Auditor General reports whose title or excerpt names a supplier, a street or road-contract topics."""
    import html as html_lib
    rows, seen = [], set()
    terms = [s for s in suppliers if s and len(s) > 4][:8] + [street_key(s)[0] for s in streets] + ["road", "paving", "construction contract"]
    for term in terms:
        try:
            found = load_json(fetch_cached(f"toronto-ag-{re.sub(r'[^a-z0-9]+', '-', term.lower())[:40]}.json", AUDITOR_API,
                                           params={"search": term, "per_page": 20}, ttl=7 * 86400))
        except Exception:
            continue
        for post in found if isinstance(found, list) else []:
            title = html_lib.unescape(re.sub("<[^>]+>", "", (post.get("title") or {}).get("rendered", ""))).strip()
            excerpt = html_lib.unescape(re.sub("<[^>]+>", " ", (post.get("excerpt") or {}).get("rendered", ""))).strip()
            if post.get("link") in seen:
                continue
            text = f"{title} {excerpt}"
            supplier = next((s for s in suppliers if s and len(s) > 4 and canonical_supplier(s) in canonical_supplier(text)), None)
            street = matching_street(text, streets)
            if not (supplier or street or term in ("road", "paving", "construction contract")):
                continue
            seen.add(post.get("link"))
            rows.append({"street": street, "supplier": supplier, "date": str(post.get("date") or "")[:10], "title": title[:300],
                         "excerpt": " ".join(excerpt.split())[:400], "url": post.get("link"),
                         "relevance": "names supplier" if supplier else ("names street" if street else "road or contract topic")})
    rows.sort(key=lambda r: (r["relevance"] != "names supplier", r["relevance"] != "names street", r["date"]), reverse=False)
    return dataset_result(
        key="integrity", title=f"Toronto Auditor General - reports relevant to {_label(streets)} and its suppliers",
        source_url=PAGES["integrity"], records=rows[:60],
        description="Auditor General reports that name one of the case's suppliers or streets, plus audits of road and construction "
                    "contracting generally. Only titles and excerpts are indexed; open the link for the report.",
        fields=[("Street", "street"), ("Supplier", "supplier"), ("Relevance", "relevance"), ("Date", "date"), ("Title", "title"),
                ("Excerpt", "excerpt"), ("Link", "url")])


def _sidewalk_rows():
    package = load_json(fetch_cached("toronto-sidewalk-package.json", f"{CKAN}/api/3/action/package_show",
                                     params={"id": SIDEWALK_PACKAGE}))
    url = next(r["url"] for r in package["result"]["resources"] if r["format"] == "CSV")
    raw = fetch_cached("toronto-sidewalk.csv", url, ttl=7 * 86400)
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def planning(streets, since_year):
    """Capital project pipeline (upcoming construction contracts with estimated ranges) and the sidewalk program."""
    rows = []
    for r in _csv("capital_pipeline", RESOURCES["capital_pipeline"]):
        text = f"{r.get('Name and Construction Contract Number') or ''} {r.get('Scope of Work: Detailed Description') or ''}"
        street = matching_street(text, streets)
        if street:
            rows.append({"street": street, "kind": "pipeline", "date": str(r.get("Target Award Year") or ""),
                         "description": " ".join(text.split())[:500], "category": r.get("Type of Work"),
                         "division": r.get("Delivery Division"), "sourcing": r.get("Sourcing Type"),
                         "estimate_range": r.get("Estimated Range"), "term_months": r.get("Estimated Contract Term (Months)")})
    try:
        for r in _sidewalk_rows():
            location = " ".join(re.sub(r"(?i)(?:&lt;|<)br\s*/?(?:&gt;|>)", " ", r.get("LOCATION", "") or "").split())
            street = matching_street(location, streets)
            year = _year(r.get("START_YEAR"))
            if street and (year is None or year >= since_year):
                rows.append({"street": street, "kind": "sidewalk_program", "date": str(year or ""), "description": f"{r.get('PROJECT')} · {location}",
                             "category": r.get("DETAILS"), "division": r.get("OWNER"), "status": r.get("STATUS"),
                             "timeline": r.get("DURATION/ Construction Timeline")})
    except Exception:
        pass
    rows.sort(key=lambda r: r["date"], reverse=True)
    return dataset_result(
        key="planning", title=f"Toronto - capital pipeline and sidewalk program - {_label(streets)}",
        source_url=PAGES["planning"], since=since_year, records=rows,
        description="Upcoming construction contracts the City intends to tender (with estimated value ranges) and the sidewalk "
                    "construction program, where they name the case's streets. Plans and estimates are intentions, not awards.",
        fields=[("Street", "street"), ("Kind", "kind"), ("Year", "date"), ("Description", "description"), ("Category", "category"),
                ("Division", "division"), ("Sourcing", "sourcing"), ("Estimated range", "estimate_range"),
                ("Term months", "term_months"), ("Status", "status"), ("Timeline", "timeline")])


def solicitations(streets):
    """Tenders currently open that name the case's streets - work about to be awarded."""
    rows = []
    for r in _csv("solicitations", RESOURCES["solicitations"]):
        text = r.get("Solicitation Document Description") or ""
        street = matching_street(text, streets)
        if street:
            rows.append({"street": street, "document_number": r.get("Document Number"), "type": r.get("RFx (Solicitation) Type"),
                         "issued": r.get("Issue Date"), "deadline": r.get("Submission Deadline"), "division": r.get("Division"),
                         "category": r.get("High Level Category"), "description": " ".join(text.split())[:600]})
    rows.sort(key=lambda r: r["issued"] or "", reverse=True)
    return dataset_result(
        key="solicitations", title=f"Toronto Bids - open solicitations naming {_label(streets)}",
        source_url=PAGES["solicitations"], records=rows,
        description="Tenders and RFPs currently open on the Toronto Bids portal whose description names the case's streets.",
        fields=[("Street", "street"), ("Document number", "document_number"), ("Type", "type"), ("Issued", "issued"),
                ("Deadline", "deadline"), ("Division", "division"), ("Category", "category"), ("Description", "description")])


def _lobby_zip():
    package = load_json(fetch_cached("toronto-lobby-package.json", f"{CKAN}/api/3/action/package_show",
                                     params={"id": "lobbyist-registry"}))
    url = next(r["url"] for r in package["result"]["resources"] if r["format"] == "ZIP")
    return zipfile.ZipFile(io.BytesIO(fetch_cached("toronto-lobbyist-registry.zip", url, ttl=7 * 86400)))


def _text(element, path):
    found = element.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def lobbying(suppliers, streets, since_year):
    """Lobbyist Registry subject matters whose client firm is one of the suppliers, or whose particulars
    name one of the streets. Includes who was lobbied and when."""
    wanted = {canonical_supplier(s) for s in suppliers if s}
    rows = []
    archive = _lobby_zip()
    for member in archive.namelist():
        if not member.lower().endswith(".xml"):
            continue
        root = ET.fromstring(archive.read(member).decode("utf-8-sig"))
        for sm in root.iter("SM"):
            firms = [_text(f, "Name") for f in sm.findall("Firms/Firm")]
            particulars = " ".join(_text(sm, "Particulars").split())
            supplier = next((f for f in firms if canonical_supplier(f) in wanted), None)
            street = matching_street(particulars, streets)
            subject = _text(sm, "SubjectMatter")
            relevant = supplier or (street and (any(w in particulars.lower() for w in LOBBY_ROAD_WORDS)
                                               or any(w in subject.lower() for w in ("procurement", "infrastructure", "transportation", "construction"))))
            if not relevant:
                continue
            comms = sm.findall("Communications/Communication")
            dates = sorted(d for d in (_text(c, "CommunicationDate") for c in comms) if d)
            year = _year(dates[-1] if dates else _text(sm, "EffectiveDate") or _text(sm, "InitialApprovalDate"))
            if year is not None and year < since_year:
                continue
            officials = Counter(f"{_text(c, 'POH_Name')} ({_text(c, 'POH_Office')})".strip(" ()")
                                for c in comms if _text(c, "POH_Name"))
            rows.append({
                "street": street, "supplier": canonical_supplier(supplier) if supplier else None,
                "sm_number": _text(sm, "SMNumber"), "status": _text(sm, "Status"), "type": _text(sm, "Type"),
                "client": "; ".join(firms)[:300], "subject": subject,
                "particulars": particulars[:600], "registrant": f"{_text(sm, 'Registrant/FirstName')} {_text(sm, 'Registrant/LastName')}".strip(),
                "first_communication": dates[0] if dates else None, "last_communication": dates[-1] if dates else None,
                "communications": len(comms),
                "officials_lobbied": "; ".join(f"{name} x{n}" for name, n in officials.most_common(6)),
            })
    rows.sort(key=lambda r: (r["supplier"] is None, -(int((r["last_communication"] or "0000")[:4]))))
    return dataset_result(
        key="lobbying", title=f"Toronto Lobbyist Registry - {_label(streets)} and its suppliers",
        source_url=PAGES["lobbying"], since=since_year, records=rows,
        description="Registered lobbying where the client is a supplier awarded work on the case's streets, or the "
                    "subject matter names a street. Lists the City officials lobbied and communication dates. "
                    "Lobbying is legal and registered; timing relative to awards is what a reviewer checks.",
        fields=[("Street", "street"), ("Supplier", "supplier"), ("Subject matter number", "sm_number"),
                ("Status", "status"), ("Type", "type"), ("Client", "client"), ("Subject", "subject"),
                ("Registrant", "registrant"), ("First communication", "first_communication"),
                ("Last communication", "last_communication"), ("Communications", "communications"),
                ("Officials lobbied", "officials_lobbied"), ("Particulars", "particulars")])


def profile(streets, since_year):
    """Run every connector for one or more streets; a failing dataset is reported, not fatal."""
    streets = [streets] if isinstance(streets, str) else list(dict.fromkeys(streets))
    results, errors = [], []
    steps = [("awarded", lambda: awarded_contracts(streets, since_year)),
             ("road_program", lambda: road_program(streets)),
             ("restrictions", lambda: road_restrictions(streets)),
             ("requests_311", lambda: requests_311(streets, since_year))]
    for key, step in steps:
        try:
            results.append(step())
        except Exception as error:  # network/format problems should not lose the other datasets
            errors.append(f"{key}: {type(error).__name__}")
    suppliers = {r["supplier"] for d in results if d["key"] == "awarded" for r in d["records"]}
    if suppliers:
        try:
            results.append(non_competitive(suppliers, since_year))
        except Exception as error:
            errors.append(f"non_competitive: {type(error).__name__}")
    for key, step in [("lobbying", lambda: lobbying(suppliers, streets, since_year)),
                      ("planning", lambda: planning(streets, since_year)),
                      ("solicitations", lambda: solicitations(streets)),
                      ("development", lambda: development(streets, since_year)),
                      ("traffic", lambda: traffic(streets)),
                      ("integrity", lambda: integrity(suppliers, streets)),
                      ("climate", lambda: climate.seasons(*CLIMATE_STATION, since_year, PAGES["climate"]))]:
        try:
            results.append(step())
        except Exception as error:
            errors.append(f"{key}: {type(error).__name__}")
    return results, errors


def supplier_awards(names, since_year):
    """Every competitive City award won by the supplier or one of its other names (a supplier case, not a street case)."""
    from .common import company_matcher
    matches = company_matcher(names)
    rows = []
    for r in _csv("awarded", RESOURCES["awarded"]):
        year = _year(r.get("Award Authority Obtained Date"))
        if not matches(r.get("Successful Supplier")) or (year is not None and year < since_year):
            continue
        text = " ".join(r.get("Solicitation Document Description", "").split())
        rows.append({
            "city": NAME, "vendor": canonical_supplier(r.get("Successful Supplier")), "vendor_name": r.get("Successful Supplier"),
            "document_number": r.get("Document Number"), "type": r.get("RFx (Solicitation) Type"), "amount": _amount(r.get("Award")),
            "award_date": r.get("Award Authority Obtained Date"), "year": year, "division": r.get("Division"),
            "description": text[:600], "road_work": any(w in text.lower() for w in ROAD_WORK_WORDS),
        })
    rows.sort(key=lambda r: r["award_date"] or "", reverse=True)
    result = dataset_result(
        key="city_awards", title=f"Toronto Bids - contracts awarded to {names[0] if names else 'the supplier'}",
        source_url=PAGES["awarded"], since=since_year, records=rows,
        description="Competitive contracts the City of Toronto awarded to this supplier or a name variant. Amounts are award "
                    "values, not payments. Non-competitive contracts are a separate dataset.",
        fields=[("City", "city"), ("Vendor name", "vendor_name"), ("Document number", "document_number"), ("Solicitation type", "type"),
                ("Award amount", "amount"), ("Award date", "award_date"), ("Division", "division"), ("Description", "description")])
    result["total"] = len(rows)
    return result
