import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.cartelsol.mosdon-dev.com"
TOKEN_URL = "https://auth.cartelsol.mosdon-dev.com/oauth2/token"

def _headers():
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json"
    }

def refresh_access_token():
    """
    Refresh über Auth-Service (OAuth2 Token Endpoint):
    POST https://auth.cartelsol.mosdon-dev.com/oauth2/token
    Body (form-urlencoded):
      grant_type=refresh_token
      refresh_token=...
    """
    global ACCESS_TOKEN, REFRESH_TOKEN

    data = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID
    }

    r = requests.post(
        TOKEN_URL,
        data=data,  # form-urlencoded ist Standard für /oauth2/token
        headers={"Accept": "application/json"},
        timeout=30
    )
    print(f"POST {TOKEN_URL} -> {r.status_code}")
    r.raise_for_status()

    resp = r.json() if r.content else {}

    if "access_token" not in resp:
        raise RuntimeError(f"Refresh ok, aber access_token fehlt. Keys: {list(resp.keys())}")

    ACCESS_TOKEN = resp["access_token"]
    # refresh_token kommt nicht immer neu zurück -> alten behalten
    REFRESH_TOKEN = resp.get("refresh_token", REFRESH_TOKEN)

    print("✅ Neuer access_token geholt")

def get_json(url: str):
    r = requests.get(url, headers=_headers(), timeout=30)
    print(f"GET {url} -> {r.status_code}")

    if r.status_code == 401:
        print("401 Unauthorized -> refreshe Access Token und retry...")
        refresh_access_token()
        r = requests.get(url, headers=_headers(), timeout=30)
        print(f"GET (retry) {url} -> {r.status_code}")

    r.raise_for_status()
    return r.json()


def get_all_from_endpoint(url: str):
    
    first = get_json(url)

    if isinstance(first, list):
        return first

    items = []
    if isinstance(first, dict) and isinstance(first.get("items"), list):
        items.extend(first.get("items"))
        next_url = first.get("next") or (first.get("links") and first.get("links").get("next"))
        while next_url:
            page = get_json(next_url)
            if isinstance(page, dict) and isinstance(page.get("items"), list):
                items.extend(page.get("items"))
                next_url = page.get("next") or (page.get("links") and page.get("links").get("next"))
            else:
                break
        return items

    if isinstance(first, dict):
        all_items = []
        limit = 200
        offset = 0
        while True:
            paged_url = f"{url}?limit={limit}&offset={offset}"
            page = get_json(paged_url)
            if isinstance(page, dict) and isinstance(page.get("items"), list):
                page_items = page.get("items")
            elif isinstance(page, list):
                page_items = page
            else:
                break

            if not page_items:
                break
            all_items.extend(page_items)
            if len(page_items) < limit:
                break
            offset += limit

        if all_items:
            return all_items

    if isinstance(first, dict):
        return [first]

    return []

#Normaliserung Zeitangaben: ISO-8601 -> datetime UTC, epoch seconds, Starttag, Wochentag, Monat
    def _iso_to_dt(value):
    """ISO-8601 -> datetime UTC. Akzeptiert '...Z'. 'trip not finished' => None."""
    if not value:
        return None
    if isinstance(value, str) and value.strip().lower() == "trip not finished":
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def _to_epoch_seconds(dt):
    return int(dt.timestamp()) if dt else None

def _start_day(dt):
    return dt.date().isoformat() if dt else None

def _weekday_mon0(dt):
    return dt.weekday() if dt else None  # Monday=0..Sunday=6

def _month_yyyy_mm(dt):
    return f"{dt.year:04d}-{dt.month:02d}" if dt else None

# Alle Fahrzeuge holen 

    vehicles_data = get_json(f"{BASE_URL}/vehicles")

if isinstance(vehicles_data, dict) and isinstance(vehicles_data.get("items"), list):
    vehicles = vehicles_data["items"]
elif isinstance(vehicles_data, list):
    vehicles = vehicles_data
