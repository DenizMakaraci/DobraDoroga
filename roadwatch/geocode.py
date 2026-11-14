"""Street lookup through OpenStreetMap Nominatim, proxied server-side so the browser never talks to a
third party and the usage policy (identifying User-Agent, about 1 request/second) is honoured."""
import asyncio
import time

import httpx
from fastapi import HTTPException

from .opendata import CITIES
from .opendata.common import USER_AGENT, fold

NOMINATIM = "https://nominatim.openstreetmap.org"
HIGHWAY_TYPES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
                 "living_street", "service", "road", "pedestrian", "track", "cycleway", "busway"}
_lock = asyncio.Lock()
_last_call = 0.0


def city_key(address, display_name=""):
    haystack = fold(" ".join(address.get(f) or "" for f in ("city", "town", "municipality", "county", "state_district")) + " " + display_name)
    for key, module in CITIES.items():
        if fold(module.NAME) in haystack:
            return key
    return None


def cities():
    return [{"key": m.KEY, "name": m.NAME, "language": m.LANGUAGE, "centre": m.CENTRE, "bbox": m.BBOX, "portal": m.PORTAL}
            for m in CITIES.values()]


def _place(item):
    address = item.get("address", {})
    road = address.get("road") or (item.get("name") if item.get("category") == "highway" else None)
    return {
        "osm_type": item.get("osm_type"), "osm_id": item.get("osm_id"),
        "display_name": item.get("display_name", ""), "road": road,
        "city": address.get("city") or address.get("town") or address.get("municipality"),
        "state": address.get("state"), "country_code": address.get("country_code"),
        "latitude": float(item["lat"]), "longitude": float(item["lon"]),
        "geometry": item.get("geojson") if item.get("category") == "highway" else None,
        "city_key": city_key(address, item.get("display_name", "")),
    }


async def _get(path, params):
    global _last_call
    async with _lock:
        wait = 1.1 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(f"{NOMINATIM}{path}", params={**params, "format": "jsonv2",
                                                                         "addressdetails": 1})
    if response.status_code != 200:
        raise HTTPException(502, "The map search service is unavailable. Try again in a moment.")
    return response.json()


async def search(query, limit=6, city=None):
    params = {"q": query, "limit": limit, "polygon_geojson": 1, "countrycodes": "ca"}
    module = CITIES.get(city or "")
    if module:
        west, south, east, north = module.BBOX
        params.update({"viewbox": f"{west},{north},{east},{south}", "bounded": 1})
        if fold(module.NAME) not in fold(query):
            params["q"] = f"{query}, {module.NAME}"
    items = await _get("/search", params)
    places = [_place(i) for i in items if i.get("category") == "highway" and i.get("type") in HIGHWAY_TYPES]
    return places or [_place(i) for i in items]


async def reverse(latitude, longitude):
    item = await _get("/reverse", {"lat": latitude, "lon": longitude, "zoom": 17, "polygon_geojson": 1})
    if "error" in item:
        raise HTTPException(404, "No street found at that point.")
    return _place(item)
