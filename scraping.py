#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Railway SolarWeb/Fronius scraper → Home Assistant updater

Env vars (same semantics as your local + HA version):
  EMAIL, PASSWORD
  LOGIN_URL              (default https://login.fronius.com)
  PV_SYSTEM_URL          (SolarWeb system URL)
  USERNAME               (optional for generic SolarWeb login)
  PROXY_URL              (optional Selenium proxy, e.g. socks5://127.0.0.1:1055)
  SCRAPE_INTERVAL_SEC    (default 5)
  HEADLESS               (0/1, default 1 on Railway)

Home Assistant:
  HA_URL                 (e.g. http://100.67.69.31:8123)
  HA_TOKEN               (Long-Lived Access Token)
  HA_PROXY_URL           (optional per-request proxy for HA calls)

Optional XPath overrides (if your dashboard layout changes):
  XPATH_PROD, XPATH_CONS, XPATH_GRID
"""

import os
import time
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ───────────────────────── Config ─────────────────────────
HA_URL        = os.getenv("HA_URL", "http://100.67.69.31:8123")
HA_TOKEN      = os.getenv("HA_TOKEN", "")
HA_PROXY_URL  = os.getenv("HA_PROXY_URL")  # e.g. socks5h://127.0.0.1:1055

LOGIN_URL     = os.getenv("LOGIN_URL", "https://login.fronius.com")
USERNAME      = os.getenv("USERNAME")      # for generic SolarWeb login
PASSWORD      = os.getenv("PASSWORD")

EMAIL         = os.getenv("EMAIL")         # for Fronius SSO (preferred)
PV_SYSTEM_URL = os.getenv("PV_SYSTEM_URL", "https://www.solarweb.com/PvSystems/PvSystem?pvSystemId=a2064e7f-807b-4ec8-99e2-d271da292275")

SENSOR_PRODUCTION  = "sensor.pv_production"
SENSOR_CONSUMPTION = "sensor.pv_consumption"
SENSOR_GRID        = "sensor.grid_export"

PROXY_URL     = (os.getenv("PROXY_URL") or "").strip() or None
SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "5"))
HEADLESS      = os.getenv("HEADLESS", "1") not in ("0", "false", "False")

SHORT_WAIT, MED_WAIT, LONG_WAIT = 5, 20, 40

# XPaths (can be overridden by env)
XPATH_PROD = os.getenv("XPATH_PROD", "/html/body/div[3]/div[1]/div/div/div[2]/div/div/div[2]/div[2]/div[2]/div/span[1]/b")
XPATH_CONS = os.getenv("XPATH_CONS", "/html/body/div[3]/div[1]/div/div/div[2]/div/div/div[2]/div[2]/div[1]/div/span[1]/b")
XPATH_GRID = os.getenv("XPATH_GRID", "/html/body/div[3]/div[1]/div/div/div[2]/div[2]/div[3]/div/span[1]/b")


# ───────────────────────── Utils ─────────────────────────
def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def _parse_watts(txt: Optional[str]) -> Optional[int]:
    """
    Accepts strings like '829 W', '829W', '0.83 kW', '0,83 kW'. → int watts
    """
    if not txt:
        return None
    t = txt.replace("\xa0", " ").strip()
    import re
    m = re.search(r"(\d+(?:[.,]\d+)?)(?:\s*)?(kW|W)?", t, re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "W").lower()
    if unit == "kw":
        val *= 1000.0
    return int(round(val))


# Robust EN/DE innerText parser (same as your local working one)
JS_EXTRACT = r"""
function extract() {
  try {
    const txt = document.body.innerText.normalize('NFKD');

    const prodMatch = /(\d+(?:[.,]\d+)?)[ ]*(kW|W)[ ]*(?:of solar energy is produced|produced|production|erzeugt|produktion)/i.exec(txt);
    const gridMatch = /(\d+(?:[.,]\d+)?)[ ]*(kW|W)[ ]*(?:are being fed into the grid|fed into the grid|einspeisung|eingespeist|netz)/i.exec(txt);
    const consMatch = /(?:consumption|hausverbrauch|verbrauch)[^0-9]*?(\d+(?:[.,]\d+)?)[ ]*(kW|W)/i.exec(txt);

    function toWatts(numStr, unit){
      if(!numStr) return null;
      let v = parseFloat(numStr.replace(',', '.'));
      if(isNaN(v)) return null;
      if(unit && unit.toLowerCase() === 'kw') v *= 1000;
      return Math.round(v);
    }

    const production  = prodMatch ? toWatts(prodMatch[1], prodMatch[2]) : null;
    const gridFeedIn  = gridMatch ? toWatts(gridMatch[1], gridMatch[2]) : null;
    const consumption = consMatch ? toWatts(consMatch[1], consMatch[2]) : null;

    return { production, gridFeedIn, consumption };
  } catch(e) {
    return { production: null, gridFeedIn: null, consumption: null };
  }
}
return extract();
"""


# ───────────────── Selenium setup ─────────────────
def make_driver():
    log("Launching Chrome...")
    opts = webdriver.ChromeOptions()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1366,960")
    opts.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36")
    if PROXY_URL:
        opts.add_argument(f"--proxy-server={PROXY_URL}")

    # Railway images typically have chromedriver at this path; adjust if needed
    try:
        driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opts)
    except Exception:
        # Fallback to PATH
        driver = webdriver.Chrome(service=Service(), options=opts)
    return driver


def _try_accept_cookies(driver):
    try:
        selectors = [
            "button#onetrust-accept-btn-handler",
            "button[aria-label='Accept all']",
            "button[aria-label='Accept']",
            "button.cookie-btn-accept",
            "//button[contains(., 'Accept') or contains(., 'Agree') or contains(., 'Akzeptieren') or contains(., 'Alle akzeptieren')]",
        ]
        for sel in selectors:
            try:
                el = (WebDriverWait(driver, 3)
                      .until(EC.element_to_be_clickable((By.XPATH, sel))) if sel.startswith("//")
                      else WebDriverWait(driver, 3)
                      .until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel))))
                el.click()
                log("Accepted cookies banner")
                return
            except Exception:
                continue
    except Exception:
        pass


def _switch_to_login_iframe_if_any(driver):
    try:
        for idx, frame in enumerate(driver.find_elements(By.TAG_NAME, "iframe")):
            try:
                driver.switch_to.frame(frame)
                email_el, *_ = _find_login_fields(driver, quick=True)
                if email_el:
                    log(f"Switched to login iframe index={idx}")
                    return True
                driver.switch_to.default_content()
            except Exception:
                driver.switch_to.default_content()
                continue
    except Exception:
        pass
    return False


def _find_login_fields(driver, quick=False):
    timeout = 5 if quick else 30
    email_candidates = [
        (By.NAME, "Email"),
        (By.NAME, "email"),
        (By.ID, "Email"),
        (By.ID, "email"),
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.CSS_SELECTOR, "input[id*='email']"),
        (By.XPATH, "//label[contains(., 'Email')]/following::input[1]"),
        # Fronius specific:
        (By.ID, "usernameUserInput"),
    ]
    last_err = None
    for by, key in email_candidates:
        try:
            email_el = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, key)))
            # password
            pwd_el = None
            for pby, pkey in [
                (By.NAME, "Password"),
                (By.NAME, "password"),
                (By.ID, "Password"),
                (By.ID, "password"),
                (By.CSS_SELECTOR, "input[type='password']"),
                (By.CSS_SELECTOR, "input[id*='password']"),
                (By.XPATH, "//label[contains(., 'Password') or contains(., 'Passwort')]/following::input[1]"),
            ]:
                try:
                    pwd_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((pby, pkey)))
                    break
                except Exception:
                    continue
            # submit
            submit_el = None
            for sby, skey in [
                (By.ID, "submitButton"),
                (By.CSS_SELECTOR, "button[type='submit']"),
                (By.XPATH, "//button[contains(., 'Sign in') or contains(., 'Anmelden') or contains(., 'Log in')]"),
                (By.XPATH, "//input[@type='submit']"),
            ]:
                try:
                    submit_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((sby, skey)))
                    break
                except Exception:
                    continue
            return email_el, pwd_el, submit_el
        except Exception as e:
            last_err = e
            continue
    if last_err:
        log(f"Login fields not found yet; retrying. Last error: {type(last_err).__name__}")
    return None, None, None


# ───────────────── Login flows ─────────────────
def do_login_fronius(driver):
    log("Navigating to Fronius login…")
    driver.get(LOGIN_URL)
    _try_accept_cookies(driver)

    email_el, password_el, submit_el = _find_login_fields(driver)
    if not email_el and _switch_to_login_iframe_if_any(driver):
        email_el, password_el, submit_el = _find_login_fields(driver)
    if not email_el:
        raise TimeoutError("Could not locate login inputs on Fronius login page.")

    email_el.clear(); email_el.send_keys(EMAIL)
    if not password_el:
        password_el = WebDriverWait(driver, MED_WAIT).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
    password_el.clear(); password_el.send_keys(PASSWORD)
    password_el.send_keys(Keys.RETURN)
    log("Submitted login form.")
    time.sleep(5)
    driver.switch_to.default_content()


def do_login_solarweb_generic(driver):
    log("Navigating to SolarWeb login…")
    driver.get("https://www.solarweb.com/Account/SignIn")
    WebDriverWait(driver, 60).until(lambda d: d.execute_script("return document.readyState") == "complete")
    _try_accept_cookies(driver)

    email_el, password_el, submit_el = _find_login_fields(driver)
    if not email_el and _switch_to_login_iframe_if_any(driver):
        email_el, password_el, submit_el = _find_login_fields(driver)

    if not email_el:
        log(f"Login form not detected. URL={driver.current_url} title={driver.title}")
        raise TimeoutError("Could not locate login fields on SolarWeb login page")

    email_el.clear(); email_el.send_keys(EMAIL or USERNAME)
    if password_el:
        password_el.clear(); password_el.send_keys(PASSWORD)
    if submit_el:
        submit_el.click()
    else:
        password_el.send_keys(Keys.RETURN)

    driver.switch_to.default_content()
    time.sleep(3)


def open_pv(driver):
    log("Opening PV system page…")
    driver.get(PV_SYSTEM_URL)
    # Wait for widget/values to render
    try:
        WebDriverWait(driver, LONG_WAIT).until(
            EC.any_of(
                EC.presence_of_element_located((By.ID, "powerWidget")),
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(),' W') or contains(text(),' kW')]"))
            )
        )
    except Exception:
        pass
    log("✓ PV system page loaded.")


# ──────────────── Extraction helpers ────────────────
def _extract_by_xpath(driver) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    prod = cons = grid = None
    try:
        prod_el = WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, XPATH_PROD)))
        prod = _parse_watts(prod_el.text)
    except Exception:
        pass
    try:
        cons_el = driver.find_element(By.XPATH, XPATH_CONS)
        cons = _parse_watts(cons_el.text)
    except Exception:
        pass
    try:
        grid_el = driver.find_element(By.XPATH, XPATH_GRID)
        grid = _parse_watts(grid_el.text)
    except Exception:
        pass
    return prod, cons, grid


def scrape_once(driver):
    # 1) Resilient text/regex (EN+DE)
    data = driver.execute_script(JS_EXTRACT) or {}
    prod = data.get("production")
    grid = data.get("gridFeedIn")
    cons = data.get("consumption")

    # 2) Fallback DOM XPaths
    if prod is None or grid is None or cons is None:
        xp_prod, xp_cons, xp_grid = _extract_by_xpath(driver)
        if prod is None: prod = xp_prod
        if cons is None: cons = xp_cons
        if grid is None: grid = xp_grid

    log(f"DEBUG: prod={prod} | grid={grid} | cons={cons}")
    return prod, grid, cons


# ──────────────── HA integration ────────────────
def ha_set_state(entity_id, value):
    if not HA_TOKEN or not HA_URL:
        log(f"HA not configured, skipping update: {entity_id}={value}")
        return
    if value is None:
        return  # don’t spam 0s
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


# ──────────────── Liveness server ────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")


def run_dummy_server():
    try:
        server = HTTPServer(("0.0.0.0", 8080), DummyHandler)
        server.serve_forever()
    except Exception:
        pass


# ──────────────── Main ────────────────
def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()

    if not PASSWORD:
        raise RuntimeError("PASSWORD not provided. Set env vars.")

    driver = make_driver()
    last = {"prod": None, "cons": None, "grid": None}

    try:
        # Prefer Fronius SSO if EMAIL provided, else generic SolarWeb
        try:
            if EMAIL:
                do_login_fronius(driver)
            else:
                raise RuntimeError("EMAIL not set, using SolarWeb flow")
        except Exception as e:
            log(f"Fronius login failed ({e}). Trying SolarWeb login flow…")
            do_login_solarweb_generic(driver)

        open_pv(driver)
        log(f"✓ Entering scrape loop every {SCRAPE_INTERVAL_SEC}s")

        while True:
            try:
                prod, grid, cons = scrape_once(driver)

                updated = False
                if prod is not None:
                    last["prod"] = int(prod); updated = True
                if cons is not None:
                    last["cons"] = int(cons); updated = True
                if grid is not None:
                    last["grid"] = int(grid); updated = True

                if not updated and all(v is None for v in last.values()):
                    log("No values yet; skipping HA update this cycle.")
                else:
                    # Use last-known-good values; never force 0 on scrape failure
                    ha_set_state(SENSOR_PRODUCTION,  last["prod"])
                    ha_set_state(SENSOR_CONSUMPTION, last["cons"])
                    ha_set_state(SENSOR_GRID,        last["grid"])

            except Exception as e:
                log(f"✖ Scrape cycle error: {e}")
                traceback.print_exc()

            time.sleep(SCRAPE_INTERVAL_SEC)

    finally:
        log("Shutting down…")
        try:
            driver.quit()
        except Exception:
            pass
        log("Chrome closed.")


if __name__ == "__main__":
    main()
