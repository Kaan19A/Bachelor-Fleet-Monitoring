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

//Normaliserung Zeitangaben: ISO-8601 -> datetime UTC, epoch seconds, Starttag, Wochentag, Monat
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

// Alle Fahrzeuge holen

    vehicles_data = get_json(f"{BASE_URL}/vehicles")

if isinstance(vehicles_data, dict) and isinstance(vehicles_data.get("items"), list):
    vehicles = vehicles_data["items"]
elif isinstance(vehicles_data, list):
    vehicles = vehicles_data
else:
    raise ValueError("Unerwartetes Format von /vehicles")
