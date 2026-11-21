import time
from pathlib import Path
import pandas as pd
import pgeocode
import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from routing.models import FuelStation


class Command(BaseCommand):
    help = "Import fuel stations from CSV and fast-geocode using US postal dataset."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            type=str,
            default="fuel-prices-for-be-assessment.csv",
            help="Path to fuel prices CSV file",
        )

    def handle(self, *args, **options):
        csv_path = Path(settings.BASE_DIR) / options["csv"]
        if not csv_path.exists():
            self.stderr.write(self.style.ERROR(f"CSV not found at: {csv_path}"))
            return

        df = pd.read_csv(csv_path)

        # Build fast offline lookup for US (city, state) -> (lat, lon)
        nomi = pgeocode.Nominatim("us")
        us_df = nomi._data[["place_name", "state_code", "latitude", "longitude"]].dropna()
        us_df["city_key"] = us_df["place_name"].str.strip().str.lower()
        us_df["state_key"] = us_df["state_code"].str.strip().str.upper()

        city_coords = (
            us_df.groupby(["city_key", "state_key"])[["latitude", "longitude"]]
            .mean()
            .to_dict("index")
        )

        FuelStation.objects.all().delete()
        stations = []
        missing_pairs = set()

        for _, row in df.iterrows():
            city = str(row["City"]).strip()
            state = str(row["State"]).strip().upper()
            match = city_coords.get((city.lower(), state))

            lat = float(match["latitude"]) if match else None
            lon = float(match["longitude"]) if match else None
            if match is None:
                missing_pairs.add((city, state))

            stations.append(
                FuelStation(
                    opis_id=int(row["OPIS Truckstop ID"]),
                    name=str(row["Truckstop Name"]).strip(),
                    address=str(row.get("Address", "") or "").strip(),
                    city=city,
                    state=state,
                    rack_id=int(row["Rack ID"]) if pd.notna(row.get("Rack ID")) else None,
                    retail_price=float(row["Retail Price"]),
                    latitude=lat,
                    longitude=lon,
                )
            )

        FuelStation.objects.bulk_create(stations, batch_size=1000)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(stations)} stations ({len(missing_pairs)} unmatched city/state pairs)."
            )
        )

        # Fallback Nominatim lookup only for the few unmatched pairs
        headers = {"User-Agent": "SpotterDjangoFuelRouter/1.0"}
        for city, state in sorted(missing_pairs):
            try:
                resp = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": f"{city}, {state}, USA", "format": "json", "limit": 1},
                    headers=headers,
                    timeout=8,
                )
                if resp.ok and resp.json():
                    item = resp.json()[0]
                    FuelStation.objects.filter(
                        city__iexact=city, state__iexact=state
                    ).update(
                        latitude=float(item["lat"]),
                        longitude=float(item["lon"]),
                    )
                time.sleep(1.0)
            except Exception:
                continue

        geocoded = FuelStation.objects.filter(latitude__isnull=False).count()
        self.stdout.write(
            self.style.SUCCESS(f"Done! {geocoded}/{len(stations)} stations have coordinates.")
        )