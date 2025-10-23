#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SolarWeb JSON scraper → Home Assistant updater (Railway-ready, robust)

ENV to set in Railway:
  # SolarWeb (either)
  PV_SYSTEM_ID         e.g. a2064e7f-807b-4ec8-99e2-d271da292275
  PV_SYSTEM_URL        optional; auto-extracts ID from ?pvSystemId=...

  SCRAPE_INTERVAL_SEC  default 30

  # Home Assistant
  HA_URL               e.g. http://100.67.69.31:8123
  HA_TOKEN             HA long-lived token
  HA_PROXY_URL         optional per-request proxy (e.g. socks5h://127.0.0.1:1055)
"""

import os
import re
import time
import json
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple

import requests


# ───────────────────────── Config ─────────────────────────
HA_URL        = os.getenv("HA_URL", "http://100.67.69.31:8123")
HA_TOKEN      = (os.getenv("HA_TOKEN") or "").strip()
HA_PROXY_URL  = (os.getenv("HA_PROXY_URL") or "").strip() or None

PV_SYSTEM_ID  = (os.getenv("PV_SYSTEM_ID") or "").strip()
PV_SYSTEM_URL = (os.getenv("PV_SYSTEM_URL") or "").strip()

SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "30"))

SENSOR_PRODUCTION  = "sensor.pv_production"   # W
SENSOR_CONSUMPTION = "sensor.pv_consumption"  # W
SENSOR_GRID        = "sensor.grid_export"     # W (feed-in)

# Try endpoints in this order; first 200 wins.
ENDPOINT_PATHS = [
    # Most common current ones
    "/api/PowerFlow/GetPowerFlowRealtimeData",
    "/api/PowerFlow/PowerFlowRealtimeData",
    "/PowerFlow/GetPowerFlowRealtimeData",
    # Some regions/rollouts seen with this
    "/api/v1/PowerFlowRealtimeData",
    # Legacy
    "/RealTimeData/GetGraphData",
]

BASE = "https://www.solarweb.com"

SESSION = requests.Session()


# ───────────────────────── Utils ─────────────────────────
def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _extract_id_from_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"pvSystemId=([0-9a-fA-F-]{36})", url)
    return m.group(1) if m else ""


def _build_referer(pv_id: str) -> str:
    return f"{BASE}/PvSystems/PvSystem?pvSystemId={pv_id}"


def ha_set_state(entity_id: str, value: Optional[int]) -> None:
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
    payload = {"state": str(int(value))}
    try:
        kwargs = {"headers": headers, "json": payload, "timeout": 15}
        if HA_PROXY_URL:
            kwargs["proxies"] = {"http": HA_PROXY_URL, "https": HA_PROXY_URL}
        r = requests.post(url, **kwargs)
        r.raise_for_status()
        log(f"✓ Updated {entity_id} = {value}")
    except Exception as e:
        log(f"⚠ Exception updating {entity_id}: {e}")


# ───────────────────── SolarWeb fetch ─────────────────────
def _parse_powerflow_site_shape(data: dict) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Shape:
    {"Body":{"Data":{"Site":{"P_PV":4580,"P_Load":-620,"P_Grid":-3960}}}}
    Return (prod, grid_export, consumption)
    """
    site = data.get("Body", {}).get("Data", {}).get("Site", {})
    if not site:
        return None, None, None

    # Production: non-negative
    prod = site.get("P_PV")
    prod = int(prod) if isinstance(prod, (int, float)) else None

    # Consumption: absolute of P_Load
    p_load = site.get("P_Load")
    cons = int(abs(p_load)) if isinstance(p_load, (int, float)) else None

    # Grid export: positive export in W (SolarWeb sign can vary)
    p_grid = site.get("P_Grid")
    if isinstance(p_grid, (int, float)):
        grid_export = int(abs(p_grid)) if p_grid < 0 else 0
    else:
        grid_export = None

    return prod, grid_export, cons


def _parse_legacy_flat_shape(data: dict) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Shape:
    {"CurrentPower":4560,"Consumption":487,"GridExport":1320,...}
    """
    prod = data.get("CurrentPower")
    cons = data.get("Consumption")
    grid = data.get("GridExport")
    prod = int(prod) if isinstance(prod, (int, float)) else None
    cons = int(cons) if isinstance(cons, (int, float)) else None
    grid = int(grid) if isinstance(grid, (int, float)) else None
    return prod, grid, cons


def fetch_solarweb_data(pv_id: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Try all known endpoints until one returns 200 and parse it.
    Returns (production_W, grid_export_W, consumption_W) or (None,None,None).
    """
    # Headers that make CDNs happy
    SESSION.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Python/requests SolarWebScraper/1.1",
        "Accept": "application/json, text/plain, */*",
        "Referer": _build_referer(pv_id),
        "Origin": BASE,
    })

    params = {"pvSystemId": pv_id}

    last_err = None
    for path in ENDPOINT_PATHS:
        url = f"{BASE}{path}"
        try:
            r = SESSION.get(url, params=params, timeout=20)
            if r.status_code == 404:
                # Try next known path
                log(f"INFO: {path} → 404, trying next endpoint…")
                continue
            r.raise_for_status()

            # Some responses are text/plain; try .json() with fallback
            try:
                data = r.json()
            except json.JSONDecodeError:
                data = json.loads(r.text)

            # Try PowerFlow shape first, then legacy flat
            prod, grid, cons = _parse_powerflow_site_shape(data)
            if prod is None and grid is None and cons is None:
                prod, grid, cons = _parse_legacy_flat_shape(data)

            if any(v is not None for v in (prod, grid, cons)):
                log(f"INFO: Using endpoint {path}")
                log(f"DEBUG raw: {data if isinstance(data, dict) else 'non-dict JSON'}")
                return prod, grid, cons

            log(f"INFO: {path} returned JSON but parsers found nothing; trying next…")

        except Exception as e:
            last_err = e
            log(f"WARN: fetch from {path} failed: {e}. Trying next…")

    if last_err:
        log(f"✖ All endpoints failed; last error: {last_err}")
        traceback.print_exc()

    return None, None, None


# ───────────────────── Liveness HTTP ─────────────────────
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


# ───────────────────────── Main ──────────────────────────
def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()

    pv_id = PV_SYSTEM_ID or _extract_id_from_url(PV_SYSTEM_URL)
    if not pv_id:
        raise RuntimeError("Missing PV_SYSTEM_ID (or PV_SYSTEM_URL to auto-extract it).")

    log(f"Starting SolarWeb scraper (interval={SCRAPE_INTERVAL_SEC}s)")
    log(f"Target system: {pv_id}")
    log(f"Referer: {_build_referer(pv_id)}")

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

                log(
                    f"Current Power → Production: {last['prod']} W | "
                    f"Consumption: {last['cons']} W | Grid export: {last['grid']} W"
                    + ("" if changed else " (no change)")
                )

        except Exception as e:
            log(f"✖ Scrape cycle error: {e}")
            traceback.print_exc()

        time.sleep(SCRAPE_INTERVAL_SEC)


if __name__ == "__main__":
    main()