else:
    raise ValueError("Unerwartetes Format von /vehicles")

#xtraktion der Vins

vins = [v["vin"] for v in vehicles if "vin" in v]
vins = list(dict.fromkeys(vins))

if not vins:
    raise ValueError("Keine VINs im Vehicles-Response gefunden")

print(f"Fahrzeuge: {len(vehicles)} | VINs: {len(vins)}")

#Fahrzeugdaten nach Vin zuordnen

vin_to_model = {}
vin_to_vehicle_data = {}
vin_to_vehicle_info = {}

for v in vehicles:
    vin_key = v.get("vin")
    model = v.get("model")  
    if vin_key:
        vin_to_model[vin_key] = model

        vehicle_data = v.get("vehicleData") if isinstance(v.get("vehicleData"), dict) else {}
        vin_to_vehicle_data[vin_key] = {
            "pairing_state": v.get("pairingState"),
            "fuel_level": vehicle_data.get("fuelLevel"),
            "odometer": vehicle_data.get("odometer"),
            "last_communication": vehicle_data.get("lastCommunication")
        }

        vin_to_vehicle_info[vin_key] = {
            "vin": v.get("vin"),
            "model": v.get("model"),
            "pairing_state": v.get("pairingState"),
            "mosdon_id": v.get("mosdonId"),
            "license_plate": v.get("licensePlate"),
            "vehicle_data": vehicle_data,
            "current_fleet": v.get("currentFleet"),
            "current_technicians": v.get("currentTechnicians"),
            "diagnosis": v.get("diagnosis"),
            "raw_vehicle": v
        }

        # Trips für jede Vin holen

 all_trips = []

for i, vin in enumerate(vins, start=1):
    trips_data = get_json(f"{BASE_URL}/trips/vehicle/{vin}")

    if isinstance(trips_data, dict) and isinstance(trips_data.get("items"), list):
        trips = trips_data["items"]
    elif isinstance(trips_data, list):
        trips = trips_data
    else:
        trips = [trips_data]

    print(f"[{i}/{len(vins)}] VIN={vin} | Trips={len(trips)}")

    for trip in trips:
        if not isinstance(trip, dict):
            continue

        trip["vin"] = vin
        trip["vehicle_model"] = vin_to_model.get(vin)

        vehicle_data = vin_to_vehicle_data.get(vin, {})
        trip["pairing_state"] = vehicle_data.get("pairing_state")
        trip["fuel_level"] = vehicle_data.get("fuel_level")
        trip["odometer"] = vehicle_data.get("odometer")
        trip["last_communication"] = vehicle_data.get("last_communication")

        trip["vehicle_info"] = vin_to_vehicle_info.get(vin, {})

        #normalisierung der timesstamps für die trips
        start_time = trip.get("startTime")
        end_time = trip.get("endTime")

        start_dt = _iso_to_dt(start_time)
        end_dt = _iso_to_dt(end_time)

        start_ts = _to_epoch_seconds(start_dt)
        end_ts = _to_epoch_seconds(end_dt)

        is_finished = 1 if end_ts is not None else 0

        #unfinished trips sollen sichtbar sein

        trip["start_ts"] = start_ts
        trip["end_ts"] = end_ts
        trip["is_finished"] = is_finished
        trip["start_day"] = _start_day(start_dt)
        trip["start_weekday"] = _weekday_mon0(start_dt)
        trip["start_month"] = _month_yyyy_mm(start_dt)


        #Tripdauer soll angezeigt werden nur wenn es ein finished ist sonst unfinshed 

        if start_ts is not None and end_ts is not None:
            duration_seconds = max(0, int(end_ts - start_ts))
            trip["trip_duration_seconds"] = duration_seconds
            trip["trip_duration_minutes"] = round(duration_seconds / 60.0, 2)
        else:
            trip["trip_duration_seconds"] = None
            trip["trip_duration_minutes"] = None

        all_trips.append(trip)

        print("\n==============================")
        print(f"GESAMT Trips über alle Fahrzeuge: {len(all_trips)}")

        #sqlite speicherung der Trips und Vehicles

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        #erweiterung der Tabelle für die Speicherung der Fahrzeugdaten

        cur.execute("""
CREATE TABLE IF NOT EXISTS vehicles (
    vin TEXT PRIMARY KEY,
    model TEXT,
    pairing_state TEXT,
    mosdon_id TEXT,
    license_plate TEXT,
    odometer REAL,
    fuel_level REAL,
    last_communication TEXT,
    last_communication_ts INTEGER,
    current_fleet TEXT,
    current_technicians TEXT,
    diagnosis TEXT,
    raw_json TEXT
);
""")

