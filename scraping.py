import os
import time
import logging
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

HA_URL = os.getenv("HA_URL", "http://100.67.69.31:8123")
HA_TOKEN = os.getenv("HA_TOKEN")
HA_PROXY_URL = os.getenv("HA_PROXY_URL")

EMAIL = os.getenv("EMAIL")
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")

FRONIUS_LOGIN_URL = os.getenv("FRONIUS_LOGIN_URL", "https://login.fronius.com")
LOGIN_URL = os.getenv("LOGIN_URL", "https://www.solarweb.com/Account/SignIn")
PV_SYSTEM_URL = os.getenv(
    "PV_SYSTEM_URL",
    "https://www.solarweb.com/PvSystems/PvSystem?pvSystemId=55576a9d-f6c4-455a-8994-b77eecb99e6c"
)

SCRAPE_INTERVAL_SEC = int(os.getenv("SCRAPE_INTERVAL_SEC", "5"))

SENSORS = {
    "pv_production": "sensor.pv_production",
    "pv_production2": "sensor.pv_production2",
    "grid_export": "sensor.grid_export"
}

HEADLESS = True

# --- helpers ---

def ha_set_state(entity_id, value, unit="W"):
    """Post a value to Home Assistant sensor."""
    if value is None:
        value = 0  # <--- convert None → 0
    try:
        proxies = {}
        if HA_PROXY_URL:
            proxies = {"http": HA_PROXY_URL, "https": HA_PROXY_URL}

        url = f"{HA_URL}/api/states/{entity_id}"
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "content-type": "application/json",
        }
        data = {"state": str(value), "attributes": {"unit_of_measurement": unit}}
        resp = requests.post(url, headers=headers, json=data, proxies=proxies, timeout=10)
        resp.raise_for_status()
        logging.info(f"✓ Updated {entity_id} = {value}")
    except Exception as e:
        logging.error(f"✗ Failed to update {entity_id}: {e}")


def get_driver():
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)
    return driver


def login_and_open_system(driver):
    if EMAIL:
        logging.info("Using Fronius login flow")
        driver.get(FRONIUS_LOGIN_URL)
        time.sleep(3)
        driver.find_element(By.ID, "signInName").send_keys(EMAIL)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "next").click()
        time.sleep(5)
        driver.get(PV_SYSTEM_URL)
    else:
        logging.info("Using SolarWeb login flow")
        driver.get(LOGIN_URL)
        time.sleep(3)
        driver.find_element(By.ID, "Username").send_keys(USERNAME)
        driver.find_element(By.ID, "Password").send_keys(PASSWORD)
        driver.find_element(By.ID, "loginButton").click()
        time.sleep(5)

    logging.info("✓ PV system page loaded.")


def extract_values(driver):
    """Scrape production, consumption, grid. Return ints (None→0 handled later)."""
    try:
        prod_elem = driver.find_element(By.ID, "production-power-now")
        prod = int(prod_elem.text.replace("W", "").replace(",", "").strip())
    except Exception:
        prod = None

    try:
        cons_elem = driver.find_element(By.ID, "consumption-power-now")
        cons = int(cons_elem.text.replace("W", "").replace(",", "").strip())
    except Exception:
        cons = None

    try:
        grid_elem = driver.find_element(By.ID, "grid-power-now")
        grid = int(grid_elem.text.replace("W", "").replace(",", "").strip())
    except Exception:
        grid = None

    return prod, cons, grid


def main():
    driver = get_driver()
    try:
        login_and_open_system(driver)
        logging.info(f"✓ Entering scrape loop every {SCRAPE_INTERVAL_SEC}s")

        while True:
            prod, cons, grid = extract_values(driver)

            # None → 0 fallback
            prod = prod if prod is not None else 0
            cons = cons if cons is not None else 0
            grid = grid if grid is not None else 0

            logging.debug(f"prod={prod} | grid={grid} | cons={cons}")

            ha_set_state(SENSORS["pv_production"], prod)
            ha_set_state(SENSORS["pv_production2"], cons)
            ha_set_state(SENSORS["grid_export"], grid)

            time.sleep(SCRAPE_INTERVAL_SEC)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
