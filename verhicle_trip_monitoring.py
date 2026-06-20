import requests
import json
import os
from datetime import datetime, timezone
import sqlite3

BASE_URL = "https://api.cartelsol.mosdon-dev.com"
TOKEN_URL = "https://auth.cartelsol.mosdon-dev.com/oauth2/token"

# 
BEARER_TOKEN = ""



def get_headers():
    if not BEARER_TOKEN:
        raise ValueError("Bitte zuerst den Bearer Token in BEARER_TOKEN einfuegen.")

    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Accept": "application/json",
    }


def get_trip_data():
    url = f"{BASE_URL}{TRIPS_ENDPOINT}"
    response = requests.get(url, headers=get_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    trip_data = get_trip_data()
    print(json.dumps(trip_data, indent=2, ensure_ascii=False))