# Columns sicherstellen (für bestehende DB)
for col_def in [
    "model TEXT",
    "pairing_state TEXT",
    "mosdon_id TEXT",
    "license_plate TEXT",
    "odometer REAL",
    "fuel_level REAL",
    "last_communication TEXT",
    "last_communication_ts INTEGER",
    "current_fleet TEXT",
    "current_technicians TEXT",
    "diagnosis TEXT",
    "raw_json TEXT",
]:
    try:
        cur.execute(f"ALTER TABLE vehicles ADD COLUMN {col_def};")
    except sqlite3.OperationalError:
        pass

cur.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_model ON vehicles(model);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_pairing_state ON vehicles(pairing_state);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_last_comm_ts ON vehicles(last_communication_ts);")

for v in vehicles:
    vin_key = v.get("vin")
    if not vin_key:
        continue

    model = v.get("model")
    pairing_state = v.get("pairingState")
    mosdon_id = v.get("mosdonId")
    license_plate = v.get("licensePlate")

    vehicle_data = v.get("vehicleData") if isinstance(v.get("vehicleData"), dict) else {}
    odometer = vehicle_data.get("odometer")
    fuel_level = vehicle_data.get("fuelLevel")
    last_communication = vehicle_data.get("lastCommunication")
    last_comm_ts = _to_epoch_seconds(_iso_to_dt(last_communication))

    current_fleet = v.get("currentFleet")
    if isinstance(current_fleet, dict):
        current_fleet = current_fleet.get("name")

    current_technicians = v.get("currentTechnicians")
    if isinstance(current_technicians, (dict, list)):
        current_technicians = json.dumps(current_technicians, ensure_ascii=False)

    diagnosis = v.get("diagnosis")
    if isinstance(diagnosis, (dict, list)):
        diagnosis = json.dumps(diagnosis, ensure_ascii=False)

    cur.execute("""
    INSERT INTO vehicles (
        vin, model, pairing_state, mosdon_id, license_plate,
        odometer, fuel_level, last_communication, last_communication_ts,
        current_fleet, current_technicians, diagnosis, raw_json
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(vin) DO UPDATE SET
        model = excluded.model,
        pairing_state = excluded.pairing_state,
        mosdon_id = excluded.mosdon_id,
        license_plate = excluded.license_plate,
        odometer = excluded.odometer,
        fuel_level = excluded.fuel_level,
        last_communication = excluded.last_communication,
        last_communication_ts = excluded.last_communication_ts,
        current_fleet = excluded.current_fleet,
        current_technicians = excluded.current_technicians,
        diagnosis = excluded.diagnosis,
        raw_json = excluded.raw_json;
    """, (
        vin_key, model, pairing_state, mosdon_id, license_plate,
        odometer, fuel_level, last_communication, last_comm_ts,
        current_fleet, current_technicians, diagnosis,
        json.dumps(v, ensure_ascii=False)
    ))

    # trips tabellen felder nromalisiert

    cur.execute("""
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY,
    vin TEXT NOT NULL,
    vehicle_model TEXT,
    vehicle_label TEXT,
    start_time TEXT,
    end_time TEXT,
    trip_duration_seconds INTEGER,
    trip_duration_minutes REAL,

    -- Normalisierung für Grafana
    start_ts INTEGER,
    end_ts INTEGER,
    is_finished INTEGER,
    start_day TEXT,
    start_weekday INTEGER,
    start_month TEXT,

    raw_json TEXT
);
""")

