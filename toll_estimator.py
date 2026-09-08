import math
import re
from collections import defaultdict

import requests
from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree


IP_ROAD_LAYER_QUERY = (
    "https://sigip.infraestruturasdeportugal.pt/pub/rest/services/"
    "MOBILE_DRR/IPSIG_MOBILE_REDE_NOVO/MapServer/0/query"
)

# Class 1 average €/km by motorway/corridor. These are only applied to km that
# the official IP road layer itself marks as tolled (portagem=sim).
# Unknown tolled motorway refs use the conservative national fallback.
TOLL_RATE_PER_KM_BY_ROAD = {
    # MVP: one configurable average rate for every detected tolled kilometre.
    # Roads currently toll-free remain overridden to 0 below.
    "A22": 0.000,
    "A23": 0.000,
    "A24": 0.000,
    "A25": 0.000,
}

DEFAULT_TOLL_RATE_PER_KM = 0.080
TOLL_MODEL_VERSION = "v3-0.08-fallback"

# Approximate ratios between Portuguese toll classes. We are estimating the
# price after identifying the actual tolled kilometres, not reproducing a toll
# operator's exact entry/exit matrix.
CLASS_MULTIPLIER = {
    1: 1.00,
    2: 1.75,
    3: 2.25,
    4: 2.50,
}

# These motorways are nationally toll-free under the current 2026 rules.
# This is a safety override in case the public GIS layer has not yet reflected
# a legal change. Partial exemptions (e.g. A28) are intentionally NOT here;
# those are handled by the geometry's portagem flag section by section.
FULLY_FREE_2026 = {"A22", "A23", "A24", "A25"}

EARTH_RADIUS_M = 6_371_008.8


def _normalise_road_ref(value):
    if not value:
        return ""
    text = str(value).upper().replace(" ", "")
    text = text.replace("AUTOESTRADA", "A")
    if "/" in text:
        # A17/IC1 -> A17, A32/IC2 -> A32
        text = text.split("/", 1)[0]
    return text


def _haversine_m(a, b):
    lon1, lat1 = a
    lon2, lat2 = b
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _project_xy(lon, lat, lon0, lat0):
    """Local equirectangular projection, accurate enough for road matching."""
    x = (
        EARTH_RADIUS_M
        * math.radians(lon - lon0)
        * math.cos(math.radians(lat0))
    )
    y = EARTH_RADIUS_M * math.radians(lat - lat0)
    return x, y


def _project_geometry(geom, lon0, lat0):
    if geom.geom_type == "LineString":
        return LineString(
            [_project_xy(x, y, lon0, lat0) for x, y in geom.coords]
        )
    if geom.geom_type == "MultiLineString":
        parts = []
        for line in geom.geoms:
            parts.append(
                LineString(
                    [_project_xy(x, y, lon0, lat0) for x, y in line.coords]
                )
            )
        from shapely.geometry import MultiLineString
        return MultiLineString(parts)
    return geom


def _route_bbox(path, padding_deg=0.015):
    lons = [p[0] for p in path]
    lats = [p[1] for p in path]
    return (
        min(lons) - padding_deg,
        min(lats) - padding_deg,
        max(lons) + padding_deg,
        max(lats) + padding_deg,
    )


