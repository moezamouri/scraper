import os
import time
import traceback
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


HA_URL = os.getenv("HA_URL", "http://100.67.69.31:8123")
HA_TOKEN = os.getenv("HA_TOKEN")
LOGIN_URL = os.getenv("LOGIN_URL", "https://www.solarweb.com/Account/SignIn")
USERNAME = os.getenv("USERNAME", "magnus.moehrlein@gmail.com")
PASSWORD = os.getenv("PASSWORD", "Magnus2003!")

# Sensor names in Home Assistant
SENSOR_PRODUCTION = "sensor.pv_production"
SENSOR_CONSUMPTION = "sensor.pv_production2"
SENSOR_GRID = "sensor.grid_export"

# Optional proxy control
PROXY_URL = os.getenv("PROXY_URL")  # e.g. socks5://127.0.0.1:1055


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
        r = requests.post(url, headers=headers, json=data, timeout=10)
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


def do_login(driver):
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


def main():
    driver = make_driver()
    try:
        do_login(driver)
        log("✓ Entering scrape loop every 5s")

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

            time.sleep(5)

    finally:
        log("Interrupted. Exiting…")
        driver.quit()
        log("Chrome closed.")


if __name__ == "__main__":
    main()
