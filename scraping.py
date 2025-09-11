import os
import time
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys


HA_URL = os.getenv("HA_URL", "http://100.67.69.31:8123")
HA_TOKEN = os.getenv("HA_TOKEN")
HA_PROXY_URL = os.getenv("HA_PROXY_URL")  # e.g. socks5h://127.0.0.1:1055

# New-style SolarWeb defaults (kept for compatibility)
LOGIN_URL = os.getenv("LOGIN_URL", "https://www.solarweb.com/Account/SignIn")
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")

# Old working flow env (Fronius login + direct PV page)
EMAIL = os.getenv("EMAIL")
PV_SYSTEM_URL = os.getenv(
    "PV_SYSTEM_URL",
    "https://www.solarweb.com/PvSystems/PvSystem?pvSystemId=a2064e7f-807b-4ec8-99e2-d271da292275"
)

# Sensor names in Home Assistant
SENSOR_PRODUCTION = "sensor.pv_production"
SENSOR_CONSUMPTION = "sensor.pv_consumption"
SENSOR_GRID = "sensor.grid_export"

# Optional proxy control
PROXY_URL = os.getenv("PROXY_URL")  # e.g. socks5://127.0.0.1:1055

# Scrape interval
SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "5"))
SHORT_WAIT, MED_WAIT, LONG_WAIT = 5, 20, 40


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def make_driver():
    log("Launching Chrome headless...")
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1366,960")
    opts.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36")

    # Only set proxy if explicitly provided
    if PROXY_URL:
        opts.add_argument(f"--proxy-server={PROXY_URL}")

    driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opts)
    return driver


def ha_set_state(entity_id, value):
    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "content-type": "application/json",
    }
    data = {"state": str(value)}
    try:
        kwargs = {"headers": headers, "json": data, "timeout": 10}
        if HA_PROXY_URL:
            # Per-request SOCKS proxy just for HA calls
            kwargs["proxies"] = {
                "http": HA_PROXY_URL,
                "https": HA_PROXY_URL,
            }
        r = requests.post(url, **kwargs)
        r.raise_for_status()
        log(f"✓ Updated {entity_id} = {value}")
    except Exception as e:
        log(f"⚠ Exception updating {entity_id}: {e}")


def _try_accept_cookies(driver):
    try:
        # common cookie buttons
        possible_selectors = [
            "button#onetrust-accept-btn-handler",
            "button[aria-label='Accept all']",
            "button[aria-label='Accept']",
            "button.cookie-btn-accept",
            "//button[contains(., 'Accept') or contains(., 'Agree')]",
        ]
        for sel in possible_selectors:
            try:
                if sel.startswith("//"):
                    el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, sel)))
                else:
                    el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                el.click()
                log("Accepted cookies banner")
                return
            except Exception:
                continue
    except Exception:
        pass


def _switch_to_login_iframe_if_any(driver):
    # Some sites render login inside an iframe
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for idx, frame in enumerate(iframes):
            try:
                driver.switch_to.frame(frame)
                # probe for a known field quickly
                if _find_login_fields(driver, quick=True):
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
    # Returns tuple (username_el, password_el, submit_el) or (None,None,None)
    timeout = 5 if quick else 30
    candidates = [
        (By.ID, "Username"),
        (By.NAME, "Username"),
        (By.NAME, "username"),
        (By.NAME, "Email"),
        (By.NAME, "email"),
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.CSS_SELECTOR, "input#email"),
    ]
    last_err = None
    for by, key in candidates:
        try:
            user_el = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, key)))
            # find password near by common selectors
            pass_el = None
            for pby, pkey in [
                (By.ID, "Password"),
                (By.NAME, "Password"),
                (By.NAME, "password"),
                (By.CSS_SELECTOR, "input[type='password']"),
            ]:
                try:
                    pass_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((pby, pkey)))
                    break
                except Exception:
                    continue
            # submit
            submit_el = None
            for sby, skey in [
                (By.ID, "submitButton"),
                (By.CSS_SELECTOR, "button[type='submit']"),
                (By.XPATH, "//button[contains(., 'Sign in') or contains(., 'Log in')]")
            ]:
                try:
                    submit_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((sby, skey)))
                    break
                except Exception:
                    continue
            return user_el, pass_el, submit_el
        except Exception as e:
            last_err = e
            continue
    if last_err:
        log(f"Login fields not found yet; retrying. Last error: {type(last_err).__name__}")
    return None, None, None