def fetch_tolled_ip_features(path, timeout=20):
    """
    Fetch only road sections that the official Infraestruturas de Portugal
    public road layer marks as tolled (portagem = 0 => 'sim').
    """
    if len(path) < 2:
        return []

    xmin, ymin, xmax, ymax = _route_bbox(path)
    envelope = f"{xmin},{ymin},{xmax},{ymax}"

    features = []
    offset = 0
    page_size = 1000

    while True:
        params = {
            "f": "geojson",
            "where": "portagem = 0",
            "outFields": (
                "roadnumber,categoria,portagem,jurisdicao,gestao,estado_"
            ),
            "returnGeometry": "true",
            "geometry": envelope,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }

        response = requests.get(
            IP_ROAD_LAYER_QUERY,
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("error"):
            raise RuntimeError(
                f"Erro na rede oficial IP: {payload['error']}"
            )

        batch = payload.get("features", [])
        features.extend(batch)

        if len(batch) < page_size:
            break

        offset += page_size
        if offset > 10000:
            break

    return features


def _densified_route_segments(path, max_piece_m=120.0):
    """Yield small route pieces as (start, end, length_m)."""
    for start, end in zip(path, path[1:]):
        length_m = _haversine_m(start, end)
        if length_m <= 0:
            continue

        pieces = max(1, int(math.ceil(length_m / max_piece_m)))
        for i in range(pieces):
            t0 = i / pieces
            t1 = (i + 1) / pieces
            a = (
                start[0] + (end[0] - start[0]) * t0,
                start[1] + (end[1] - start[1]) * t0,
            )
            b = (
                start[0] + (end[0] - start[0]) * t1,
                start[1] + (end[1] - start[1]) * t1,
            )
            yield a, b, _haversine_m(a, b)


def estimate_portuguese_tolls(
    path,
    vehicle_class=1,
    match_distance_m=85.0,
):
    """
    Estimate Portuguese toll cost using the *actual tolled road geometry*.

    1) Google gives the route polyline.
    2) Official IP GIS says which road geometries are tolled.
    3) We count only route metres that run close to those tolled geometries.
    4) We apply a Class-1 €/km average for the detected motorway, multiplied
       by the selected toll class.

    The result is still an estimate (not an operator invoice), but it avoids
    charging free motorway sections simply because they carry an A-road ref.
    """
    if len(path) < 2:
        return {
            "known": True,
            "cost": 0.0,
            "tolled_km": 0.0,
            "by_road_km": {},
            "source": "Sem percurso suficiente para estimar portagens",
        }

    features = fetch_tolled_ip_features(path)
    if not features:
        return {
            "known": True,
            "cost": 0.0,
            "tolled_km": 0.0,
            "by_road_km": {},
            "source": "Rede oficial IP: nenhum troço portajado cruzado",
        }

    lon0 = sum(p[0] for p in path) / len(path)
    lat0 = sum(p[1] for p in path) / len(path)

    projected_geoms = []
    road_refs = []

    for feature in features:
        geom_json = feature.get("geometry")
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
            projected = _project_geometry(geom, lon0, lat0)
        except Exception:
            continue

        road = _normalise_road_ref(
            feature.get("properties", {}).get("roadnumber")
        )
        if not road:
            road = "OUTRA"

        projected_geoms.append(projected)
        road_refs.append(road)

    if not projected_geoms:
        return {
            "known": False,
            "cost": 0.0,
            "tolled_km": 0.0,
            "by_road_km": {},
            "source": "Não foi possível interpretar a geometria oficial IP",
        }

    tree = STRtree(projected_geoms)
    by_road_m = defaultdict(float)

    for a, b, piece_m in _densified_route_segments(path):
        mid_lon = (a[0] + b[0]) / 2
        mid_lat = (a[1] + b[1]) / 2
        mx, my = _project_xy(mid_lon, mid_lat, lon0, lat0)
        point = Point(mx, my)

        try:
            nearest_index = tree.nearest(point)
        except Exception:
            continue

        if nearest_index is None:
            continue

        # Shapely 2 returns an integer index for STRtree.nearest.
        idx = int(nearest_index)
        nearest_geom = projected_geoms[idx]
        distance_m = point.distance(nearest_geom)

        if distance_m <= match_distance_m:
            road = road_refs[idx]
            if road in FULLY_FREE_2026:
                continue
            by_road_m[road] += piece_m

    by_road_km = {
        road: metres / 1000.0
        for road, metres in sorted(by_road_m.items())
        if metres > 30
    }

    multiplier = CLASS_MULTIPLIER.get(int(vehicle_class), 1.0)
    total_cost = 0.0
    details = []

    for road, km in by_road_km.items():
        rate = TOLL_RATE_PER_KM_BY_ROAD.get(
            road,
            DEFAULT_TOLL_RATE_PER_KM,
        )
        effective_rate = rate * multiplier
        cost = km * effective_rate
        total_cost += cost
        details.append(
            f"{road}: {km:.1f} km × {effective_rate:.3f} €/km"
        )

    tolled_km = sum(by_road_km.values())

    if details:
        source = "IP (troços portajados) · " + " · ".join(details)
    else:
        source = "Rede oficial IP: nenhum troço portajado cruzado"

    return {
        "known": True,
        "cost": round(total_cost, 2),
        "tolled_km": round(tolled_km, 1),
        "by_road_km": by_road_km,
        "source": source,
    }


MOTORWAY_RE = re.compile(r"(?<![A-Z0-9])A\s*[- ]?\s*(\d{1,2})(?!\d)", re.IGNORECASE)


def estimate_tolls_from_google_steps(steps, vehicle_class=1):
    """Fallback estimator when the IP GIS service is unavailable.

    Uses only Google route steps whose navigation instruction explicitly names
    a Portuguese motorway (A1, A3, A28, etc.). It then applies the same
    configurable average €/km. Fully toll-free motorways are excluded.

    This is intentionally a fallback: it is less precise than the IP geometry
    matcher for partially free motorways such as the A28.
    """
    by_road_m = defaultdict(float)

    for step in steps or []:
        instruction = (
            step.get("navigationInstruction", {}).get("instructions", "")
            or ""
        )
        distance_m = float(step.get("distanceMeters", 0) or 0)
        if distance_m <= 0 or not instruction:
            continue

        refs = MOTORWAY_RE.findall(instruction.upper())
        if not refs:
            continue

        road = f"A{refs[0]}"
        if road in FULLY_FREE_2026:
            continue

        by_road_m[road] += distance_m

    by_road_km = {
        road: metres / 1000.0
        for road, metres in sorted(by_road_m.items())
        if metres > 30
    }

    multiplier = CLASS_MULTIPLIER.get(int(vehicle_class), 1.0)
    total_cost = 0.0
    details = []

    for road, km in by_road_km.items():
        rate = TOLL_RATE_PER_KM_BY_ROAD.get(road, DEFAULT_TOLL_RATE_PER_KM)
        effective_rate = rate * multiplier
        total_cost += km * effective_rate
        details.append(f"{road}: {km:.1f} km × {effective_rate:.3f} €/km")

    tolled_km = sum(by_road_km.values())
    source = "Fallback Google (AE identificada)"
    if details:
        source += " · " + " · ".join(details)
    else:
        source += " · nenhum troço de AE portajada identificado"

    return {
        "known": True,
        "cost": round(total_cost, 2),
        "tolled_km": round(tolled_km, 1),
        "by_road_km": by_road_km,
        "source": source,
    }
