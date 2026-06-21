import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.cartelsol.mosdon-dev.com"
TOKEN_URL = "https://auth.cartelsol.mosdon-dev.com/oauth2/token"

VEHICLES_ENDPOINT = "/vehicles"
TRIPS_BY_VEHICLE_ENDPOINT = "/trips/vehicle/{vin}"
DEFAULT_DB_PATH = "trips.db"
TIMEOUT_SECONDS = 300
LONG_TRIP_SECONDS = 2 * 60 * 60


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


def get_headers():
    bearer_token = os.getenv("CARTELSOL_BEARER_TOKEN")
    if not bearer_token:
        raise ValueError("Bitte CARTELSOL_BEARER_TOKEN in der .env Datei eintragen.")

    return {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
    }


def get_json(endpoint):
    url = f"{BASE_URL}{endpoint}"
    response = requests.get(url, headers=get_headers(), timeout=30)
    print(f"GET {url} -> {response.status_code}")
    response.raise_for_status()
    return response.json()


def normalize_response_items(data, endpoint_name):
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]

    raise ValueError(f"Unerwartetes Format von {endpoint_name}")


def get_vehicles():
    return normalize_response_items(get_json(VEHICLES_ENDPOINT), VEHICLES_ENDPOINT)


def get_trip_data(vehicles):
    vins = [vehicle["vin"] for vehicle in vehicles if vehicle.get("vin")]
    vins = list(dict.fromkeys(vins))
    if not vins:
        raise ValueError("Keine VINs im Vehicles-Response gefunden")

    all_trips = []
    for vin in vins:
        endpoint = TRIPS_BY_VEHICLE_ENDPOINT.format(vin=vin)
        trips = normalize_response_items(get_json(endpoint), endpoint)

        for trip in trips:
            if isinstance(trip, dict):
                trip["vin"] = vin
                all_trips.append(trip)

        print(f"VIN={vin} | Trips={len(trips)}")

    return all_trips


def first_value(data, *keys):
    for key in keys:
        if key in data and data[key] not in ("", None):
            return data[key]
    return None


def nested_value(data, *path):
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def to_float(value):
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def to_int(value):
    number = to_float(value)
    return int(number) if number is not None else None


def normalize_iso(value):
    if value in ("", None):
        return None
    if isinstance(value, str) and value.strip().lower() == "trip not finished":
        return None

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def epoch_seconds(iso_value):
    if not iso_value:
        return None
    return int(datetime.fromisoformat(iso_value).timestamp())


def stable_trip_id(trip):
    source_id = first_value(trip, "id", "tripId", "trip_id", "uuid")
    if source_id is not None:
        return str(source_id), str(source_id)

    raw_json = json.dumps(trip, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw_json.encode("utf-8")).hexdigest(), None


def detect_hardware_group(vehicle):
    candidates = [
        first_value(vehicle, "hardwareGroup", "hardware_group", "hardwareVersion"),
        first_value(vehicle, "model", "vehicleModel"),
        nested_value(vehicle, "vehicleData", "hardwareVersion"),
    ]
    text = " ".join(str(item) for item in candidates if item).upper()

    if "C1" in text:
        return "C1"
    if "C3" in text:
        return "C3"
    return None


def vehicle_row(vehicle):
    vehicle_data = vehicle.get("vehicleData") if isinstance(vehicle.get("vehicleData"), dict) else {}

    return {
        "vin": vehicle.get("vin"),
        "vehicle_model": first_value(vehicle, "model", "vehicleModel"),
        "license_plate": first_value(vehicle, "licensePlate", "license_plate"),
        "pairing_state": first_value(vehicle, "pairingState", "pairing_state"),
        "hardware_group": detect_hardware_group(vehicle),
        "odometer": to_float(first_value(vehicle_data, "odometer")),
        "fuel_level": to_float(first_value(vehicle_data, "fuelLevel", "fuel_level")),
        "last_communication": normalize_iso(first_value(vehicle_data, "lastCommunication")),
        "raw_json": json.dumps(vehicle, ensure_ascii=False),
    }


