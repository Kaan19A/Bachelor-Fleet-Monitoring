import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import requests
import sqlite3

try:
    from dotenv import load_dotenv
except ImportError: 
    sys.stderr.write(
        "FEHLER: python-dotenv ist nicht installiert. "
        "Bitte ausfuehren: pip3 install python-dotenv requests\n"
    )
    raise



# Env-Variablen ueberschreibbar

ENV_PATH = os.getenv("TRIPMON_ENV", "/etc/tripdaten-mon/tripdaten-mon.env")
DB_PATH = os.getenv("TRIPMON_DB", "/var/lib/grafana/sqlite/trips.db")
LOG_PATH = os.getenv("TRIPMON_LOG", "/var/log/tripdaten-mon/tripdaten-mon.log")

HTTP_TIMEOUT = 30          # Sekunden pro Request
HTTP_RETRIES = 3           # Versuche bei transienten Fehlern (Timeout/Connection/5xx)
HTTP_BACKOFF = 5           # Sekunden, multipliziert mit Versuchsnummer
SQLITE_TIMEOUT = 30        # Sekunden busy-timeout beim Verbinden



# Logging Datei mit Zeitstempel

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



# Atomare Env-Datei schreiben 

def save_dotenv_values(path, updates):
    """Schreibt Schluessel/Werte atomar zurueck in die Env-Datei.

    Wirft bei Fehler eine Exception (NICHT schlucken!), damit ein verlorener
    rotierter Refresh-Token sofort auffaellt und der Lauf hart abbricht.
    """
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


# Token-Verwaltung

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
                # invalid_grant => Refresh-Token selbst ist tot/abgelaufen -> manueller Eingriff
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

            # Sofort persistieren  bei Rotation 
            self.save_tokens()
            log.info("Access-Token erneuert")


# HTTP error Handling

def _is_transient_status(status):
    return status in (429, 500, 502, 503, 504)


def get_json(url, token_manager, _already_refreshed=False):
    last_exc = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            headers = {
                "Authorization": f"Bearer {token_manager.get_access_token()}",
                "Accept": "application/json",
            }
            response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            log.info("GET %s -> %s (Versuch %d)", url, response.status_code, attempt)

            if response.status_code == 401 and not _already_refreshed:
                log.warning("401 Unauthorized -> Access-Token wird einmal erneuert")
                token_manager.refresh_access_token()
                return get_json(url, token_manager, _already_refreshed=True)

            if _is_transient_status(response.status_code):
                last_exc = RuntimeError(f"HTTP {response.status_code} bei {url}")
                raise last_exc

            response.raise_for_status()
            return response.json()

        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError) as exc:
            last_exc = exc
            log.warning("Netzwerkfehler bei %s (Versuch %d/%d): %s",
                        url, attempt, HTTP_RETRIES, exc)
        except RuntimeError as exc:  # transienter HTTP-Status
            last_exc = exc
            log.warning("Transienter Fehler bei %s (Versuch %d/%d): %s",
                        url, attempt, HTTP_RETRIES, exc)

        if attempt < HTTP_RETRIES:
            time.sleep(HTTP_BACKOFF * attempt)

    raise RuntimeError(f"Endgueltig fehlgeschlagen nach {HTTP_RETRIES} Versuchen: {url}") from last_exc


def get_all_from_endpoint(url, token_manager):
    """Holt alle Items eines (ggf. paginierten) Endpoints.

    Unterstuetzt: direkte Liste, {"items":[...]}, 'next'-Link, limit/offset.
    """
    first = get_json(url, token_manager)

    if isinstance(first, list):
        return first

    if isinstance(first, dict) and isinstance(first.get("items"), list):
        items = list(first["items"])
        next_url = first.get("next") or (first.get("links") or {}).get("next")
        while next_url:
            page = get_json(next_url, token_manager)
            if isinstance(page, dict) and isinstance(page.get("items"), list):
                items.extend(page["items"])
                next_url = page.get("next") or (page.get("links") or {}).get("next")
            else:
                break
        # Falls keine next-Links: per limit/offset weiterblaettern
        if not (first.get("next") or (first.get("links") or {}).get("next")):
            limit, offset = 200, len(items)
            while len(first["items"]) >= 200:  # nur wenn erste Seite "voll" war
                paged = get_json(f"{url}?limit={limit}&offset={offset}", token_manager)
                page_items = paged.get("items") if isinstance(paged, dict) else (
                    paged if isinstance(paged, list) else [])
                if not page_items:
                    break
                items.extend(page_items)
                if len(page_items) < limit:
                    break
                offset += limit
        return items

    if isinstance(first, dict):
        return [first]
    return []


