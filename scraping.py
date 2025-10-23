#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Railway Selenium scraper → Home Assistant.

Fixes:
- Handles SolarWeb/Fronius iframe + lazy load
- Force-refreshes iframe when numbers go stale
- Robust EN/DE text parsing + XPath fallback
- Re-login if session dies
- Never writes 0 on bad scrape (keeps last-good)

ENV (same as before):
  EMAIL, PASSWORD
  LOGIN_URL                  (default https://login.fronius.com)
  PV_SYSTEM_URL              (SolarWeb system URL)
  SCRAPE_INTERVAL_SEC        (default 5)
  HEADLESS                   (default 1 on Railway)
  PROXY_URL                  (optional Selenium proxy)

Home Assistant:
  HA_URL, HA_TOKEN, HA_PROXY_URL

Optional:
  XPATH_PROD, XPATH_CONS, XPATH_GRID
  DEBUG_SAVE_SCREENSHOTS=1   (saves /app/snap_*.png)
  RELOGIN_MINUTES=120        (force page/login refresh cadence)
"""

import os, time, traceback, threading, re
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
HA_PROXY_URL  = os.getenv("HA_PROXY_URL")

LOGIN_URL     = os.getenv("LOGIN_URL", "https://login.fronius.com")
EMAIL         = os.getenv("EMAIL")
PASSWORD      = os.getenv("PASSWORD")
PV_SYSTEM_URL = os.getenv("PV_SYSTEM_URL", "https://www.solarweb.com/PvSystems/PvSystem?pvSystemId=a2064e7f-807b-4ec8-99e2-d271da292275")

SENSOR_PRODUCTION  = "sensor.pv_production"
SENSOR_CONSUMPTION = "sensor.pv_consumption"
SENSOR_GRID        = "sensor.grid_export"

PROXY_URL     = (os.getenv("PROXY_URL") or "").strip() or None
SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "5"))
HEADLESS      = os.getenv("HEADLESS", "1") not in ("0", "false", "False")
RELOGIN_MINUTES = int(os.getenv("RELOGIN_MINUTES", "120"))
DEBUG_SAVE_SCREENSHOTS = os.getenv("DEBUG_SAVE_SCREENSHOTS", "0") in ("1", "true", "True")

SHORT_WAIT, MED_WAIT, LONG_WAIT = 5, 20, 40

# XPaths (overridable by env)
XPATH_PROD = os.getenv("XPATH_PROD", "/html/body/div[3]/div[1]/div/div/div[2]/div/div/div[2]/div[2]/div[2]/div/span[1]/b")
XPATH_CONS = os.getenv("XPATH_CONS", "/html/body/div[3]/div[1]/div/div/div[2]/div/div/div[2]/div[2]/div[1]/div/span[1]/b")
XPATH_GRID = os.getenv("XPATH_GRID", "/html/body/div[3]/div[1]/div/div/div[2]/div[2]/div[3]/div/span[1]/b")

def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)

def _parse_watts_str(s: Optional[str]) -> Optional[int]:
    if not s: return None
    s = s.replace("\xa0", " ").strip()
    m = re.search(r"(\d+(?:[.,]\d+)?)(?:\s*)?(kW|W)?", s, re.I)
    if not m: return None
    val = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "W").lower()
    if unit == "kw": val *= 1000.0
    return int(round(val))

# Robust EN/DE innerText parser
JS_EXTRACT = r"""
function extract() {
  try {
    const rootDoc = document;
    let txt = rootDoc.body ? rootDoc.body.innerText : rootDoc.innerText;
    txt = (txt || "").normalize('NFKD');

    // Common EN/DE phrasings
    const prodMatch = /(\d+(?:[.,]\d+)?)[ ]*(kW|W)\s*(?:of solar energy is produced|produced|production|erzeugt|produktion)/i.exec(txt);
    const gridMatch = /(\d+(?:[.,]\d+)?)[ ]*(kW|W)\s*(?:are being fed into the grid|fed into the grid|einspeisung|eingespeist|netz)/i.exec(txt);
    const consMatch = /(?:consumption|hausverbrauch|verbrauch)[^0-9]*?(\d+(?:[.,]\d+)?)[ ]*(kW|W)/i.exec(txt);

    function toWatts(numStr, unit){
      if(!numStr) return null;
      let v = parseFloat(numStr.replace(',', '.'));
      if(isNaN(v)) return null;
      if(unit && unit.toLowerCase() === 'kw') v *= 1000;
      return Math.round(v);
    }

    return {
      production:  prodMatch ? toWatts(prodMatch[1], prodMatch[2]) : null,
      gridFeedIn:  gridMatch ? toWatts(gridMatch[1], gridMatch[2]) : null,
      consumption: consMatch ? toWatts(consMatch[1], consMatch[2]) : null
    };
  } catch(e) {
    return { production: null, gridFeedIn: null, consumption: null };
  }
}
return extract();
"""

def make_driver():
    log("Launching Chrome…")
    opts = webdriver.ChromeOptions()
    if HEADLESS: opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1366,960")
    # Prefer EN locale to stabilize labels
    opts.add_argument("--lang=en-US")
    opts.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36")
    if PROXY_URL: opts.add_argument(f"--proxy-server={PROXY_URL}")

    try:
        return webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opts)
    except Exception:
        return webdriver.Chrome(service=Service(), options=opts)

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
                el = (WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, sel)))
                      if sel.startswith("//")
                      else WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel))))
                el.click(); log("Accepted cookies banner"); return
            except Exception:
                continue
    except Exception:
        pass

def _switch_to_login_iframe_if_any(driver):
    try:
        for idx, frame in enumerate(driver.find_elements(By.TAG_NAME, "iframe")):
            try:
                driver.switch_to.frame(frame)
                if _find_login_fields(driver, quick=True)[0]:
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
        (By.ID, "usernameUserInput"),
        (By.NAME, "Email"), (By.NAME, "email"),
        (By.ID, "Email"), (By.ID, "email"),
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.CSS_SELECTOR, "input[id*='email']"),
        (By.XPATH, "//label[contains(., 'Email')]/following::input[1]"),
    ]
    last_err = None
    for by, key in email_candidates:
        try:
            email_el = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, key)))
            pwd_el = None
            for pby, pkey in [
                (By.NAME, "Password"), (By.NAME, "password"),
                (By.ID, "Password"), (By.ID, "password"),
                (By.CSS_SELECTOR, "input[type='password']"),
                (By.XPATH, "//label[contains(., 'Password') or contains(., 'Passwort')]/following::input[1]"),
            ]:
                try:
                    pwd_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((pby, pkey)))
                    break
                except Exception: continue
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
                except Exception: continue
            return email_el, pwd_el, submit_el
        except Exception as e:
            last_err = e
            continue
    if last_err:
        log(f"Login fields not found yet; retrying. Last error: {type(last_err).__name__}")
    return None, None, None

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
    password_el.clear(); password_el.send_keys(PASSWORD); password_el.send_keys(Keys.RETURN)
    time.sleep(5)
    try: driver.switch_to.default_content()
    except Exception: pass
    log("Submitted login.")

def _save_snap(driver, tag):
    if not DEBUG_SAVE_SCREENSHOTS: return
    try:
        path = f"/app/snap_{int(time.time())}_{tag}.png"
        driver.save_screenshot(path)
        log(f"Saved screenshot: {path}")
    except Exception as e:
        log(f"Failed to save screenshot: {e}")

def on_login_or_consent_page(driver) -> bool:
    u = (driver.current_url or "").lower()
    t = (driver.title or "").lower()
    return ("login" in u) or ("signin" in u) or ("sign in" in t) or ("anmelden" in t)

def open_pv(driver):
    log("Opening PV system page…")
    driver.get(PV_SYSTEM_URL)
    _try_accept_cookies(driver)
    # Wait for some power text or widget shell
    try:
        WebDriverWait(driver, LONG_WAIT).until(
            EC.any_of(
                EC.presence_of_element_located((By.ID, "powerWidget")),
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(),' W') or contains(text(),' kW')]")),
                EC.presence_of_element_located((By.TAG_NAME, "iframe"))
            )
        )
    except Exception:
        pass
    time.sleep(4)

    # Try to force-refresh first iframe (many SolarWeb dashboards embed live widget)
    try:
        iframe = driver.find_element(By.TAG_NAME, "iframe")
        driver.switch_to.frame(iframe)
        # Some sites need a reload to render live numbers on headless
        try:
            driver.execute_script("window.location.reload();")
            time.sleep(4)
        except Exception:
            pass
        driver.switch_to.default_content()
    except Exception:
        pass

    _save_snap(driver, "pv_loaded")
    log("✓ PV system page loaded.")

def _extract_by_xpath(driver) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    prod = cons = grid = None
    # Try main doc
    try:
        prod_el = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.XPATH, XPATH_PROD)))
        prod = _parse_watts_str(prod_el.text)
    except Exception: pass
    try:
        cons = _parse_watts_str(driver.find_element(By.XPATH, XPATH_CONS).text)
    except Exception: pass
    try:
        grid = _parse_watts_str(driver.find_element(By.XPATH, XPATH_GRID).text)
    except Exception: pass
    if prod is not None or cons is not None or grid is not None:
        return prod, cons, grid

    # Try inside first iframe as fallback
    try:
        iframe = driver.find_element(By.TAG_NAME, "iframe")
        driver.switch_to.frame(iframe)
        try:
            if prod is None:
                prod_el = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.XPATH, XPATH_PROD)))
                prod = _parse_watts_str(prod_el.text)
        except Exception: pass
        try:
            if cons is None:
                cons = _parse_watts_str(driver.find_element(By.XPATH, XPATH_CONS).text)
        except Exception: pass
        try:
            if grid is None:
                grid = _parse_watts_str(driver.find_element(By.XPATH, XPATH_GRID).text)
        except Exception: pass
    except Exception:
        pass
    finally:
        try: driver.switch_to.default_content()
        except Exception: pass

    return prod, cons, grid

def _extract_textwise(driver) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    data = driver.execute_script(JS_EXTRACT) or {}
    prod = data.get("production")
    grid = data.get("gridFeedIn")
    cons = data.get("consumption")
    if prod or grid or cons:
        return prod, cons, grid

    # Try inside iframe via script
    try:
        iframe = driver.find_element(By.TAG_NAME, "iframe")
        driver.switch_to.frame(iframe)
        data = driver.execute_script(JS_EXTRACT) or {}
        prod = data.get("production")
        grid = data.get("gridFeedIn")
        cons = data.get("consumption")
    except Exception:
        pass
    finally:
        try: driver.switch_to.default_content()
        except Exception: pass
    return prod, cons, grid

def scrape_once(driver):
    # If we got bounced to login, re-login and reopen PV
    if on_login_or_consent_page(driver):
        log("Detected login page; re-authenticating…")
        do_login_fronius(driver)
        open_pv(driver)

    # 1) Try resilient text parser (EN/DE) on doc & iframe
    prod, cons, grid = _extract_textwise(driver)

    # 2) Fallback to XPaths
    if prod is None or cons is None or grid is None:
        xp_prod, xp_cons, xp_grid = _extract_by_xpath(driver)
        if prod is None: prod = xp_prod
        if cons is None: cons = xp_cons
        if grid is None: grid = xp_grid

    # 3) If still nothing, try a gentle iframe refresh once
    if prod is None and cons is None and grid is None:
        try:
            iframe = driver.find_element(By.TAG_NAME, "iframe")
            driver.switch_to.frame(iframe)
            driver.execute_script("window.location.reload();")
            time.sleep(3)
            driver.switch_to.default_content()
            _save_snap(driver, "iframe_reloaded")
            prod, cons, grid = _extract_textwise(driver)
        except Exception:
            pass

    log(f"DEBUG: prod={prod} | grid={grid} | cons={cons}")
    return prod, grid, cons

def ha_set_state(entity_id, value):
    if not HA_TOKEN or not HA_URL: 
        log(f"HA not configured, skipping update: {entity_id}={value}")
        return
    if value is None: 
        return
    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "content-type": "application/json"}
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

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")

def run_dummy_server():
    try:
        HTTPServer(("0.0.0.0", 8080), DummyHandler).serve_forever()
    except Exception:
        pass

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    if not EMAIL or not PASSWORD:
        raise RuntimeError("EMAIL/PASSWORD not provided.")

    driver = make_driver()
    last = {"prod": None, "cons": None, "grid": None}
    last_login = time.time()

    try:
        # initial login + open dashboard
        do_login_fronius(driver)
        open_pv(driver)
        log(f"✓ Entering scrape loop every {SCRAPE_INTERVAL_SEC}s")

        while True:
            try:
                # periodic hard refresh/re-login (cookie/session hygiene)
                if (time.time() - last_login) > RELOGIN_MINUTES * 60:
                    log("Periodic refresh: re-opening PV page.")
                    driver.get(PV_SYSTEM_URL)
                    time.sleep(3)
                    if on_login_or_consent_page(driver):
                        do_login_fronius(driver)
                    open_pv(driver)
                    last_login = time.time()

                prod, grid, cons = scrape_once(driver)

                updated = False
                if prod is not None and prod != last.get("prod"): last["prod"] = int(prod); updated = True
                if cons is not None and cons != last.get("cons"): last["cons"] = int(cons); updated = True
                if grid is not None and grid != last.get("grid"): last["grid"] = int(grid); updated = True

                if updated:
                    ha_set_state(SENSOR_PRODUCTION,  last["prod"])
                    ha_set_state(SENSOR_CONSUMPTION, last["cons"])
                    ha_set_state(SENSOR_GRID,        last["grid"])
                else:
                    log("No fresh numbers this cycle; keeping last-good values.")

                # If we go N cycles with no numbers at all, try a page refresh
                if prod is None and cons is None and grid is None:
                    _save_snap(driver, "empty_cycle")
                    # gentle page refresh to kick widgets
                    driver.execute_script("location.reload();")
                    time.sleep(3)

            except Exception as e:
                log(f"✖ Scrape cycle error: {e}")
                traceback.print_exc()
                _save_snap(driver, "cycle_error")

            time.sleep(SCRAPE_INTERVAL_SEC)

    finally:
        log("Shutting down…")
        try: driver.quit()
        except Exception: pass
        log("Chrome closed.")

if __name__ == "__main__":
    main()
