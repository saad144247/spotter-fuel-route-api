import time
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from routing.serializers import RouteFuelPlanRequestSerializer
from routing.services import (
    MPG,
    MAX_RANGE_MILES,
    fetch_osrm_route,
    find_candidate_stations_along_route,
    optimize_fuel_stops,
    resolve_us_location,
)


class RouteFuelPlanView(APIView):
    """
    POST /api/route-fuel-plan/
    Accepts `start` and `finish` locations within the USA and returns:
    - Route & map GeoJSON geometry + waypoint summary
    - Cost-effective fuel stops along the route (max 500-mile range, 10 MPG)
    - Total money spent on fuel
    """

    def post(self, request):
        started_at = time.perf_counter()
        serializer = RouteFuelPlanRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Invalid input", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_input = serializer.validated_data["start"]
        finish_input = serializer.validated_data["finish"]

        try:
            start_lat, start_lon, start_label = resolve_us_location(start_input)
            end_lat, end_lon, end_label = resolve_us_location(finish_input)

            # Single routing call to OSRM for road geometry & total distance
            route_data = fetch_osrm_route((start_lat, start_lon), (end_lat, end_lon))
            candidates = find_candidate_stations_along_route(route_data["cumulative_points"])
            fuel_stops, total_gallons, total_fuel_cost = optimize_fuel_stops(
                route_data["total_distance_miles"], candidates
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response(
                {"error": f"Routing or optimization error: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

        # Build clean map markers & polyline payload for frontend/map clients
        map_markers = [
            {
                "type": "start",
                "label": start_label,
                "latitude": start_lat,
                "longitude": start_lon,
                "mile_marker": 0.0,
            }
        ] + [
            {
                "type": "fuel_stop",
                "stop_number": stop["stop_number"],
                "label": f"{stop['station_name']} ({stop['city']}, {stop['state']})",
                "latitude": stop["latitude"],
                "longitude": stop["longitude"],
                "mile_marker": stop["mile_marker"],
                "price_per_gallon": stop["price_per_gallon"],
                "gallons_purchased": stop["gallons_purchased"],
                "stop_cost_usd": stop["stop_cost_usd"],
            }
            for stop in fuel_stops
        ] + [
            {
                "type": "finish",
                "label": end_label,
                "latitude": end_lat,
                "longitude": end_lon,
                "mile_marker": route_data["total_distance_miles"],
            }
        ]

        return Response(
            {
                "trip": {
                    "start": {"input": start_input, "resolved": start_label, "latitude": start_lat, "longitude": start_lon},
                    "finish": {"input": finish_input, "resolved": end_label, "latitude": end_lat, "longitude": end_lon},
                    "total_distance_miles": route_data["total_distance_miles"],
                    "estimated_duration_hours": route_data["estimated_duration_hours"],
                    "vehicle_specs": {
                        "max_range_miles": MAX_RANGE_MILES,
                        "mpg": MPG,
                    },
                },
                "fuel_summary": {
                    "total_fuel_stops": len(fuel_stops),
                    "total_gallons": total_gallons,
                    "total_fuel_cost_usd": total_fuel_cost,
                    "currency": "USD",
                },
                "fuel_stops": fuel_stops,
                "map": {
                    "route_geojson": route_data["geometry"],
                    "markers": map_markers,
                },
                "meta": {
                    "routing_api_calls": 1,
                    "processing_time_ms": elapsed_ms,
                },
            },
            status=status.HTTP_200_OK,
        )