# Normalisierungs-Helfer fuer Grafana

def _iso_to_dt(value):
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
    return dt.weekday() if dt else None


def _month_yyyy_mm(dt):
    return f"{dt.year:04d}-{dt.month:02d}" if dt else None



# Datenabruf

def fetch_vehicles(base_url, token_manager):
    data = get_all_from_endpoint(f"{base_url}/vehicles", token_manager)
    if not isinstance(data, list):
        raise ValueError("Unerwartetes Format von /vehicles")
    return data


def build_vehicle_maps(vehicles):
    vin_to_model, vin_to_vehicle_data = {}, {}
    for v in vehicles:
        vin_key = v.get("vin")
        if not vin_key:
            continue
        vin_to_model[vin_key] = v.get("model")
        vd = v.get("vehicleData") if isinstance(v.get("vehicleData"), dict) else {}
        vin_to_vehicle_data[vin_key] = {
            "pairing_state": v.get("pairingState"),
            "fuel_level": vd.get("fuelLevel"),
            "odometer": vd.get("odometer"),
            "last_communication": vd.get("lastCommunication"),
        }
    return vin_to_model, vin_to_vehicle_data


def fetch_all_trips(base_url, vins, vin_to_model, token_manager):
    """Holt Trips je VIN. Fehlerhafte VINs werden uebersprungen, nicht abgebrochen."""
    all_trips = []
    failed_vins = []
    for i, vin in enumerate(vins, start=1):
        try:
            trips_data = get_json(f"{base_url}/trips/vehicle/{vin}", token_manager)
        except Exception as exc:
            failed_vins.append(vin)
            log.error("[%d/%d] VIN=%s uebersprungen wegen Fehler: %s",
                      i, len(vins), vin, exc)
            continue

        if isinstance(trips_data, dict) and isinstance(trips_data.get("items"), list):
            trips = trips_data["items"]
        elif isinstance(trips_data, list):
            trips = trips_data
        else:
            trips = [trips_data]

        log.info("[%d/%d] VIN=%s | Trips=%d", i, len(vins), vin, len(trips))

        for trip in trips:
            if not isinstance(trip, dict):
                continue
            trip["vin"] = vin
            trip["vehicle_model"] = vin_to_model.get(vin)

            start_dt = _iso_to_dt(trip.get("startTime"))
            end_dt = _iso_to_dt(trip.get("endTime"))
            start_ts = _to_epoch_seconds(start_dt)
            end_ts = _to_epoch_seconds(end_dt)

            trip["start_ts"] = start_ts
            trip["end_ts"] = end_ts
            trip["is_finished"] = 1 if end_ts is not None else 0
            trip["start_day"] = _start_day(start_dt)
            trip["start_weekday"] = _weekday_mon0(start_dt)
            trip["start_month"] = _month_yyyy_mm(start_dt)

            if start_ts is not None and end_ts is not None:
                dur = max(0, int(end_ts - start_ts))
                trip["trip_duration_seconds"] = dur
                trip["trip_duration_minutes"] = round(dur / 60.0, 2)
            else:
                trip["trip_duration_seconds"] = None
                trip["trip_duration_minutes"] = None

            all_trips.append(trip)

    if failed_vins:
        log.warning("%d/%d VINs konnten nicht abgerufen werden: %s",
                    len(failed_vins), len(vins), ", ".join(failed_vins))
    return all_trips, failed_vins


# Json datei: baut je VIN ein angereichertes vehicle_info-Objekt 
def _build_vehicle_info_map(vehicles):
    info_map, flat_map = {}, {}
    for v in vehicles:
        vin_key = v.get("vin")
        if not vin_key:
            continue
        vd = v.get("vehicleData") if isinstance(v.get("vehicleData"), dict) else {}
        current_fleet = v.get("currentFleet")
        if isinstance(current_fleet, dict):
            current_fleet = current_fleet.get("name")
        info_map[vin_key] = {
            "vin": vin_key,
            "model": v.get("model"),
            "pairing_state": v.get("pairingState"),
            "mosdon_id": v.get("mosdonId"),
            "license_plate": v.get("licensePlate"),
            "vehicle_data": vd,
            "current_fleet": current_fleet,
            "current_technicians": v.get("currentTechnicians"),
            "diagnosis": v.get("diagnosis"),
            "raw_vehicle": v,
        }
        flat_map[vin_key] = {
            "pairing_state": v.get("pairingState"),
            "fuel_level": vd.get("fuelLevel"),
            "odometer": vd.get("odometer"),
            "last_communication": vd.get("lastCommunication"),
        }
    return info_map, flat_map


