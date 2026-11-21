# Spotter Backend Django Engineer — Route & Fuel Stop Optimization API

A Django REST Framework API that calculates a driving route between two locations within the USA, finds cost-effective fuel stops using the provided OPIS fuel prices dataset, and returns total fuel cost assuming a **500-mile max range** and **10 MPG** fuel economy.

## Key Features
- **Single Routing API Call:** Uses free OSRM (`router.project-osrm.org`) in a single request per route and caches results.
- **Fast Local US Geocoding:** Uses `pgeocode` offline US postal/city lookup (`city_lookup` / `zip_lookup`) with cached Nominatim fallback.
- **Cost-Effective Fuel Stop Planner:** Evaluates candidate stations along the route corridor under a 500-mile range and 10 MPG constraint, returning per-stop gallons/cost and total fuel spend.
- **Map-Ready Payload:** Returns route GeoJSON geometry and ordered map markers (`start`, `fuel_stop`, `finish`).

## Setup & Run

1. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   .\venv\Scripts\Activate.ps1