def do_login_solarweb(driver):
    log("Navigating to login page…")
    driver.get(LOGIN_URL)

    # Allow initial scripts to load
    WebDriverWait(driver, 60).until(lambda d: d.execute_script("return document.readyState") == "complete")

    _try_accept_cookies(driver)

    # Try in main document first; if not found, attempt iframes
    username_el, password_el, submit_el = _find_login_fields(driver)
    if not username_el:
        if _switch_to_login_iframe_if_any(driver):
            username_el, password_el, submit_el = _find_login_fields(driver)
        else:
            # still not found in default content; nothing to switch
            pass

    if not username_el:
        # as last resort, dump current URL and title for debugging
        log(f"Login form not detected. URL={driver.current_url} title={driver.title}")
        raise TimeoutError("Could not locate login fields on the page")

    username_el.clear()
    username_el.send_keys(USERNAME)
    if password_el:
        password_el.clear()
        password_el.send_keys(PASSWORD)
    if submit_el:
        submit_el.click()
    else:
        # try pressing Enter on password field
        password_el.submit()

    # Exit iframe if we were inside one so post-login waits operate on main page
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Wait for a clear post-login signal (dashboard or URL change)
    try:
        WebDriverWait(driver, 60).until(
            lambda d: "dashboard" in d.current_url.lower() or
                      d.find_elements(By.CSS_SELECTOR, ".dashboard, .main-dashboard, [data-testid='dashboard']")
        )
    except Exception:
        log(f"Post-login indicator not found yet. URL={driver.current_url}")


def scrape_values(driver):
    try:
        prod = cons = grid = None

        prod_el = driver.find_element(By.ID, "production-value")
        cons_el = driver.find_element(By.ID, "consumption-value")
        grid_el = driver.find_element(By.ID, "grid-value")

        if prod_el:
            prod = int(prod_el.text.replace(" W", "").replace(",", "").strip())
        if cons_el:
            cons = int(cons_el.text.replace(" W", "").replace(",", "").strip())
        if grid_el:
            grid = int(grid_el.text.replace(" W", "").replace(",", "").strip())

        return prod, cons, grid

    except Exception as e:
        log(f"⚠ Scrape error: {e}")
        return None, None, None


# ───────────────────────────────────────────────
# Fronius flow (working version you shared)
# ───────────────────────────────────────────────
def do_login_fronius(driver):
    log("Navigating to login page…")
    driver.get(os.getenv("FRONIUS_LOGIN_URL", "https://login.fronius.com"))
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
# Dummy HTTP server for liveness
# ───────────────────────────────────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def run_dummy_server():
    try:
        server = HTTPServer(("0.0.0.0", 8080), DummyHandler)
        server.serve_forever()
    except Exception:
        pass


def main():
    # Background liveness server
    threading.Thread(target=run_dummy_server, daemon=True).start()

    driver = make_driver()
    try:
        # Choose flow: prefer Fronius flow if EMAIL provided; else SolarWeb generic
        if EMAIL:
            do_login_fronius(driver)
            open_pv(driver)
            log(f"✓ Entering scrape loop every {SCRAPE_INTERVAL_SEC}s")
            while True:
                try:
                    prod, grid, cons = scrape_once(driver)
                    if prod is not None:
                        ha_set_state(SENSOR_PRODUCTION, prod)
                    if cons is not None:
                        ha_set_state(SENSOR_CONSUMPTION, cons)
                    if grid is not None:
                        ha_set_state(SENSOR_GRID, grid)
                except Exception as e:
                    log(f"✖ Fatal error: {e}")
                    traceback.print_exc()
                    break
                time.sleep(SCRAPE_INTERVAL_SEC)
        else:
            # SolarWeb generic
            do_login_solarweb(driver)
            log(f"✓ Entering scrape loop every {SCRAPE_INTERVAL_SEC}s")
            while True:
                try:
                    prod, cons, grid = scrape_values(driver)
                    log(f"DEBUG: prod={prod} | grid={grid} | cons={cons}")
                    if prod is not None:
                        ha_set_state(SENSOR_PRODUCTION, prod)
                    if cons is not None:
                        ha_set_state(SENSOR_CONSUMPTION, cons)
                    if grid is not None:
                        ha_set_state(SENSOR_GRID, grid)
                except Exception as e:
                    log(f"✖ Fatal error: {e}")
                    traceback.print_exc()
                    break
                time.sleep(SCRAPE_INTERVAL_SEC)

    finally:
        log("Interrupted. Exiting…")
        driver.quit()
        log("Chrome closed.")


if __name__ == "__main__":
    main()
