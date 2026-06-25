import hashlib
import json
import os
import sqlite3
import time
import logging
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - klare Fehlermeldung statt kryptischem Traceback
    sys.stderr.write(
        "FEHLER: python-dotenv ist nicht installiert. "
        "Bitte ausfuehren: pip3 install python-dotenv requests\n"
    )
    raise

#Env variablen sollen überschreibar sein, daher zuerst laden und dann überschreiben

ENV_PATH = os.getenv("TRIPMON_ENV", "/etc/tripdaten-mon/tripdaten-mon.env")
DB_PATH = os.getenv("TRIPMON_DB", "/var/lib/grafana/sqlite/trips.db")
LOG_PATH = os.getenv("TRIPMON_LOG", "/var/log/tripdaten-mon/tripdaten-mon.log")

HTTP_TIMEOUT = 30          # Sekunden pro Request
HTTP_RETRIES = 3           # Versuche bei transienten Fehlern (Timeout/Connection/5xx)
HTTP_BACKOFF = 5           # Sekunden, multipliziert mit Versuchsnummer
SQLITE_TIMEOUT = 30        # Sekunden busy-timeout beim Verbinden


#Logging Datei für das Monitoring und terminal ausgabe

def _setup_logging():
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError as exc:
        sys.stderr.write(f"WARN: Logdatei {LOG_PATH} nicht beschreibbar: {exc}\n")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=handlers,
    )


log = logging.getLogger("tripdaten-mon")

# Speicherung aktualisierter Token in der Env-Datei hinzufügen
def save_dotenv_values(path, updates):

lines = []
    seen = set()

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}\n")

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    for key, value in updates.items():
        os.environ[key] = value

#Token Verwaltung 


class TokenRefreshError(RuntimeError):
    pass


class TokenManager:
    def __init__(self, token_url, client_id, client_secret=None, env_file=ENV_PATH,
                 base_url=None):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.env_file = env_file
        self.base_url = base_url
        self.access_token = None
        self.refresh_token = None
        self._lock = threading.Lock()

    @staticmethod
    def _clean_token(value):
        if not value or value.startswith("HIER_"):
            return None
        return value

    def load_tokens(self):
        self.access_token = self._clean_token(os.getenv("ACCESS_TOKEN"))
        self.refresh_token = self._clean_token(os.getenv("REFRESH_TOKEN"))

        if not self.refresh_token:
            raise RuntimeError(
                f"REFRESH_TOKEN fehlt in {self.env_file}. Bitte eintragen."
            )
        if not self.access_token:
            log.info("Kein Access-Token vorhanden -> wird per Refresh erzeugt")
            self.refresh_access_token()

    def save_tokens(self):
        """Persistiert Tokens. Bei Fehler: CRITICAL + Exception (kein stiller Lockout)."""
        try:
            save_dotenv_values(self.env_file, {
                "BASE_URL": self.base_url,
                "TOKEN_URL": self.token_url,
                "CLIENT_ID": self.client_id,
                "ACCESS_TOKEN": self.access_token,
                "REFRESH_TOKEN": self.refresh_token,
            })
            log.info("Env-Datei aktualisiert (Tokens gespeichert)")
        except OSError as exc:
            log.critical(
                "TOKEN-PERSISTENZ FEHLGESCHLAGEN beim Schreiben von %s: %s. "
                "Falls der Server den Refresh-Token rotiert, ist der neue Token jetzt "
                "verloren -> moeglicher dauerhafter Lockout. Sofort pruefen!",
                self.env_file, exc,
            )
            raise TokenRefreshError(
                f"Tokens konnten nicht in {self.env_file} gespeichert werden: {exc}"
            ) from exc

    def get_access_token(self):
        if not self.access_token:
            self.refresh_access_token()
        return self.access_token

    def refresh_access_token(self):
        with self._lock:
            if not self.client_id:
                raise RuntimeError("CLIENT_ID fehlt.")
            if not self.refresh_token:
                raise RuntimeError("REFRESH_TOKEN fehlt. Token-Refresh nicht moeglich.")

            data = {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }
            auth = None
            if self.client_secret:
                auth = (self.client_id, self.client_secret)
            else:
                data["client_id"] = self.client_id

            try:
                response = requests.post(
                    self.token_url,
                    data=data,
                    auth=auth,
                    headers={"Accept": "application/json"},
                    timeout=HTTP_TIMEOUT,
                )
                log.info("POST %s -> %s", self.token_url, response.status_code)
            except requests.exceptions.SSLError as exc:
                raise TokenRefreshError(
                    "Token-Refresh wegen TLS-Zertifikatsfehler fehlgeschlagen "
                    "(kein unsicherer Fallback verwendet)."
                ) from exc
            except requests.exceptions.Timeout as exc:
                raise TokenRefreshError("Token-Refresh wegen Timeout fehlgeschlagen.") from exc
            except requests.exceptions.ConnectionError as exc:
                raise TokenRefreshError("Token-Refresh wegen Verbindungsfehler fehlgeschlagen.") from exc
            except requests.exceptions.RequestException as exc:
                raise TokenRefreshError("Token-Refresh wegen Request-Fehler fehlgeschlagen.") from exc

            response_data = {}
            if response.content:
                try:
                    response_data = response.json()
                except ValueError:
                    response_data = {}

            if response.status_code >= 400:
                oauth_error = response_data.get("error", "unbekannt")
                description = response_data.get("error_description", "keine Beschreibung")
                # wenn token abgelaufen oder invalid ist wird selbst nachgetragen

             raise TokenRefreshError(
                    f"Token-Endpunkt Fehler: HTTP {response.status_code}, "
                    f"error={oauth_error}, description={description}"
                )

            new_access_token = response_data.get("access_token")
            if not new_access_token:
                raise TokenRefreshError(
                    f"Refresh ok, aber access_token fehlt. Felder: {list(response_data.keys())}"
                )

            self.access_token = new_access_token
            new_refresh_token = response_data.get("refresh_token")
            if new_refresh_token and new_refresh_token != self.refresh_token:
                self.refresh_token = new_refresh_token
                log.info("Neuer (rotierter) Refresh-Token uebernommen")

                #Sofort speichern, bei token rotation 
            self.save_tokens()
            log.info("Access-Token erneuert")


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