def export_trips_json(path, all_trips, vehicles, vin_to_model):
    """Schreibt einen angereicherten JSON-Export der Trips
    """
    info_map, flat_map = _build_vehicle_info_map(vehicles)
    export = []
    for t in all_trips:
        vin = t.get("vin")
        flat = flat_map.get(vin, {})
        export.append({
            "id": t.get("id"),
            "vin": vin,
            "startTime": t.get("startTime"),
            "endTime": t.get("endTime"),
            "vehicle_model": t.get("vehicle_model") or vin_to_model.get(vin),
            "pairing_state": flat.get("pairing_state"),
            "fuel_level": flat.get("fuel_level"),
            "odometer": flat.get("odometer"),
            "last_communication": flat.get("last_communication"),
            "vehicle_info": info_map.get(vin),
            "start_ts": t.get("start_ts"),
            "end_ts": t.get("end_ts"),
            "is_finished": t.get("is_finished"),
            "start_day": t.get("start_day"),
            "start_weekday": t.get("start_weekday"),
            "start_month": t.get("start_month"),
            "trip_duration_seconds": t.get("trip_duration_seconds"),
            "trip_duration_minutes": t.get("trip_duration_minutes"),
        })

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return len(export)


# SQLite-Speicherung 

def open_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT)
    conn.execute("PRAGMA journal_mode=WAL;")       # gleichzeitiges Lesen (Grafana) + Schreiben
    conn.execute("PRAGMA synchronous=NORMAL;")     # WAL-konform, schnell, sicher genug
    conn.execute("PRAGMA busy_timeout=30000;")     # 30s warten statt sofort "locked"
    return conn


def _ensure_columns(cur, table, col_defs):
    for col_def in col_defs:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_def};")
        except sqlite3.OperationalError:
            pass


def save_vehicles(cur, vehicles):
    cur.execute("""
    CREATE TABLE IF NOT EXISTS vehicles (
        vin TEXT PRIMARY KEY, model TEXT, pairing_state TEXT, mosdon_id TEXT,
        license_plate TEXT, odometer REAL, fuel_level REAL, last_communication TEXT,
        last_communication_ts INTEGER, current_fleet TEXT, current_technicians TEXT,
        diagnosis TEXT, raw_json TEXT
    );""")
    _ensure_columns(cur, "vehicles", [
        "model TEXT", "pairing_state TEXT", "mosdon_id TEXT", "license_plate TEXT",
        "odometer REAL", "fuel_level REAL", "last_communication TEXT",
        "last_communication_ts INTEGER", "current_fleet TEXT",
        "current_technicians TEXT", "diagnosis TEXT", "raw_json TEXT",
    ])
    cur.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_model ON vehicles(model);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_pairing_state ON vehicles(pairing_state);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_last_comm_ts ON vehicles(last_communication_ts);")

    count = 0
    for v in vehicles:
        vin_key = v.get("vin")
        if not vin_key:
            continue
        vd = v.get("vehicleData") if isinstance(v.get("vehicleData"), dict) else {}
        last_comm = vd.get("lastCommunication")
        current_fleet = v.get("currentFleet")
        if isinstance(current_fleet, dict):
            current_fleet = current_fleet.get("name")
        current_techs = v.get("currentTechnicians")
        if isinstance(current_techs, (dict, list)):
            current_techs = json.dumps(current_techs, ensure_ascii=False)
        diagnosis = v.get("diagnosis")
        if isinstance(diagnosis, (dict, list)):
            diagnosis = json.dumps(diagnosis, ensure_ascii=False)

        cur.execute("""
        INSERT INTO vehicles (
            vin, model, pairing_state, mosdon_id, license_plate, odometer, fuel_level,
            last_communication, last_communication_ts, current_fleet,
            current_technicians, diagnosis, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vin) DO UPDATE SET
            model=excluded.model, pairing_state=excluded.pairing_state,
            mosdon_id=excluded.mosdon_id, license_plate=excluded.license_plate,
            odometer=excluded.odometer, fuel_level=excluded.fuel_level,
            last_communication=excluded.last_communication,
            last_communication_ts=excluded.last_communication_ts,
            current_fleet=excluded.current_fleet,
            current_technicians=excluded.current_technicians,
            diagnosis=excluded.diagnosis, raw_json=excluded.raw_json;
        """, (
            vin_key, v.get("model"), v.get("pairingState"), v.get("mosdonId"),
            v.get("licensePlate"), vd.get("odometer"), vd.get("fuelLevel"),
            last_comm, _to_epoch_seconds(_iso_to_dt(last_comm)),
            current_fleet, current_techs, diagnosis,
            json.dumps(v, ensure_ascii=False),
        ))
        count += 1
    return count


