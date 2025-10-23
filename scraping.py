#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SolarWeb JSON API scraper → Home Assistant updater (Railway-ready)

ENV (set in Railway):
  # SolarWeb
  PV_SYSTEM_ID           (preferred)  e.g. a2064e7f-807b-4ec8-99e2-d271da292275
  PV_SYSTEM_URL          (optional; if set, ID auto-extracted from ?pvSystemId=...)
  SCRAPE_INTERVAL_SEC    (default 30)

  # Home Assistant
  HA_URL                 e.g. http://100.67.69.31:8123
  HA_TOKEN               (Long-Lived Access Token)
  HA_PROXY_URL           (optional per-request proxy, e.g. socks5h://127.0.0.1:1055)

Notes:
- Uses https://www.solarweb.com/api/v1/PowerFlowRealtimeData?pvSystemId=<ID>
- Parses Body.Data.Site.{P_PV, P_Load, P_Grid}
- Caches last-known-good values; never overwrites sensors with 0 on fetch errors.
"""

import os
import re
import time
import json
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests


# ────────────────────────── Config ──────────────────────────
HA_URL        = os.getenv("HA_URL", "http://100.67.69.31:8123")
HA_TOKEN      = os.getenv("HA_TOKEN") or ""
HA_PROXY_URL  = os.getenv("HA_PROXY_URL")  # optional per-request proxy for HA calls

# Prefer explicit ID, else extract from PV_SYSTEM_URL
PV_SYSTEM_ID  = (os.getenv("PV_SYSTEM_ID") or "").strip()
PV_SYSTEM_URL = (os.getenv("PV_SYSTEM_URL") or "").strip()

SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "30"))

SENSOR_PRODUCTION  = "sensor.pv_production"   # W
SENSOR_CONSUMPTION = "sensor.pv_consumption"  # W
SENSOR_GRID        = "sensor.grid_export"     # W (feed-in)

# Current (2025) SolarWeb realtime endpoint
def _api_url(pv_id: str) -> str:
    return f"https://www.solarweb.com/api/v1/PowerFlowRealtimeData?pvSystemId={pv_id}"

# ────────────────────────── Helpers ─────────────────────────
def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _extract_id_from_url(url: str) -> str:
    """
    Extract UUID from PV_SYSTEM_URL (?pvSystemId=UUID). Returns "" if not found.
    """
    if not url:
        return ""
    m = re.search(r"pvSystemId=([0-9a-fA-F-]{36})", url)
    return m.group(1) if m else ""


def ha_set_state(entity_id: str, value: int | None) -> None:
    """
    Send value to Home Assistant sensor. Skips if HA not configured or value is None.
    """
    if not HA_URL or not HA_TOKEN:
        log(f"⚠ HA not configured; skipping update for {entity_id}.")
        return
    if value is None:
        log(f"⚠ Value None for {entity_id}; skipping.")
        return

    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "content-type": "application/json",
    }
    data = {"state": str(int(value))}
    try:
        kwargs = {"headers": headers, "json": data, "timeout": 15}
        if HA_PROXY_URL:
            kwargs["proxies"] = {"http": HA_PROXY_URL, "https": HA_PROXY_URL}
        r = requests.post(url, **kwargs)
        r.raise_for_status()
        log(f"✓ Updated {entity_id} = {value}")
    except Exception as e:
        log(f"⚠ Exception updating {entity_id}: {e}")


# ───────────────────── SolarWeb JSON fetch ───────────────────
SESSION = requests.Session()
SESSION.headers.update({
    # Be a good citizen; some CDNs require UA.
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Python/requests SolarWebScraper/1.0",
    "Accept": "application/json, text/plain, */*",
})

def fetch_solarweb_data(pv_id: str) -> tuple[int | None, int | None, int | None]:
    """
    Returns (production_W, grid_export_W, consumption_W) or (None, None, None) on error.

    JSON shape (example):
    {
      "Body": {
        "Data": {
          "Site": {
            "P_PV": 4580,
            "P_Load": -620,
            "P_Grid": 3960
          }
        }
      },
      "Head": {"Status":{"Code":0}}
    }
    """
    try:
        url = _api_url(pv_id)
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()

        # Guard: some responses nest slightly differently; be defensive.
        site = (
            data.get("Body", {})
                .get("Data", {})
                .get("Site", {})
        )

        # Production (PV), always >= 0
        production = site.get("P_PV")

        # Load: sign varies; store as absolute consumption in W
        p_load = site.get("P_Load")
        consumption = abs(p_load) if isinstance(p_load, (int, float)) else None

        # Grid: convention can vary per firmware. We emit *export to grid*.
        # Heuristic:
        # - If P_Grid is negative → exporting (export = abs)
        # - If P_Grid is positive → importing (export = 0)
        p_grid = site.get("P_Grid")
        grid_export = None
        if isinstance(p_grid, (int, float)):
            if p_grid < 0:
                grid_export = int(abs(p_grid))
            else:
                grid_export = 0

        # Sanitize to ints when present
        production = int(production) if production is not None else None
        consumption = int(consumption) if consumption is not None else None
        grid_export = int(grid_export) if grid_export is not None else None

        # Optional: raw debug to verify signs once
        log(f"DEBUG raw site: P_PV={site.get('P_PV')} P_Load={site.get('P_Load')} P_Grid={site.get('P_Grid')}")

        return production, grid_export, consumption

    except Exception as e:
        log(f"✖ Fetch error: {e}")
        traceback.print_exc()
        return None, None, None


# ───────────────────── Liveness HTTP server ──────────────────
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


# ─────────────────────────── Main ────────────────────────────
def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()

    # Resolve PV system ID
    pv_id = PV_SYSTEM_ID or _extract_id_from_url(PV_SYSTEM_URL)
    if not pv_id:
        raise RuntimeError("Missing PV_SYSTEM_ID (or PV_SYSTEM_URL to auto-extract it).")

    log(f"Starting SolarWeb API scraper (interval={SCRAPE_INTERVAL_SEC}s)")
    log(f"Target system: {pv_id}")
    log(f"Endpoint: {_api_url(pv_id)}")

    last = {"prod": None, "grid": None, "cons": None}

    while True:
        try:
            prod, grid, cons = fetch_solarweb_data(pv_id)

            if prod is None and grid is None and cons is None:
                log("No data fetched this cycle; keeping last-known values.")
            else:
                changed = False

                if prod is not None and prod != last["prod"]:
                    last["prod"] = prod
                    ha_set_state(SENSOR_PRODUCTION, prod)
                    changed = True

                if cons is not None and cons != last["cons"]:
                    last["cons"] = cons
                    ha_set_state(SENSOR_CONSUMPTION, cons)
                    changed = True

                if grid is not None and grid != last["grid"]:
                    last["grid"] = grid
                    ha_set_state(SENSOR_GRID, grid)
                    changed = True

                log(f"Current Power → Production: {last['prod']} W | Consumption: {last['cons']} W | Grid export: {last['grid']} W"
                    + ("" if changed else " (no change)"))

        except Exception as e:
            log(f"✖ Scrape cycle error: {e}")
            traceback.print_exc()

        time.sleep(SCRAPE_INTERVAL_SEC)


if __name__ == "__main__":
    main()
