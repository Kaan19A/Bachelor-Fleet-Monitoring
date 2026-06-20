import requests
import json
import os
from datetime import datetime, timezone
import sqlite3

BASE_URL = "https://api.cartelsol.mosdon-dev.com"
TOKEN_URL = "https://auth.cartelsol.mosdon-dev.com/oauth2/token"

VEHICLES_ENDPOINT = "/vehicles"
TRIPS_BY_VEHICLE_ENDPOINT = "/trips/vehicle/{vin}"
DEFAULT_DB_PATH = "trips.db"


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


def get_trip_data(vehicles):
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


def save_to_sqlite(vehicles, trips):
    db_path = os.getenv("SQLITE_DB_PATH", DEFAULT_DB_PATH)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vehicles (
        vin TEXT PRIMARY KEY,
        raw_json TEXT NOT NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trips (
        id TEXT PRIMARY KEY,
        vin TEXT,
        raw_json TEXT NOT NULL
    );
    """)

    for vehicle in vehicles:
        vin = vehicle.get("vin")
        if not vin:
            continue

        cursor.execute("""
        INSERT INTO vehicles (vin, raw_json)
        VALUES (?, ?)
        ON CONFLICT(vin) DO UPDATE SET
            raw_json = excluded.raw_json;
        """, (vin, json.dumps(vehicle, ensure_ascii=False)))

    saved_trips = 0
    for trip in trips:
        trip_id = trip.get("id")
        vin = trip.get("vin")

        if trip_id is None:
            continue

        cursor.execute("""
        INSERT INTO trips (id, vin, raw_json)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            vin = excluded.vin,
            raw_json = excluded.raw_json;
        """, (str(trip_id), vin, json.dumps(trip, ensure_ascii=False)))
        saved_trips += 1

    conn.commit()
    conn.close()

    return db_path, len(vehicles), saved_trips


if __name__ == "__main__":
    vehicle_data = get_vehicles()
    trip_data = get_trip_data(vehicle_data)
    db_file, vehicle_count, trip_count = save_to_sqlite(vehicle_data, trip_data)

    print("\n==============================")
    print(f"SQLite DB aktualisiert: {db_file}")
    print(f"Vehicles gespeichert/aktualisiert: {vehicle_count}")
    print(f"Trips gespeichert/aktualisiert: {trip_count}")
