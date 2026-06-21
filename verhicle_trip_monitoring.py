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


