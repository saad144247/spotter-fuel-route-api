import math
from typing import Dict, List, Optional, Tuple
import pgeocode
import requests
from django.core.cache import cache
from routing.models import FuelStation

MAX_RANGE_MILES = 500.0
MPG = 10.0
TANK_CAPACITY_GALLONS = MAX_RANGE_MILES / MPG  # 50.0 gallons
METERS_TO_MILES = 0.000621371192
SEARCH_CORRIDOR_MILES = 20.0

# Preload offline US city/zip lookup once for instant geocoding (0 external calls for US cities/zips)
_NOMI_US = pgeocode.Nominatim("us")
_US_DF = _NOMI_US._data[["postal_code", "place_name", "state_code", "latitude", "longitude"]].dropna()
_US_DF["city_key"] = _US_DF["place_name"].str.strip().str.lower()
_US_DF["state_key"] = _US_DF["state_code"].str.strip().str.upper()

_CITY_LOOKUP: Dict[Tuple[str, str], Tuple[float, float]] = {
    (idx[0], idx[1]): (float(row["latitude"]), float(row["longitude"]))
    for idx, row in _US_DF.groupby(["city_key", "state_key"])[["latitude", "longitude"]].mean().iterrows()
}
_CITY_ONLY_LOOKUP: Dict[str, Tuple[float, float]] = {
    idx: (float(row["latitude"]), float(row["longitude"]))
    for idx, row in _US_DF.groupby("city_key")[["latitude", "longitude"]].mean().iterrows()
}
_ZIP_LOOKUP: Dict[str, Tuple[float, float]] = {
    str(row["postal_code"]).strip(): (float(row["latitude"]), float(row["longitude"]))
    for _, row in _US_DF.drop_duplicates("postal_code").iterrows()
}


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def resolve_us_location(location_str: str) -> Tuple[float, float, str]:
    """
    Resolve a US location string (e.g., 'Dallas, TX', 'New York, NY', '75201', or 'lat,lon')
    into (lat, lon, normalized_label) using offline US dataset first, with cached Nominatim fallback.
    """
    raw = location_str.strip()
    cache_key = f"loc_resolve:{raw.lower()}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # Check if user passed "lat, lon" directly
    if "," in raw:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) == 2:
            try:
                lat, lon = float(parts[0]), float(parts[1])
                if 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0:
                    res = (lat, lon, f"{lat:.4f}, {lon:.4f}")
                    cache.set(cache_key, res, timeout=86400)
                    return res
            except ValueError:
                pass

    # Check 5-digit US ZIP code
    zip_candidate = raw.split()[0] if raw else ""
    if zip_candidate.isdigit() and len(zip_candidate) == 5 and zip_candidate in _ZIP_LOOKUP:
        lat, lon = _ZIP_LOOKUP[zip_candidate]
        res = (lat, lon, raw)
        cache.set(cache_key, res, timeout=86400)
        return res

    # Check "City, ST" or "City, State"
    if "," in raw:
        city_part, state_part = [p.strip() for p in raw.split(",", 1)]
        state_code = state_part.split()[0].upper()
        if (city_part.lower(), state_code) in _CITY_LOOKUP:
            lat, lon = _CITY_LOOKUP[(city_part.lower(), state_code)]
            res = (lat, lon, f"{city_part.title()}, {state_code}")
            cache.set(cache_key, res, timeout=86400)
            return res

    # Check City name alone
    if raw.lower() in _CITY_ONLY_LOOKUP:
        lat, lon = _CITY_ONLY_LOOKUP[raw.lower()]
        res = (lat, lon, raw.title())
        cache.set(cache_key, res, timeout=86400)
        return res

    # Fallback: single Nominatim lookup for street addresses or landmarks in USA
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": f"{raw}, USA", "format": "json", "limit": 1, "countrycodes": "us"},
        headers={"User-Agent": "SpotterDjangoFuelRouter/1.0"},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Could not find USA location: '{location_str}'")

    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    res = (lat, lon, data[0].get("display_name", raw))
    cache.set(cache_key, res, timeout=86400)
    return res


