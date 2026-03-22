"""
Economic News Filter — Dynamic ForexFactory events + static fallback

Rules:
- Weekends: synthetics always trade, forex always blocked
- Weekdays: synthetics blocked for any high‑impact event
- Weekdays: forex blocked if any currency in the pair matches the event's currency and the event impact is High or Medium
"""
import logging
import time
import threading
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup

# Selenium imports (optional, only used if requests fail)
try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

log = logging.getLogger(__name__)

# ------------------------------------------------------------
# ForexFactory scraper (requests with Selenium fallback)
# ------------------------------------------------------------
class ForexFactoryScraper:
    """Fetches economic calendar events. Uses requests, falls back to Selenium."""

    BASE_URL = "https://www.forexfactory.com"
    CALENDAR_URL = f"{BASE_URL}/calendar"
    JSON_URL = f"{BASE_URL}/calendar/weekly-export.json"

    # Realistic Chrome 120 headers
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

    JSON_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Referer": CALENDAR_URL,
        "Origin": BASE_URL,
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

    def _get_cookies(self) -> bool:
        """Visit the calendar page to get cookies (establishes session)."""
        try:
            resp = self.session.get(self.CALENDAR_URL, timeout=15)
            resp.raise_for_status()
            time.sleep(1)
            return True
        except Exception as e:
            log.warning(f"Could not fetch calendar page for cookies: {e}")
            return False

    def _fetch_via_selenium(self, week: str = "thisweek") -> List[Dict]:
        """Use Selenium to extract events when requests are blocked."""
        if not SELENIUM_AVAILABLE:
            log.error("Selenium not installed. Cannot fetch events.")
            raise RuntimeError("Selenium not available")

        options = uc.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

        driver = uc.Chrome(options=options)
        try:
            driver.get(self.CALENDAR_URL)
            wait = WebDriverWait(driver, 15)

            # Click "Weekly Export" dropdown if needed (sometimes visible)
            try:
                export_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Weekly Export')]")))
                export_btn.click()
                json_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'JSON')]")))
                json_link.click()
                # Wait for new window/tab
                time.sleep(2)
                driver.switch_to.window(driver.window_handles[-1])
                json_text = driver.find_element(By.TAG_NAME, "pre").text
                data = json.loads(json_text)
            except:
                # Fallback: scrape table directly
                data = self._scrape_table_with_selenium(driver)

            events = []
            for item in data:
                impact_map = {3: "High", 2: "Medium", 1: "Low", 0: "Non-Economic"}
                impact = impact_map.get(item.get("impact", 0), "Unknown")
                events.append({
                    "time": item.get("time", ""),
                    "currency": item.get("currency", ""),
                    "impact": impact,
                    "event": item.get("title", ""),
                })
            return events
        finally:
            driver.quit()

    def _scrape_table_with_selenium(self, driver):
        """Fallback when JSON export fails: scrape table rows."""
        wait = WebDriverWait(driver, 10)
        table = wait.until(EC.presence_of_element_located((By.ID, "calendarTable")))
        rows = driver.find_elements(By.CSS_SELECTOR, "tr.calendar_row")
        events = []
        for row in rows:
            try:
                time_cell = row.find_element(By.CSS_SELECTOR, "td.calendar__time")
                currency_cell = row.find_element(By.CSS_SELECTOR, "td.calendar__currency")
                impact_cell = row.find_element(By.CSS_SELECTOR, "td.calendar__impact")
                event_cell = row.find_element(By.CSS_SELECTOR, "td.calendar__event")
                time_str = time_cell.text.strip()
                currency = currency_cell.text.strip()
                impact_img = impact_cell.find_element(By.TAG_NAME, "img")
                impact_alt = impact_img.get_attribute("alt")
                impact_map = {
                    "High Impact Expected": 3,
                    "Medium Impact Expected": 2,
                    "Low Impact Expected": 1,
                    "Non-Economic": 0,
                }
                impact_val = impact_map.get(impact_alt, 0)
                event = event_cell.text.strip()
                events.append({
                    "time": time_str,
                    "currency": currency,
                    "impact": impact_val,
                    "title": event,
                })
            except Exception:
                continue
        return events

    def get_week(self, week: str = "thisweek") -> List[Dict]:
        """Get events using requests first, fallback to Selenium if blocked."""
        # Try requests method first
        self._get_cookies()  # ensure cookies are set
        params = {"week": week, "timezone": "GMT"}
        json_headers = self.JSON_HEADERS.copy()
        json_headers.update(self.session.headers)

        max_retries = 2
        for attempt in range(max_retries):
            try:
                resp = self.session.get(self.JSON_URL, params=params, headers=json_headers, timeout=15)
                if resp.status_code == 200 and "application/json" in resp.headers.get("Content-Type", ""):
                    data = resp.json()
                    events = []
                    for item in data:
                        impact_map = {3: "High", 2: "Medium", 1: "Low", 0: "Non-Economic"}
                        impact = impact_map.get(item.get("impact", 0), "Unknown")
                        events.append({
                            "time": item.get("time", ""),
                            "currency": item.get("currency", ""),
                            "impact": impact,
                            "event": item.get("title", ""),
                        })
                    if events:
                        return events
                log.warning(f"Attempt {attempt+1}: status {resp.status_code}, content type {resp.headers.get('Content-Type')}")
            except Exception as e:
                log.warning(f"Attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)

        # If requests all fail, try Selenium
        log.warning("Requests failed, trying Selenium fallback...")
        return self._fetch_via_selenium(week)

    def get_current_week(self) -> List[Dict]:
        return self.get_week("thisweek")

    def get_next_week(self) -> List[Dict]:
        return self.get_week("nextweek")


# ------------------------------------------------------------
# Main NewsFilter class (unchanged except scraper integration)
# ------------------------------------------------------------
class NewsFilter:
    # ... (keep all previous methods exactly as they were, only the scraper is replaced)