# columns sicherstellen
for col_def in [
    "vehicle_model TEXT",
    "vehicle_label TEXT",
    "trip_duration_seconds INTEGER",
    "trip_duration_minutes REAL",
    "start_ts INTEGER",
    "end_ts INTEGER",
    "is_finished INTEGER",
    "start_day TEXT",
    "start_weekday INTEGER",
    "start_month TEXT",
    "raw_json TEXT",
]:
    try:
        cur.execute(f"ALTER TABLE trips ADD COLUMN {col_def};")
    except sqlite3.OperationalError:
        pass

cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_vin ON trips(vin);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_start_ts ON trips(start_ts);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_finished ON trips(is_finished);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_vehicle_model ON trips(vehicle_model);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_start_day ON trips(start_day);")

inserted = 0
for t in all_trips:
    if not isinstance(t, dict):
        continue

    trip_id = t.get("id")
    vin = t.get("vin")

    start_time = t.get("startTime")

    #  Tripdaten mit Fahrzeugmodell und Zeitkennzahlen speichern

    end_time = t.get("endTime") or "trip not finished"

    vehicle_model = t.get("vehicle_model") or vin_to_model.get(vin)
    vehicle_label = f"{vehicle_model} ({vin})" if vehicle_model else vin

    start_ts = t.get("start_ts")
    end_ts = t.get("end_ts")
    is_finished = t.get("is_finished", 0)

    start_day = t.get("start_day")
    start_weekday = t.get("start_weekday")
    start_month = t.get("start_month")

    trip_duration_seconds = t.get("trip_duration_seconds")
    trip_duration_minutes = t.get("trip_duration_minutes")

    if trip_id is None or vin is None:
        continue

    cur.execute("""
    INSERT INTO trips (
        id, vin, vehicle_model, vehicle_label,
        start_time, end_time,
        trip_duration_seconds, trip_duration_minutes,
        start_ts, end_ts, is_finished,
        start_day, start_weekday, start_month,
        raw_json
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        vin = excluded.vin,
        vehicle_model = excluded.vehicle_model,
        vehicle_label = excluded.vehicle_label,
        start_time = excluded.start_time,
        end_time = excluded.end_time,
        trip_duration_seconds = excluded.trip_duration_seconds,
        trip_duration_minutes = excluded.trip_duration_minutes,
        start_ts = excluded.start_ts,
        end_ts = excluded.end_ts,
        is_finished = excluded.is_finished,
        start_day = excluded.start_day,
        start_weekday = excluded.start_weekday,
        start_month = excluded.start_month,
        raw_json = excluded.raw_json;
    """, (
        trip_id, vin, vehicle_model, vehicle_label,
        start_time, end_time,
        trip_duration_seconds, trip_duration_minutes,
        start_ts, end_ts, is_finished,
        start_day, start_weekday, start_month,
        json.dumps(t, ensure_ascii=False)
    ))
    inserted += 1

    #Grafana Trip Views 
    cur.execute("""
    CREATE VIEW v_trips_monitoring AS
    SELECT
    (start_ts * 1000) AS time,
    vin,
    vehicle_model,
    vehicle_label,
    is_finished,
    trip_duration_minutes,
    start_day,
    start_weekday,
    start_month
    FROM trips
    WHERE start_ts IS NOT NULL;
    """)

    #unfinished Trips (Table laufzeit in min)
    cur.execute("""
    CREATE VIEW v_unfinished_trips AS
    SELECT
    (start_ts * 1000) AS time,
    vin,
    vehicle_model,
    vehicle_label,
    start_time,
    end_time,
    (strftime('%s','now') - start_ts) / 60.0 AS minutes_running
    FROM trips
    WHERE is_finished = 0
    AND start_ts IS NOT NULL
    ORDER BY start_ts DESC;
    """)
