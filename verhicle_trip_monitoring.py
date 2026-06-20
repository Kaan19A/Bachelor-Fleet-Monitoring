import requests
import json
import os
from datetime import datetime, timezone
import sqlite3

BASE_URL = "https://api.cartelsol.mosdon-dev.com"
TOKEN_URL = "https://auth.cartelsol.mosdon-dev.com/oauth2/token"

VEHICLES_ENDPOINT = "/vehicles"
TRIPS_BY_VEHICLE_ENDPOINT = "/trips/vehicle/{vin}"


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


load_env_file()


def get_headers():
    bearer_token = os.getenv("CARTELSOL_BEARER_TOKEN")

    if not bearer_token:
        raise ValueError(
            "Bitte zuerst CARTELSOL_BEARER_TOKEN in der .env Datei eintragen."
        )

    return {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
    }


def get_json(endpoint):
    url = f"{BASE_URL}{endpoint}"
    response = requests.get(url, headers=get_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def get_vehicles():
    vehicles_data = get_json(VEHICLES_ENDPOINT)

    if isinstance(vehicles_data, dict) and isinstance(vehicles_data.get("items"), list):
        return vehicles_data["items"]
    if isinstance(vehicles_data, list):
        return vehicles_data

    raise ValueError("Unerwartetes Format von /vehicles")


def get_trip_data():
    vehicles = get_vehicles()
    vins = [vehicle["vin"] for vehicle in vehicles if "vin" in vehicle]
    vins = list(dict.fromkeys(vins))

    if not vins:
        raise ValueError("Keine VINs im Vehicles-Response gefunden")

    all_trips = []

    for vin in vins:
        endpoint = TRIPS_BY_VEHICLE_ENDPOINT.format(vin=vin)
        trips_data = get_json(endpoint)

        if isinstance(trips_data, dict) and isinstance(trips_data.get("items"), list):
            trips = trips_data["items"]
        elif isinstance(trips_data, list):
            trips = trips_data
        else:
            trips = [trips_data]

        for trip in trips:
            if isinstance(trip, dict):
                trip["vin"] = vin
                all_trips.append(trip)

        print(f"VIN={vin} | Trips={len(trips)}")

    return all_trips


if __name__ == "__main__":
    trip_data = get_trip_data()
    print(json.dumps(trip_data, indent=2, ensure_ascii=False))
