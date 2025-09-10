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

    # Force Chrome to use Tailscale’s SOCKS5 proxy
    opts.add_argument("--proxy-server=socks5://127.0.0.1:1055")

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


def do_login(driver):
    log("Navigating to login page…")
    driver.get(LOGIN_URL)

    log("Submitted login form.")
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.ID, "Username"))).send_keys(USERNAME)
    driver.find_element(By.ID, "Password").send_keys(PASSWORD)
    driver.find_element(By.ID, "submitButton").click()

    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".dashboard")))


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
