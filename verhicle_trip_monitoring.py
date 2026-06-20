import requests
import json
import os
from datetime import datetime, timezone
import sqlite3

BASE_URL = "https://api.cartelsol.mosdon-dev.com"
TOKEN_URL = "https://auth.cartelsol.mosdon-dev.com/oauth2/token"

# Bearer Token hier einfuegen.
BEARER_TOKEN = ""

VEHICLES_ENDPOINT = "/vehicles"
TRIPS_BY_VEHICLE_ENDPOINT = "/trips/vehicle/{vin}"


def get_headers():
    if not BEARER_TOKEN:
        raise ValueError("Bitte zuerst den Bearer Token in BEARER_TOKEN einfuegen.")

    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
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