def trip_row(trip, vehicle_lookup):
    trip_id, source_trip_id = stable_trip_id(trip)
    vin = trip.get("vin")
    vehicle = vehicle_lookup.get(vin, {})

    start_time = normalize_iso(first_value(trip, "startTime", "start_time", "startedAt"))
    end_time = normalize_iso(first_value(trip, "endTime", "end_time", "endedAt"))
    duration_seconds = to_int(first_value(
        trip,
        "durationSeconds",
        "duration_seconds",
        "tripDurationSeconds",
        "trip_duration_seconds",
    ))

    if duration_seconds is None and start_time and end_time:
        duration_seconds = epoch_seconds(end_time) - epoch_seconds(start_time)

    distance_km = to_float(first_value(
        trip,
        "distanceKm",
        "distance_km",
        "tripDistanceKm",
        "trip_distance_km",
        "distance",
        "tripDistance",
    ))

    distance_meters = to_float(first_value(trip, "distanceMeters", "distance_meters"))
    if distance_km is None and distance_meters is not None:
        distance_km = distance_meters / 1000.0

    is_unfinished_trip = 1 if end_time is None else 0
    is_zero_value = 1 if duration_seconds == 0 or distance_km == 0 else 0
    is_negative_value = 1 if (
        (duration_seconds is not None and duration_seconds < 0)
        or (distance_km is not None and distance_km < 0)
    ) else 0
    is_ghost_trip = 1 if duration_seconds == 0 else 0
    is_timeout_trip = 1 if duration_seconds == TIMEOUT_SECONDS else 0
    is_long_trip = 1 if duration_seconds is not None and duration_seconds > LONG_TRIP_SECONDS else 0

    raw_status = first_value(trip, "status", "tripStatus", "state")
    teleportation_signal = first_value(trip, "isTeleportation", "teleportation", "is_teleportation")
    is_teleportation = 1 if str(teleportation_signal).lower() in ("1", "true", "yes") else 0

    reasons = []
    if is_ghost_trip:
        reasons.append("ghost_trip_duration_0")
    if is_timeout_trip:
        reasons.append("timeout_trip_duration_300")
    if is_long_trip:
        reasons.append("long_trip_over_2h")
    if is_unfinished_trip:
        reasons.append("unfinished_trip")
    if is_negative_value:
        reasons.append("negative_source_value")
    if is_teleportation:
        reasons.append("teleportation_flag_in_source")

    if is_ghost_trip:
        trip_category = "ghost"
    elif is_timeout_trip:
        trip_category = "timeout"
    elif is_long_trip:
        trip_category = "long"
    elif is_unfinished_trip:
        trip_category = "unfinished"
    elif is_negative_value:
        trip_category = "negative_value"
    else:
        trip_category = "normal"

    is_anomaly = 1 if reasons else 0

    return {
        "trip_id": trip_id,
        "source_trip_id": source_trip_id,
        "vin": vin,
        "vehicle_model": vehicle.get("vehicle_model"),
        "hardware_group": vehicle.get("hardware_group"),
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": duration_seconds,
        "distance_km": distance_km,
        "status": raw_status,
        "is_ghost_trip": is_ghost_trip,
        "is_timeout_trip": is_timeout_trip,
        "is_long_trip": is_long_trip,
        "is_negative_value": is_negative_value,
        "is_zero_value": is_zero_value,
        "is_unfinished_trip": is_unfinished_trip,
        "is_teleportation": is_teleportation,
        "is_anomaly": is_anomaly,
        "anomaly_reason": ", ".join(reasons) if reasons else None,
        "trip_category": trip_category,
        "raw_json": json.dumps(trip, ensure_ascii=False),
    }


