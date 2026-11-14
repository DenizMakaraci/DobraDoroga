"""Street lookup through OpenStreetMap Nominatim, proxied server-side so the browser never talks to a
third party and the usage policy (identifying User-Agent, about 1 request/second) is honoured.

Nominatim's public server turns away some shared hosting addresses. When it does not answer, the same lookup
goes to Photon (photon.komoot.io), another free search over OpenStreetMap; it returns a street as a point
without its line, so the map shows a marker instead of the highlighted road."""
import asyncio
import logging
import time

import httpx
from fastapi import HTTPException

from .opendata import CITIES
from .opendata.common import USER_AGENT, fold

NOMINATIM = "https://nominatim.openstreetmap.org"
PHOTON = "https://photon.komoot.io"
UNAVAILABLE = ("The map search service is unavailable, so the map cannot be clicked right now. "
               "Type the street's name in the search box and press Search to add it by name.")


class Unavailable(Exception):
    pass
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
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
                response = await client.get(f"{NOMINATIM}{path}", params={**params, "format": "jsonv2",
                                                                             "addressdetails": 1})
        except httpx.HTTPError as error:
            raise Unavailable(type(error).__name__)
    if response.status_code != 200:
        raise Unavailable(f"HTTP {response.status_code}")
    return response.json()


async def _photon(path, params):
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(f"{PHOTON}{path}", params={**params, "osm_tag": "highway"})
    except httpx.HTTPError as error:
        raise Unavailable(type(error).__name__)
    if response.status_code != 200:
        raise Unavailable(f"HTTP {response.status_code}")
    return response.json().get("features", [])


def _photon_places(features):
    """Photon features as places, one per street name and city (Photon lists every segment of a street)."""
    places, seen = [], set()
    for feature in features:
        p, point = feature.get("properties", {}), feature.get("geometry", {}).get("coordinates") or []
        if p.get("osm_key") != "highway" or p.get("osm_value") not in HIGHWAY_TYPES or not p.get("name") or len(point) != 2:
            continue
        if (p.get("countrycode") or "").upper() != "CA" or (fold(p["name"]), p.get("city")) in seen:
            continue
        seen.add((fold(p["name"]), p.get("city")))
        display = ", ".join(part for part in (p["name"], p.get("district"), p.get("city"), p.get("state"), p.get("country")) if part)
        places.append({
            "osm_type": {"W": "way", "N": "node", "R": "relation"}.get(p.get("osm_type")), "osm_id": p.get("osm_id"),
            "display_name": display, "road": p["name"], "city": p.get("city"), "state": p.get("state"), "country_code": "ca",
            "latitude": float(point[1]), "longitude": float(point[0]), "geometry": None,
            "city_key": city_key({"city": p.get("city"), "county": p.get("county")}, display),
        })
    return places


def _by_name(query, module):
    """With no map service answering, the street as typed: open data is matched by street name, so the case
    works without the street being located. It is placed at the city centre and says so."""
    road = " ".join(query.split())[:240]
    return {"osm_type": None, "osm_id": None, "road": road, "city": module.NAME, "state": None, "country_code": "ca",
            "display_name": f"{road}, {module.NAME} - added by name; map search is unavailable, so it is not located on the map",
            "latitude": module.CENTRE[0], "longitude": module.CENTRE[1], "geometry": None, "city_key": module.KEY, "by_name": True}


async def _fallback(reason, path, params):
    logging.getLogger("roadwatch").warning("Nominatim did not answer (%s); using Photon", reason)
    try:
        return _photon_places(await _photon(path, params))
    except Unavailable as error:
        logging.getLogger("roadwatch").warning("Photon did not answer either (%s)", error)
        raise HTTPException(502, UNAVAILABLE)


async def search(query, limit=6, city=None):
    params = {"q": query, "limit": limit, "polygon_geojson": 1, "countrycodes": "ca"}
    module = CITIES.get(city or "")
    if module:
        west, south, east, north = module.BBOX
        params.update({"viewbox": f"{west},{north},{east},{south}", "bounded": 1})
        if fold(module.NAME) not in fold(query):
            params["q"] = f"{query}, {module.NAME}"
    try:
        items = await _get("/search", params)
    except Unavailable as error:
        box = {"bbox": ",".join(str(v) for v in module.BBOX)} if module else {}
        try:
            return (await _fallback(error, "/api/", {"q": params["q"], "limit": 30, **box}))[:limit]
        except HTTPException:
            if not module:
                raise
            return [_by_name(query, module)]
    places = [_place(i) for i in items if i.get("category") == "highway" and i.get("type") in HIGHWAY_TYPES]
    return places or [_place(i) for i in items]


async def reverse(latitude, longitude):
    try:
        item = await _get("/reverse", {"lat": latitude, "lon": longitude, "zoom": 17, "polygon_geojson": 1})
    except Unavailable as error:
        places = await _fallback(error, "/reverse", {"lat": latitude, "lon": longitude, "limit": 5, "radius": 0.2})
        if not places:
            raise HTTPException(404, "No street found at that point.")
        return places[0]
    if "error" in item:
        raise HTTPException(404, "No street found at that point.")
    return _place(item)
