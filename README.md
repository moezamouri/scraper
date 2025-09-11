Scraper:

Small, containerized scraper that logs into Fronius / SolarWeb, extracts three live values

PV production

House consumption

Grid export (+ / import −)

…and publishes them to Home Assistant via its REST API.

It’s designed to run on Railway and reach your Home Assistant that’s on a remote Raspberry Pi over Tailscale (userspace, SOCKS5 only for HA calls). The scraper uses Selenium (Chromium) and runs headless.

--------------------------------------------------------------------------------------------------------------------------------------

How it works

The container starts tailscaled in userspace and exposes a local SOCKS5 proxy on 127.0.0.1:1055.

Selenium (headless Chromium) logs in to SolarWeb / Fronius and opens your PV system page.

Every SCRAPE_INTERVAL_SEC seconds the script extracts the three numbers from the page.

Only the HTTP calls to Home Assistant are sent through the SOCKS5 proxy, so scraping goes straight to the internet while HA traffic goes through Tailscale.

--------------------------------------------------------------------------------------------------------------------------------------

Development notes

Headless Chrome flags: --headless=new, --no-sandbox, --disable-dev-shm-usage.

Default scrape cadence: SCRAPE_INTERVAL_SEC=5.

HA calls are posted with a 10s timeout to /api/states/<entity_id> using the provided token.
