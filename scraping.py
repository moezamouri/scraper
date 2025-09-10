#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scraping.py — SolarWeb scraper for Fly.io with HA updates via Tailscale

- Logs in to Fronius
- Opens PV system page
- Reads Production, Grid feed-in, and Consumption (from text patterns)
- Sends values to Home Assistant over Tailscale
- Runs a dummy HTTP server on port 8080 (Fly.io requirement)

Env vars needed:
  EMAIL, PASSWORD
  HA_TOKEN
  HA_URL_PROD       (default http://100.67.69.31:8123/api/states/sensor.pv_production)
  HA_URL_CONS       (default http://100.67.69.31:8123/api/states/sensor.pv_production2)
  HA_URL_GRID       (default http://100.67.69.31:8123/api/states/sensor.grid_export)
"""

import os
import re
import sys
import time
import signal
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ───────────────────────────────────────────────
# Dummy HTTP Server for Fly.io health check
# ───────────────────────────────────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_dummy_server():
    server = HTTPServer(("0.0.0.0", 8080), DummyHandler)
    server.serve_forever()


# ───────────────────────────────────────────────
# Config (env)
# ───────────────────────────────────────────────
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
HA_TOKEN = os.getenv("HA_TOKEN")

HA_URL_PROD = os.getenv("HA_URL_PROD", "http://100.67.69.31:8123/api/states/sensor.pv_production")
HA_URL_CONS = os.getenv("HA_URL_CONS", "http://100.67.69.31:8123/api/states/sensor.pv_production2")
HA_URL_GRID = os.getenv("HA_URL_GRID", "http://100.67.69.31:8123/api/states/sensor.grid_export")

LOGIN_URL = os.getenv("LOGIN_URL", "https://login.fronius.com")
PV_SYSTEM_URL = os.getenv(
    "PV_SYSTEM_URL",
    "https://www.solarweb.com/PvSystems/PvSystem?pvSystemId=a2064e7f-807b-4ec8-99e2-d271da292275"
)

SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "5"))
SHORT_WAIT, MED_WAIT, LONG_WAIT = 5, 20, 40


# ───────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def send_to_ha(url, value, name):
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "state": str(value),
        "attributes": {
            "friendly_name": name,
            "unit_of_measurement": "W"
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code != 200:
            log(f"⚠ Failed to update {name}: {r.text}")
    except Exception as e:
        log(f"⚠ Exception updating {name}: {e}")


# ───────────────────────────────────────────────
# Browser setup
# ───────────────────────────────────────────────
def make_driver():
    log(f"Launching Chrome headless...")
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1366,960")
    opts.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36")
    driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opts)
    return driver


# ───────────────────────────────────────────────
# Login + open PV page
# ───────────────────────────────────────────────
def do_login(driver):
    log("Navigating to login page…")
    driver.get(LOGIN_URL)
    WebDriverWait(driver, MED_WAIT).until(EC.element_to_be_clickable((By.ID, "usernameUserInput"))).send_keys(EMAIL)
    pwd = WebDriverWait(driver, MED_WAIT).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
    pwd.send_keys(PASSWORD)
    pwd.send_keys(Keys.RETURN)
    log("Submitted login form.")
    time.sleep(5)

def open_pv(driver):
    log("Opening PV system page…")
    driver.get(PV_SYSTEM_URL)
    WebDriverWait(driver, LONG_WAIT).until(EC.presence_of_element_located((By.ID, "powerWidget")))
    log("✓ PV system page loaded.")


# ───────────────────────────────────────────────
# Scraping logic
# ───────────────────────────────────────────────
JS_EXTRACT = r"""
function extract() {
  const txt = document.body.innerText;

  const prodMatch = /(\d+(?:[.,]\d+)?)\s*(kW|W)\s+of solar energy is produced/i.exec(txt);
  const gridMatch = /(\d+(?:[.,]\d+)?)\s*(kW|W)\s+are being fed into the grid/i.exec(txt);
  const consMatch = /consumption is\s+(\d+(?:[.,]\d+)?)\s*(kW|W)/i.exec(txt);

  function toWatts(numStr, unit){
    if(!numStr) return null;
    let v = parseFloat(numStr.replace(',','.'));
    if(isNaN(v)) return null;
    if(unit.toLowerCase() === 'kw') v *= 1000;
    return v;
  }

  const prod = prodMatch ? toWatts(prodMatch[1], prodMatch[2]) : null;
  const grid = gridMatch ? toWatts(gridMatch[1], gridMatch[2]) : null;
  const cons = consMatch ? toWatts(consMatch[1], consMatch[2]) : null;

  return { production: prod, gridFeedIn: grid, consumption: cons };
}
return extract();
"""

def scrape_once(driver):
    data = driver.execute_script(JS_EXTRACT)
    prod, grid, cons = data.get("production"), data.get("gridFeedIn"), data.get("consumption")
    log(f"DEBUG: prod={prod} | grid={grid} | cons={cons}")
    return prod, grid, cons


# ───────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────
def main():
    driver = make_driver()
    try:
        do_login(driver)
        open_pv(driver)

        log(f"✓ Entering scrape loop every {SCRAPE_INTERVAL_SEC}s")
        while True:
            prod, grid, cons = scrape_once(driver)

            if prod is not None:
                send_to_ha(HA_URL_PROD, prod, "PV Production")
            if cons is not None:
                send_to_ha(HA_URL_CONS, cons, "PV Consumption")
            if grid is not None:
                send_to_ha(HA_URL_GRID, grid, "Grid Export")

            time.sleep(SCRAPE_INTERVAL_SEC)

    except KeyboardInterrupt:
        log("Interrupted. Exiting…")
    except Exception as e:
        log(f"✖ Fatal error: {e}")
        traceback.print_exc()
    finally:
        driver.quit()
        log("Chrome closed.")


def _wire_signals():
    def _exit(_sig, _frm): raise KeyboardInterrupt()
    try:
        signal.signal(signal.SIGINT, _exit)
        signal.signal(signal.SIGTERM, _exit)
    except Exception:
        pass


if __name__ == "__main__":
    _wire_signals()
    threading.Thread(target=run_dummy_server, daemon=True).start()
    main()
