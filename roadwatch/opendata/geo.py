"""Minimal geometry helpers with no GIS dependency: read line geometries from a GeoPackage and reproject
Transverse-Mercator coordinates (e.g. Québec MTM zones) to WGS84 longitude/latitude."""
import math
import sqlite3
import struct

# GRS80 / NAD83 ellipsoid (NAD83 vs WGS84 differs by ~1 m here - irrelevant for a map overlay)
A, F = 6378137.0, 1 / 298.257222101
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)

PROJECTIONS = {
    32188: {"lon0": -73.5, "k0": 0.9999, "fe": 304800.0, "fn": 0.0},   # NAD83 / MTM zone 8 (Montréal)
    32187: {"lon0": -70.5, "k0": 0.9999, "fe": 304800.0, "fn": 0.0},   # MTM zone 7
}


def tm_to_lonlat(x, y, lon0, k0, fe, fn):
    """Inverse Transverse Mercator (Snyder 1987, 8-11 .. 8-25)."""
    e1 = (1 - math.sqrt(1 - E2)) / (1 + math.sqrt(1 - E2))
    m = (y - fn) / k0
    mu = m / (A * (1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = EP2 * cos1 ** 2
    t1 = tan1 ** 2
    n1 = A / math.sqrt(1 - E2 * sin1 ** 2)
    r1 = A * (1 - E2) / (1 - E2 * sin1 ** 2) ** 1.5
    d = (x - fe) / (n1 * k0)
    lat = phi1 - (n1 * tan1 / r1) * (d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * EP2) * d ** 4 / 24
                                     + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * EP2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = math.radians(lon0) + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                                + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * EP2 + 24 * t1 ** 2) * d ** 5 / 120) / cos1
    return round(math.degrees(lon), 6), round(math.degrees(lat), 6)


def _wkb_lines(wkb, transform):
    """LineString / MultiLineString WKB (2D, 3D or measured) -> list of [[lon, lat], ...] parts."""
    order = "<" if wkb[0] == 1 else ">"
    geom_type = struct.unpack_from(order + "I", wkb, 1)[0]
    base, dims = geom_type % 1000, 2 + (geom_type // 1000 in (1, 3)) + (geom_type // 1000 in (2, 3))
    if geom_type & 0x80000000:
        dims += 1
    if geom_type & 0x40000000:
        dims += 1
    base = geom_type & 0xFF if base > 7 else base
    offset = 5

    def read_line():
        nonlocal offset
        count = struct.unpack_from(order + "I", wkb, offset)[0]
        offset += 4
        points = []
        for _ in range(count):
            coords = struct.unpack_from(order + "d" * dims, wkb, offset)
            offset += 8 * dims
            points.append(list(transform(coords[0], coords[1])))
        return points

    if base == 2:
        return [read_line()]
    if base == 5:
        parts, count = [], struct.unpack_from(order + "I", wkb, offset)[0]
        offset += 4
        for _ in range(count):
            offset += 5  # each part's own byte order + type
            parts.append(read_line())
        return parts
    return []


def gpkg_geometry(blob, srs_id):
    """GeoPackage geometry blob -> GeoJSON LineString/MultiLineString in WGS84, or None."""
    if not blob or blob[:2] != b"GP":
        return None
    flags = blob[3]
    envelope_size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7, 0)
    wkb = blob[8 + envelope_size:]
    projection = PROJECTIONS.get(srs_id)
    transform = (lambda x, y: tm_to_lonlat(x, y, **projection)) if projection else (lambda x, y: (round(x, 6), round(y, 6)))
    parts = _wkb_lines(wkb, transform)
    if not parts:
        return None
    return {"type": "LineString", "coordinates": parts[0]} if len(parts) == 1 else {"type": "MultiLineString", "coordinates": parts}


def gpkg_features(path):
    """Yield (properties dict, GeoJSON geometry) for every row of the first feature table in a GeoPackage."""
    db = sqlite3.connect(path)
    try:
        table, srs_id = db.execute("SELECT table_name, srs_id FROM gpkg_contents WHERE data_type = 'features'").fetchone()
        geometry_column = db.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?", (table,)).fetchone()[0]
        columns = [c[1] for c in db.execute(f'PRAGMA table_info("{table}")') if c[1] not in (geometry_column, "fid")]
        select = ", ".join(f'"{c}"' for c in columns)
        for row in db.execute(f'SELECT {select}, "{geometry_column}" FROM "{table}"'):
            geometry = gpkg_geometry(row[-1], srs_id)
            if geometry:
                yield dict(zip(columns, row[:-1])), geometry
    finally:
        db.close()


def bbox_of(geometry):
    coords = geometry["coordinates"] if geometry["type"] == "LineString" else [p for part in geometry["coordinates"] for p in part]
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    return min(xs), min(ys), max(xs), max(ys)


def intersects(box, other):
    return not (box[2] < other[0] or box[0] > other[2] or box[3] < other[1] or box[1] > other[3])