def save_trips(cur, all_trips, vin_to_model):
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trips (
        id INTEGER PRIMARY KEY, vin TEXT NOT NULL, vehicle_model TEXT,
        vehicle_label TEXT, start_time TEXT, end_time TEXT,
        trip_duration_seconds INTEGER, trip_duration_minutes REAL,
        start_ts INTEGER, end_ts INTEGER, is_finished INTEGER,
        start_day TEXT, start_weekday INTEGER, start_month TEXT, raw_json TEXT
    );""")
    _ensure_columns(cur, "trips", [
        "vehicle_model TEXT", "vehicle_label TEXT", "trip_duration_seconds INTEGER",
        "trip_duration_minutes REAL", "start_ts INTEGER", "end_ts INTEGER",
        "is_finished INTEGER", "start_day TEXT", "start_weekday INTEGER",
        "start_month TEXT", "raw_json TEXT",
    ])
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_vin ON trips(vin);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_start_ts ON trips(start_ts);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_finished ON trips(is_finished);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_vehicle_model ON trips(vehicle_model);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trips_start_day ON trips(start_day);")

    inserted = 0
    for t in all_trips:
        trip_id = t.get("id")
        vin = t.get("vin")
        if trip_id is None or vin is None:
            continue
        vehicle_model = t.get("vehicle_model") or vin_to_model.get(vin)
        vehicle_label = f"{vehicle_model} ({vin})" if vehicle_model else vin
        cur.execute("""
        INSERT INTO trips (
            id, vin, vehicle_model, vehicle_label, start_time, end_time,
            trip_duration_seconds, trip_duration_minutes, start_ts, end_ts,
            is_finished, start_day, start_weekday, start_month, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            vin=excluded.vin, vehicle_model=excluded.vehicle_model,
            vehicle_label=excluded.vehicle_label, start_time=excluded.start_time,
            end_time=excluded.end_time,
            trip_duration_seconds=excluded.trip_duration_seconds,
            trip_duration_minutes=excluded.trip_duration_minutes,
            start_ts=excluded.start_ts, end_ts=excluded.end_ts,
            is_finished=excluded.is_finished, start_day=excluded.start_day,
            start_weekday=excluded.start_weekday, start_month=excluded.start_month,
            raw_json=excluded.raw_json;
        """, (
            trip_id, vin, vehicle_model, vehicle_label,
            t.get("startTime"), t.get("endTime") or "trip not finished",
            t.get("trip_duration_seconds"), t.get("trip_duration_minutes"),
            t.get("start_ts"), t.get("end_ts"), t.get("is_finished", 0),
            t.get("start_day"), t.get("start_weekday"), t.get("start_month"),
            json.dumps(t, ensure_ascii=False),
        ))
        inserted += 1
    return inserted


def create_views(cur):
    # Basis-Views
    for v in ("v_trips_monitoring", "v_unfinished_trips", "v_vehicle_activity",
              "v_trips_per_day_last_31d"):
        cur.execute(f"DROP VIEW IF EXISTS {v};")
    # Analyse-Views (haengen an v_trips_monitoring)
    for v in ("v_anomaly_timeseries", "v_ghost_trips", "v_hardware_comparison",
              "v_long_trips", "v_negative_values", "v_teleportation_events",
              "v_timeout_trips", "v_trip_distance_timeseries",
              "v_trip_duration_timeseries", "v_trips_last_31_days"):
        cur.execute(f"DROP VIEW IF EXISTS {v};")

    # Reiches v_trips_monitoring: Anomalie-Flags + Kategorie direkt aus trips berechnet.
    # WICHTIG: echte Dauer = (end_ts - start_ts), NICHT trip_duration_seconds
    # (das ist auf >=0 gekappt und wuerde negative Trips verbergen).
    # Regeln 1:1 aus der Legacy-Pipeline rekonstruiert (validiert: 0 Abweichungen):
    #   negativ:   dur < 0      -> 'negative_source_value'
    #   lang:      dur > 7200   -> 'long_trip_over_2h' (2h)
    #   unfinished: is_finished=0 -> 'unfinished_trip'
    #   is_anomaly = OR aller Flags ; distance_km/hardware_group lagen nie vor (NULL).
    cur.execute("""
    CREATE VIEW v_trips_monitoring AS
    SELECT
        (start_ts*1000) AS time,
        id AS trip_id,
        vin,
        vehicle_model,
        vehicle_label,
        start_time,
        end_time,
        is_finished,
        trip_duration_minutes,
        dur AS duration_seconds,
        NULL AS distance_km,
        NULL AS hardware_group,
        start_day,
        start_weekday,
        start_month,
        CASE WHEN dur < 0    THEN 1 ELSE 0 END AS is_negative_value,
        CASE WHEN dur = 0    THEN 1 ELSE 0 END AS is_zero_value,
        CASE WHEN dur > 7200 THEN 1 ELSE 0 END AS is_long_trip,
        CASE WHEN is_finished = 0 THEN 1 ELSE 0 END AS is_unfinished_trip,
        0 AS is_timeout_trip,
        0 AS is_teleportation,
        0 AS is_ghost_trip,
        CASE WHEN is_finished = 0 OR dur < 0 OR dur = 0 OR dur > 7200
             THEN 1 ELSE 0 END AS is_anomaly,
        CASE
            WHEN is_finished = 0 THEN 'unfinished_trip'
            WHEN dur > 7200      THEN 'long_trip_over_2h'
            WHEN dur < 0         THEN 'negative_source_value'
            ELSE NULL
        END AS anomaly_reason,
        CASE
            WHEN is_finished = 0 THEN 'unfinished'
            WHEN dur > 7200      THEN 'long'
            WHEN dur < 0         THEN 'negative_value'
            ELSE 'normal'
        END AS trip_category
    FROM (
        SELECT *, (end_ts - start_ts) AS dur
        FROM trips
        WHERE start_ts IS NOT NULL
    );""")

    cur.execute("""
    CREATE VIEW v_unfinished_trips AS
    SELECT (start_ts*1000) AS time, vin, vehicle_model, vehicle_label,
           start_time, end_time,
           (strftime('%s','now') - start_ts)/60.0 AS minutes_running
    FROM trips WHERE is_finished=0 AND start_ts IS NOT NULL
    ORDER BY start_ts DESC;""")

    cur.execute("""
    CREATE VIEW v_vehicle_activity AS
    SELECT pairing_state,
           CASE WHEN last_communication_ts IS NOT NULL
                 AND last_communication_ts >= (strftime('%s','now')-30*60)
                THEN 'aktiv' ELSE 'inaktiv' END AS aktiv_status,
           COUNT(*) AS fahrzeuge
    FROM vehicles GROUP BY pairing_state, aktiv_status
    ORDER BY pairing_state, aktiv_status;""")

    cur.execute("""
    CREATE VIEW v_trips_per_day_last_31d AS
    WITH RECURSIVE days(day) AS (
        SELECT date('now','-30 day')
        UNION ALL SELECT date(day,'+1 day') FROM days WHERE day < date('now')
    ), agg AS (
        SELECT start_day AS day, COUNT(*) AS trips,
               SUM(CASE WHEN is_finished=0 THEN 1 ELSE 0 END) AS unfinished_trips
        FROM trips WHERE start_day >= date('now','-30 day') GROUP BY start_day
    )
    SELECT (strftime('%s', days.day)*1000) AS time, days.day,
           COALESCE(agg.trips,0) AS trips,
           COALESCE(agg.unfinished_trips,0) AS unfinished_trips
    FROM days LEFT JOIN agg ON agg.day=days.day ORDER BY days.day;""")

    # --- Analyse-Views (bauen auf v_trips_monitoring auf) ---
    cur.execute("""
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
        ORDER BY time;""")

    cur.execute("""
    CREATE VIEW v_ghost_trips AS
        SELECT * FROM v_trips_monitoring WHERE is_ghost_trip = 1 ORDER BY start_time;""")

    cur.execute("""
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
        ORDER BY start_time;""")

    cur.execute("""
    CREATE VIEW v_long_trips AS
        SELECT * FROM v_trips_monitoring WHERE is_long_trip = 1 ORDER BY start_time;""")

    cur.execute("""
    CREATE VIEW v_negative_values AS
        SELECT * FROM v_trips_monitoring WHERE is_negative_value = 1 ORDER BY start_time;""")

    cur.execute("""
    CREATE VIEW v_teleportation_events AS
        SELECT * FROM v_trips_monitoring WHERE is_teleportation = 1 ORDER BY start_time;""")

    cur.execute("""
    CREATE VIEW v_timeout_trips AS
        SELECT * FROM v_trips_monitoring WHERE is_timeout_trip = 1 ORDER BY start_time;""")

    cur.execute("""
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
        ORDER BY start_time;""")

    cur.execute("""
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
        ORDER BY start_time;""")

    cur.execute("""
    CREATE VIEW v_trips_last_31_days AS
        SELECT * FROM v_trips_monitoring
        WHERE start_time >= datetime('now', '-31 days')
        ORDER BY start_time;""")


# Hauptablauf

def main():
    global ENV_PATH, DB_PATH, LOG_PATH

    local_test_mode = False
    json_export_path = None
    if not os.path.exists(ENV_PATH):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_env = os.path.join(script_dir, ".env")
        if os.path.exists(local_env):
            local_test_mode = True
            ENV_PATH = local_env
            json_export_path = os.path.join(script_dir, "trips_all.json")
            if "TRIPMON_DB" not in os.environ:
                DB_PATH = os.path.join(script_dir, "trips_local.db")
            if "TRIPMON_LOG" not in os.environ:
                LOG_PATH = os.path.join(script_dir, "tripmon_local.log")

    _setup_logging()
    log.info("===== Monitoring-Lauf gestartet =====")
    if local_test_mode:
        log.warning("LOKALER TESTMODUS aktiv | Env=%s | DB=%s", ENV_PATH, DB_PATH)

    load_dotenv(ENV_PATH)

    base_url = os.getenv("BASE_URL", "https://api.cartelsol.mosdon-dev.com")
    token_url = os.getenv("TOKEN_URL", "https://auth.cartelsol.mosdon-dev.com/oauth2/token")
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")

    missing = [n for n, val in {
        "CLIENT_ID": client_id,
        "ACCESS_TOKEN": os.getenv("ACCESS_TOKEN"),
        "REFRESH_TOKEN": os.getenv("REFRESH_TOKEN"),
    }.items() if not val or val.startswith("HIER_")]
    if missing:
        raise RuntimeError(
            f"Fehlende Pflichtwerte in {ENV_PATH}: {', '.join(missing)}"
        )

    token_manager = TokenManager(token_url, client_id, client_secret, ENV_PATH,
                                 base_url=base_url)
    token_manager.load_tokens()

    # 1) Fahrzeuge holen
    vehicles = fetch_vehicles(base_url, token_manager)
    vins = list(dict.fromkeys(v["vin"] for v in vehicles if "vin" in v))
    if not vins:
        raise ValueError("Keine VINs im /vehicles-Response gefunden")
    log.info("Fahrzeuge: %d | VINs: %d", len(vehicles), len(vins))

    vin_to_model, _ = build_vehicle_maps(vehicles)

    # 2) Trips holen 
    all_trips, failed_vins = fetch_all_trips(base_url, vins, vin_to_model, token_manager)
    log.info("Gesamt Trips ueber alle Fahrzeuge: %d", len(all_trips))

    # 2b) NUR lokal: JSON-Export zum Reinschauen in VS Code (auf dem Server inaktiv)
    if local_test_mode and json_export_path:
        n = export_trips_json(json_export_path, all_trips, vehicles, vin_to_model)
        log.info("LOKAL: JSON-Export geschrieben: %d Trips -> %s", n, json_export_path)

    # 3) Speichern 
    conn = open_db(DB_PATH)
    try:
        cur = conn.cursor()
        v_count = save_vehicles(cur, vehicles)
        t_count = save_trips(cur, all_trips, vin_to_model)
        create_views(cur)
        conn.commit()
    finally:
        conn.close()

    log.info("SQLite aktualisiert: %s", DB_PATH)
    log.info("Vehicles gespeichert: %d | Trips gespeichert: %d", v_count, t_count)
    if failed_vins:
        log.warning("Lauf mit %d uebersprungenen VINs beendet", len(failed_vins))
    log.info("===== Monitoring-Lauf erfolgreich beendet =====")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.getLogger("tripdaten-mon").critical(
            "ABBRUCH des Monitoring-Laufs: %s", exc, exc_info=True)
        sys.exit(1)
