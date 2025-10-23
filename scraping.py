#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SolarWeb JSON API scraper → Home Assistant updater
---------------------------------------------------

Replaces Selenium scraping with direct JSON calls.

Env vars (set in Railway):
  EMAIL, PASSWORD       (optional; not used by API)
  PV_SYSTEM_ID          (your SolarWeb system UUID)
  SCRAPE_INTERVAL_SEC   (default 30)
  HA_URL                (e.g. http://100.67.69.31:8123)
  HA_TOKEN              (Home Assistant long-lived access token)
  HA_PROXY_URL          (optional SOCKS proxy for HA calls)
"""

import os
import time
import json
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests


# ───────────────────────────────────────────────
# Config
# ───────────────────────────────────────────────
HA_URL        = os.getenv("HA_URL", "http://100.67.69.31:8123")
HA_TOKEN      = os.getenv("HA_TOKEN")
HA_PROXY_URL  = os.getenv("HA_PROXY_URL")  # optional proxy for HA calls

PV_SYSTEM_ID  = os.getenv("PV_SYSTEM_ID", "a2064e7f-807b-4ec8-99e2-d271da292275")
SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "30"))

SENSOR_PRODUCTION  = "sensor.pv_production"
SENSOR_CONSUMPTION = "sensor.pv_consumption"
SENSOR_GRID        = "sensor.grid_export"

SOLARWEB_API = f"https://www.solarweb.com/RealTimeData/GetGraphData?pvSystemId={PV_SYSTEM_ID}"

# ───────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────
def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def ha_set_state(entity_id, value):
    """Send value to Home Assistant sensor."""
    if value is None or HA_URL is None or HA_TOKEN is None:
        log(f"⚠ Skipping HA update for {entity_id}: invalid value or HA not configured.")
        return
    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "content-type": "application/json",
    }
    data = {"state": str(int(value))}
    try:
        kwargs = {"headers": headers, "json": data, "timeout": 10}
        if HA_PROXY_URL:
            kwargs["proxies"] = {"http": HA_PROXY_URL, "https": HA_PROXY_URL}
        r = requests.post(url, **kwargs)
        r.raise_for_status()
        log(f"✓ Updated {entity_id} = {value}")
    except Exception as e:
        log(f"⚠ Exception updating {entity_id}: {e}")


# ───────────────────────────────────────────────
# Fetch SolarWeb JSON
# ───────────────────────────────────────────────
def fetch_solarweb_data():
    """Fetch and parse real-time data from SolarWeb."""
    try:
        r = requests.get(SOLARWEB_API, timeout=20)
        r.raise_for_status()
        data = r.json()

        # Extract relevant fields (names confirmed from SolarWeb payload)
        production  = data.get("CurrentPower")       # PV production (W)
        consumption = data.get("Consumption")        # House consumption (W)
        grid_export = data.get("GridExport")         # Feed-in to grid (W)
        if production is None and "Data" in data:
            # fallback for nested key variants
            sub = data["Data"]
            production  = sub.get("CurrentPower")
            consumption = sub.get("Consumption")
            grid_export = sub.get("GridExport")

        return int(production or 0), int(grid_export or 0), int(consumption or 0)

    except Exception as e:
        log(f"✖ Fetch error: {e}")
        traceback.print_exc()
        return None, None, None


# ───────────────────────────────────────────────
# Liveness HTTP endpoint
# ───────────────────────────────────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK\n")


def run_dummy_server():
    try:
        server = HTTPServer(("0.0.0.0", 8080), DummyHandler)
        server.serve_forever()
    except Exception:
        pass


# ───────────────────────────────────────────────
# Main Loop
# ───────────────────────────────────────────────
def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    log(f"Starting SolarWeb API scraper (interval={SCRAPE_INTERVAL_SEC}s)")
    log(f"Target system: {PV_SYSTEM_ID}")

    last = {"prod": None, "grid": None, "cons": None}

    while True:
        try:
            prod, grid, cons = fetch_solarweb_data()

            if prod is None and grid is None and cons is None:
                log("No data fetched this cycle.")
            else:
                # Update only if new or changed
                if prod is not None:
                    last["prod"] = prod
                    ha_set_state(SENSOR_PRODUCTION, prod)
                if cons is not None:
                    last["cons"] = cons
                    ha_set_state(SENSOR_CONSUMPTION, cons)
                if grid is not None:
                    last["grid"] = grid
                    ha_set_state(SENSOR_GRID, grid)

                log(f"Current Power → Production: {prod} W | Consumption: {cons} W | Grid export: {grid} W")

        except Exception as e:
            log(f"✖ Scrape cycle error: {e}")
            traceback.print_exc()

        time.sleep(SCRAPE_INTERVAL_SEC)


if __name__ == "__main__":
    main()