def create_schema(cursor):
    cursor.execute("PRAGMA foreign_keys = ON;")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS raw_vehicles (
        vin TEXT PRIMARY KEY,
        raw_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS raw_trips (
        trip_id TEXT PRIMARY KEY,
        vin TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (vin) REFERENCES raw_vehicles(vin)
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vehicles (
        vin TEXT PRIMARY KEY,
        vehicle_model TEXT,
        license_plate TEXT,
        pairing_state TEXT,
        hardware_group TEXT,
        odometer REAL,
        fuel_level REAL,
        last_communication TEXT,
        raw_json TEXT NOT NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trips (
        trip_id TEXT PRIMARY KEY,
        source_trip_id TEXT,
        vin TEXT NOT NULL,
        vehicle_model TEXT,
        hardware_group TEXT,
        start_time TEXT,
        end_time TEXT,
        duration_seconds INTEGER,
        distance_km REAL,
        status TEXT,
        is_ghost_trip INTEGER NOT NULL DEFAULT 0,
        is_timeout_trip INTEGER NOT NULL DEFAULT 0,
        is_long_trip INTEGER NOT NULL DEFAULT 0,
        is_negative_value INTEGER NOT NULL DEFAULT 0,
        is_zero_value INTEGER NOT NULL DEFAULT 0,
        is_unfinished_trip INTEGER NOT NULL DEFAULT 0,
        is_teleportation INTEGER NOT NULL DEFAULT 0,
        is_anomaly INTEGER NOT NULL DEFAULT 0,
        anomaly_reason TEXT,
        trip_category TEXT,
        raw_json TEXT NOT NULL,
        FOREIGN KEY (vin) REFERENCES vehicles(vin)
    );
    """)

    for statement in [
        "CREATE INDEX IF NOT EXISTS idx_trips_vin ON trips(vin);",
        "CREATE INDEX IF NOT EXISTS idx_trips_start_time ON trips(start_time);",
        "CREATE INDEX IF NOT EXISTS idx_trips_category ON trips(trip_category);",
        "CREATE INDEX IF NOT EXISTS idx_trips_anomaly ON trips(is_anomaly);",
        "CREATE INDEX IF NOT EXISTS idx_trips_hardware ON trips(hardware_group);",
        "CREATE INDEX IF NOT EXISTS idx_raw_trips_vin ON raw_trips(vin);",
    ]:
        cursor.execute(statement)


def create_views(cursor):
    for view_name in [
        "v_trips_monitoring",
        "v_trip_duration_timeseries",
        "v_trip_distance_timeseries",
        "v_anomaly_timeseries",
        "v_hardware_comparison",
        "v_ghost_trips",
        "v_timeout_trips",
        "v_long_trips",
        "v_teleportation_events",
        "v_unfinished_trips",
        "v_trips_last_31_days",
        "v_negative_values",
    ]:
        cursor.execute(f"DROP VIEW IF EXISTS {view_name};")

    cursor.execute("""
    CREATE VIEW v_trips_monitoring AS
    SELECT
        t.trip_id,
        t.source_trip_id,
        t.vin,
        COALESCE(t.vehicle_model, v.vehicle_model) AS vehicle_model,
        COALESCE(t.hardware_group, v.hardware_group) AS hardware_group,
        t.start_time,
        t.end_time,
        t.duration_seconds,
        t.distance_km,
        t.status,
        t.is_ghost_trip,
        t.is_timeout_trip,
        t.is_long_trip,
        t.is_negative_value,
        t.is_zero_value,
        t.is_unfinished_trip,
        t.is_teleportation,
        t.is_anomaly,
        t.anomaly_reason,
        t.trip_category
    FROM trips t
    LEFT JOIN vehicles v ON v.vin = t.vin;
    """)

    cursor.execute("""
    CREATE VIEW v_trip_duration_timeseries AS
    SELECT
        start_time AS time,
        duration_seconds AS value,
        trip_category AS series,
        trip_id,
        vin,
        vehicle_model,
        trip_category,
        anomaly_reason
    FROM v_trips_monitoring
    WHERE start_time IS NOT NULL
      AND duration_seconds IS NOT NULL
    ORDER BY start_time;
    """)

    cursor.execute("""
    CREATE VIEW v_trip_distance_timeseries AS
    SELECT
        start_time AS time,
        distance_km AS value,
        trip_category AS series,
        trip_id,
        vin,
        vehicle_model,
        trip_category,
        anomaly_reason
    FROM v_trips_monitoring
    WHERE start_time IS NOT NULL
      AND distance_km IS NOT NULL
    ORDER BY start_time;
    """)

    cursor.execute("""
    CREATE VIEW v_anomaly_timeseries AS
    SELECT
        date(start_time) AS time,
        COUNT(*) AS value,
        'anomalies' AS series,
        NULL AS trip_id,
        NULL AS vin,
        NULL AS vehicle_model,
        NULL AS trip_category,
        GROUP_CONCAT(DISTINCT anomaly_reason) AS anomaly_reason
    FROM v_trips_monitoring
    WHERE start_time IS NOT NULL
      AND is_anomaly = 1
    GROUP BY date(start_time)
    ORDER BY time;
    """)

    cursor.execute("""
    CREATE VIEW v_hardware_comparison AS
    SELECT
        start_time AS time,
        duration_seconds AS value,
        COALESCE(hardware_group, 'unknown') AS series,
        trip_id,
        vin,
        vehicle_model,
        hardware_group,
        trip_category,
        anomaly_reason
    FROM v_trips_monitoring
    WHERE start_time IS NOT NULL
      AND duration_seconds IS NOT NULL
    ORDER BY start_time;
    """)

    cursor.execute("CREATE VIEW v_ghost_trips AS SELECT * FROM v_trips_monitoring WHERE is_ghost_trip = 1 ORDER BY start_time;")
    cursor.execute("CREATE VIEW v_timeout_trips AS SELECT * FROM v_trips_monitoring WHERE is_timeout_trip = 1 ORDER BY start_time;")
    cursor.execute("CREATE VIEW v_long_trips AS SELECT * FROM v_trips_monitoring WHERE is_long_trip = 1 ORDER BY start_time;")
    cursor.execute("CREATE VIEW v_teleportation_events AS SELECT * FROM v_trips_monitoring WHERE is_teleportation = 1 ORDER BY start_time;")
    cursor.execute("CREATE VIEW v_unfinished_trips AS SELECT * FROM v_trips_monitoring WHERE is_unfinished_trip = 1 ORDER BY start_time;")

    cursor.execute("""
    CREATE VIEW v_trips_last_31_days AS
    SELECT *
    FROM v_trips_monitoring
    WHERE start_time >= datetime('now', '-31 days')
    ORDER BY start_time;
    """)

    cursor.execute("""
    CREATE VIEW v_negative_values AS
    SELECT *
    FROM v_trips_monitoring
    WHERE is_negative_value = 1
    ORDER BY start_time;
    """)


def save_to_sqlite(vehicles, trips):
    db_path = os.getenv("SQLITE_DB_PATH", DEFAULT_DB_PATH)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()

    create_schema(cursor)

    vehicle_rows = []
    for vehicle in vehicles:
        row = vehicle_row(vehicle)
        if row["vin"]:
            vehicle_rows.append(row)

    vehicle_lookup = {row["vin"]: row for row in vehicle_rows}

    for row in vehicle_rows:
        cursor.execute("""
        INSERT INTO raw_vehicles (vin, raw_json)
        VALUES (?, ?)
        ON CONFLICT(vin) DO UPDATE SET
            raw_json = excluded.raw_json,
            fetched_at = datetime('now');
        """, (row["vin"], row["raw_json"]))

        cursor.execute("""
        INSERT INTO vehicles (
            vin, vehicle_model, license_plate, pairing_state, hardware_group,
            odometer, fuel_level, last_communication, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vin) DO UPDATE SET
            vehicle_model = excluded.vehicle_model,
            license_plate = excluded.license_plate,
            pairing_state = excluded.pairing_state,
            hardware_group = excluded.hardware_group,
            odometer = excluded.odometer,
            fuel_level = excluded.fuel_level,
            last_communication = excluded.last_communication,
            raw_json = excluded.raw_json;
        """, (
            row["vin"],
            row["vehicle_model"],
            row["license_plate"],
            row["pairing_state"],
            row["hardware_group"],
            row["odometer"],
            row["fuel_level"],
            row["last_communication"],
            row["raw_json"],
        ))

    saved_trips = 0
    for trip in trips:
        row = trip_row(trip, vehicle_lookup)
        if not row["trip_id"] or not row["vin"]:
            continue

        cursor.execute("""
        INSERT INTO raw_trips (trip_id, vin, raw_json)
        VALUES (?, ?, ?)
        ON CONFLICT(trip_id) DO UPDATE SET
            vin = excluded.vin,
            raw_json = excluded.raw_json,
            fetched_at = datetime('now');
        """, (row["trip_id"], row["vin"], row["raw_json"]))

        cursor.execute("""
        INSERT INTO trips (
            trip_id, source_trip_id, vin, vehicle_model, hardware_group,
            start_time, end_time, duration_seconds, distance_km, status,
            is_ghost_trip, is_timeout_trip, is_long_trip, is_negative_value,
            is_zero_value, is_unfinished_trip, is_teleportation, is_anomaly,
            anomaly_reason, trip_category, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trip_id) DO UPDATE SET
            source_trip_id = excluded.source_trip_id,
            vin = excluded.vin,
            vehicle_model = excluded.vehicle_model,
            hardware_group = excluded.hardware_group,
            start_time = excluded.start_time,
            end_time = excluded.end_time,
            duration_seconds = excluded.duration_seconds,
            distance_km = excluded.distance_km,
            status = excluded.status,
            is_ghost_trip = excluded.is_ghost_trip,
            is_timeout_trip = excluded.is_timeout_trip,
            is_long_trip = excluded.is_long_trip,
            is_negative_value = excluded.is_negative_value,
            is_zero_value = excluded.is_zero_value,
            is_unfinished_trip = excluded.is_unfinished_trip,
            is_teleportation = excluded.is_teleportation,
            is_anomaly = excluded.is_anomaly,
            anomaly_reason = excluded.anomaly_reason,
            trip_category = excluded.trip_category,
            raw_json = excluded.raw_json;
        """, (
            row["trip_id"],
            row["source_trip_id"],
            row["vin"],
            row["vehicle_model"],
            row["hardware_group"],
            row["start_time"],
            row["end_time"],
            row["duration_seconds"],
            row["distance_km"],
            row["status"],
            row["is_ghost_trip"],
            row["is_timeout_trip"],
            row["is_long_trip"],
            row["is_negative_value"],
            row["is_zero_value"],
            row["is_unfinished_trip"],
            row["is_teleportation"],
            row["is_anomaly"],
            row["anomaly_reason"],
            row["trip_category"],
            row["raw_json"],
        ))
        saved_trips += 1

    create_views(cursor)
    conn.commit()
    conn.close()

    return db_path, len(vehicle_rows), saved_trips


def main():
    vehicle_data = get_vehicles()
    trip_data = get_trip_data(vehicle_data)
    db_file, vehicle_count, trip_count = save_to_sqlite(vehicle_data, trip_data)

    print("\n==============================")
    print(f"SQLite DB aktualisiert: {db_file}")
    print(f"Vehicles gespeichert/aktualisiert: {vehicle_count}")
    print(f"Trips gespeichert/aktualisiert: {trip_count}")
    print("Views erstellt:")
    print("  - v_trips_monitoring")
    print("  - v_trip_duration_timeseries")
    print("  - v_trip_distance_timeseries")
    print("  - v_anomaly_timeseries")
    print("  - v_hardware_comparison")
    print("  - v_ghost_trips")
    print("  - v_timeout_trips")
    print("  - v_long_trips")
    print("  - v_teleportation_events")
    print("  - v_unfinished_trips")
    print("  - v_trips_last_31_days")
    print("  - v_negative_values")


if __name__ == "__main__":
    main()