def fetch_osrm_route(start_coords: Tuple[float, float], end_coords: Tuple[float, float]) -> Dict:
    """
    Makes ONE call to the free OSRM routing API to retrieve route geometry and total distance.
    """
    s_lat, s_lon = start_coords
    e_lat, e_lon = end_coords
    cache_key = f"osrm_route:{s_lat:.4f},{s_lon:.4f}->{e_lat:.4f},{e_lon:.4f}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    url = f"http://router.project-osrm.org/route/v1/driving/{s_lon},{s_lat};{e_lon},{e_lat}"
    resp = requests.get(
        url,
        params={"overview": "full", "geometries": "geojson", "steps": "false"},
        timeout=12,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise ValueError("No driving route found between the specified USA locations.")

    route = payload["routes"][0]
    coords_lon_lat = route["geometry"]["coordinates"]  # [[lon, lat], ...]
    total_distance_miles = route["distance"] * METERS_TO_MILES
    duration_hours = route["duration"] / 3600.0

    # Compute cumulative mileage along route polyline vertices
    cumulative_points = []
    cum_miles = 0.0
    prev_lat, prev_lon = coords_lon_lat[0][1], coords_lon_lat[0][0]
    cumulative_points.append((prev_lat, prev_lon, 0.0))

    for lon, lat in coords_lon_lat[1:]:
        seg = haversine_miles(prev_lat, prev_lon, lat, lon)
        cum_miles += seg
        cumulative_points.append((lat, lon, cum_miles))
        prev_lat, prev_lon = lat, lon

    # Scale cumulative haversine distances slightly so final mile matches exact OSRM road distance
    if cum_miles > 0:
        scale = total_distance_miles / cum_miles
        cumulative_points = [(lat, lon, m * scale) for lat, lon, m in cumulative_points]

    result = {
        "total_distance_miles": round(total_distance_miles, 2),
        "estimated_duration_hours": round(duration_hours, 2),
        "geometry": route["geometry"],
        "cumulative_points": cumulative_points,
    }
    cache.set(cache_key, result, timeout=3600)
    return result


def find_candidate_stations_along_route(cumulative_points: List[Tuple[float, float, float]]) -> List[Dict]:
    """
    Finds fuel stations within SEARCH_CORRIDOR_MILES of the route and maps each to its nearest route mile marker.
    """
    lats = [p[0] for p in cumulative_points]
    lons = [p[1] for p in cumulative_points]
    pad = 0.45  # ~30 miles bounding box padding

    qs = FuelStation.objects.filter(
        latitude__gte=min(lats) - pad,
        latitude__lte=max(lats) + pad,
        longitude__gte=min(lons) - pad,
        longitude__lte=max(lons) + pad,
    ).values("opis_id", "name", "address", "city", "state", "retail_price", "latitude", "longitude")

    # Sample route points every ~4 miles for fast distance projection
    sampled = [cumulative_points[0]]
    for pt in cumulative_points[1:]:
        if pt[2] - sampled[-1][2] >= 4.0:
            sampled.append(pt)
    if sampled[-1] != cumulative_points[-1]:
        sampled.append(cumulative_points[-1])

    candidates_by_opis: Dict[int, Dict] = {}
    for st in qs:
        st_lat, st_lon = st["latitude"], st["longitude"]
        best_dist = float("inf")
        best_mile = 0.0

        for r_lat, r_lon, r_mile in sampled:
            if abs(st_lat - r_lat) > 0.45 or abs(st_lon - r_lon) > 0.55:
                continue
            d = haversine_miles(st_lat, st_lon, r_lat, r_lon)
            if d < best_dist:
                best_dist = d
                best_mile = r_mile

        if best_dist <= SEARCH_CORRIDOR_MILES:
            opis = st["opis_id"]
            # Keep lowest price if duplicate OPIS entry exists
            if opis not in candidates_by_opis or st["retail_price"] < candidates_by_opis[opis]["retail_price"]:
                candidates_by_opis[opis] = {
                    **st,
                    "mile_marker": round(best_mile, 2),
                    "distance_off_route_miles": round(best_dist, 2),
                }

    stations = sorted(candidates_by_opis.values(), key=lambda x: x["mile_marker"])
    return stations


def optimize_fuel_stops(
    total_distance_miles: float, stations: List[Dict]
) -> Tuple[List[Dict], float, float]:
    """
    Computes optimal cost-effective fuel stops assuming:
    - Vehicle starts with a full tank (500-mile range / 50 gallons)
    - Vehicle achieves 10 MPG
    - Lookahead strategy: at current position, look within reachable 500 miles.
      If a cheaper station is reachable ahead, buy only enough fuel to reach that cheaper station.
      Otherwise, fill tank up to full (or enough to finish the trip if destination is within 500 miles).
    """
    total_gallons_needed = total_distance_miles / MPG

    if total_distance_miles <= MAX_RANGE_MILES:
        # Vehicle starts with full 500-mile range; calculate fuel cost using cheapest local/route rate
        avg_price = (
            min(s["retail_price"] for s in stations)
            if stations
            else float(FuelStation.objects.order_by("retail_price").first().retail_price)
        )
        total_cost = round(total_gallons_needed * avg_price, 2)
        return [], round(total_gallons_needed, 2), total_cost

    # Add virtual destination node at total_distance_miles with price 0
    nodes = [
        {
            "mile_marker": 0.0,
            "retail_price": stations[0]["retail_price"] if stations else 3.50,
            "is_start": True,
        }
    ] + stations + [
        {
            "mile_marker": total_distance_miles,
            "retail_price": 0.0,
            "is_destination": True,
        }
    ]

    stops_plan: List[Dict] = []
    current_mile = 0.0
    fuel_remaining_miles = MAX_RANGE_MILES  # Starts with full tank (500 miles)
    total_cost = 0.0
    total_gallons_purchased = 0.0

    # Account for initial tank fuel cost at starting market price
    initial_gallons = min(total_distance_miles, MAX_RANGE_MILES) / MPG
    start_price = nodes[0]["retail_price"]
    total_cost += initial_gallons * start_price
    total_gallons_purchased += initial_gallons

    idx = 0
    while current_mile + fuel_remaining_miles < total_distance_miles:
        reachable = [
            (i, n)
            for i, n in enumerate(nodes)
            if i > idx and 0 < (n["mile_marker"] - current_mile) <= MAX_RANGE_MILES
        ]

        if not reachable:
            # Fallback if a sparse rural stretch exceeds 500 miles between stations
            next_candidates = [(i, n) for i, n in enumerate(nodes) if i > idx]
            if not next_candidates:
                break
            next_idx, next_node = next_candidates[0]
        else:
            # Prefer the first cheaper station reachable within 500 miles; else pick cheapest reachable
            current_price = nodes[idx]["retail_price"]
            cheaper = [(i, n) for i, n in reachable if n["retail_price"] < current_price]
            if cheaper:
                next_idx, next_node = cheaper[0]
            else:
                next_idx, next_node = min(reachable, key=lambda item: item[1]["retail_price"])

        leg_miles = next_node["mile_marker"] - current_mile
        fuel_remaining_miles -= leg_miles
        current_mile = next_node["mile_marker"]
        idx = next_idx

        if next_node.get("is_destination"):
            break

        # Decide how much fuel to buy at this station
        remaining_trip_miles = total_distance_miles - current_mile
        future_reachable = [
            n
            for n in nodes[idx + 1 :]
            if 0 < (n["mile_marker"] - current_mile) <= MAX_RANGE_MILES
        ]
        cheaper_ahead = [
            n for n in future_reachable if n["retail_price"] < next_node["retail_price"]
        ]

        if cheaper_ahead:
            target_miles = cheaper_ahead[0]["mile_marker"] - current_mile
        else:
            target_miles = min(MAX_RANGE_MILES, remaining_trip_miles)

        miles_to_buy = max(0.0, target_miles - max(0.0, fuel_remaining_miles))
        if miles_to_buy > 0:
            gallons_bought = miles_to_buy / MPG
            cost = gallons_bought * next_node["retail_price"]
            fuel_remaining_miles = max(0.0, fuel_remaining_miles) + miles_to_buy
            total_cost += cost
            total_gallons_purchased += gallons_bought

            stops_plan.append(
                {
                    "stop_number": len(stops_plan) + 1,
                    "opis_id": next_node["opis_id"],
                    "station_name": next_node["name"],
                    "address": next_node["address"],
                    "city": next_node["city"],
                    "state": next_node["state"],
                    "latitude": next_node["latitude"],
                    "longitude": next_node["longitude"],
                    "mile_marker": next_node["mile_marker"],
                    "distance_off_route_miles": next_node["distance_off_route_miles"],
                    "price_per_gallon": round(next_node["retail_price"], 3),
                    "gallons_purchased": round(gallons_bought, 2),
                    "stop_cost_usd": round(cost, 2),
                }
            )

    return stops_plan, round(total_gallons_purchased, 2), round(total_cost, 2)