cur.execute("PRAGMA table_info(trips);")
trip_columns = [column[1] for column in cur.fetchall()]
if trip_columns and "id" not in trip_columns:
    legacy_trips_table = f"legacy_trips_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    cur.execute(f"ALTER TABLE trips RENAME TO {legacy_trips_table};")
    print(f"Alte trips Tabelle nach {legacy_trips_table} verschoben.")

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
cur.execute("DROP VIEW IF EXISTS v_trips_monitoring;")
cur.execute("DROP VIEW IF EXISTS v_unfinished_trips;")
cur.execute("DROP VIEW IF EXISTS v_vehicle_activity;")
cur.execute("DROP VIEW IF EXISTS v_trips_per_day_last_31d;")

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

# verhicle active/inactive view letze 30 min

cur.execute("""
    CREATE VIEW v_vehicle_activity AS
    SELECT
    pairing_state,
    CASE
    WHEN last_communication_ts IS NOT NULL
    AND last_communication_ts >= (strftime('%s','now') - 30*60)
    THEN 'aktiv'
    ELSE 'inaktiv'
    END AS aktiv_status,
    COUNT(*) AS fahrzeuge
    FROM vehicles
    GROUP BY pairing_state, aktiv_status
    ORDER BY pairing_state, aktiv_status;
    """) 

# trips pro tag letzte (31 Tage)

cur.execute("""
CREATE VIEW v_trips_per_day_last_31d AS
WITH RECURSIVE days(day) AS (
  SELECT date('now','-30 day')
  UNION ALL
  SELECT date(day,'+1 day') FROM days WHERE day < date('now')
),
agg AS (
  SELECT
    start_day AS day,
    COUNT(*) AS trips,
    SUM(CASE WHEN is_finished=0 THEN 1 ELSE 0 END) AS unfinished_trips
  FROM trips
  WHERE start_day >= date('now','-30 day')
  GROUP BY start_day
)
SELECT
  (strftime('%s', days.day) * 1000) AS time,
  days.day,
  COALESCE(agg.trips, 0) AS trips,
  COALESCE(agg.unfinished_trips, 0) AS unfinished_trips
FROM days
LEFT JOIN agg ON agg.day = days.day
ORDER BY days.day;
""")

conn.commit()
conn.close()

print("\n==============================")
print(f" SQLite DB aktualisiert: {db_path}")
print(f" Trips gespeichert/aktualisiert: {inserted}")
print(f" Vehicles gespeichert/aktualisiert: {len(vehicles)}")
print(" Views erstellt:")
print("  - v_trips_monitoring")
print("  - v_unfinished_trips")
print("  - v_vehicle_activity")
print("  - v_trips_per_day_last_31d")
