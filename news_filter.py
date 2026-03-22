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
import os  # <-- added for binary path detection
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

    def _find_chrome_binary(self) -> str:
        """Return the path to Chrome/Chromium binary."""
        # First check environment variable (set in render.yaml)
        env_bin = os.environ.get('CHROME_BIN')
        if env_bin and os.path.exists(env_bin):
            return env_bin

        # Common locations on Linux
        candidates = [
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        # If not found, fallback to default (will raise error later)
        return "chromium-browser"

    def _fetch_via_selenium(self, week: str = "thisweek") -> List[Dict]:
        """Use Selenium to extract events when requests are blocked."""
        if not SELENIUM_AVAILABLE:
            log.error("Selenium not installed. Cannot fetch events.")
            raise RuntimeError("Selenium not available")

        chrome_bin = self._find_chrome_binary()
        log.info(f"Using Chrome binary: {chrome_bin}")

        options = uc.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.binary_location = chrome_bin

        driver = None
        try:
            driver = uc.Chrome(options=options)
            driver.get(self.CALENDAR_URL)
            wait = WebDriverWait(driver, 15)

            # Try to get JSON via export dropdown
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
            except Exception as e:
                log.warning(f"JSON export failed, falling back to table scrape: {e}")
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
            if driver:
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
# Main NewsFilter class (dynamic events)
# ------------------------------------------------------------
class NewsFilter:
    """
    Determines if a market is in a news blackout based on:
    - Weekends (forex blocked)
    - Dynamic events scraped from ForexFactory (forex blocked if currency affected & impact >= Medium)
    - Synthetics blocked only for High impact events (any currency)
    """

    # Static fallback windows (used only when no dynamic events available)
    STATIC_WINDOWS = [
        (None, 7, 45, 8, 30, ["EUR","GBP","CHF"], "high", "London Open"),
        (None, 12, 15, 14, 0, ["USD","CAD"], "high", "NY Open"),
        (0, 0, 0, 2, 0, ["ALL"], "medium", "Monday Open"),
        (1, 13, 30, 14, 30, ["USD"], "medium", "USD Tuesday"),
        (2, 12, 0, 13, 0, ["USD"], "high", "ADP Employment"),
        (2, 18, 45, 20, 30, ["USD"], "high", "FOMC Window"),
        (3, 12, 0, 13, 30, ["USD","EUR"], "high", "ECB / Jobless Claims"),
        (4, 12, 0, 14, 0, ["USD"], "high", "NFP / Payrolls"),
        (4, 20, 0, 23, 59, ["ALL"], "medium", "Friday Close"),
    ]

    # Map each forex pair to its two currencies
    MARKET_CURRENCIES = {
        "frxEURUSD": ["EUR","USD"], "frxGBPUSD": ["GBP","USD"],
        "frxUSDJPY": ["USD","JPY"], "frxAUDUSD": ["AUD","USD"],
        "frxUSDCAD": ["USD","CAD"], "frxUSDCHF": ["USD","CHF"],
        "frxEURGBP": ["EUR","GBP"], "frxEURJPY": ["EUR","JPY"],
        "frxGBPJPY": ["GBP","JPY"], "frxXAUUSD": ["USD","XAU"],
        "frxXAGUSD": ["USD","XAG"],
    }

    def __init__(self):
        self._enabled = True
        self._scraper = ForexFactoryScraper()
        self._dynamic_events = []          # list of event dicts for current week
        self._last_update = None
        self._lock = threading.Lock()
        self._update_events()              # initial fetch

    # ------------------------------------------------------------
    # Event updating (run periodically)
    # ------------------------------------------------------------
    def _update_events(self):
        """Fetch this week's events from ForexFactory and store them."""
        try:
            events = self._scraper.get_current_week()
            with self._lock:
                self._dynamic_events = events
                self._last_update = datetime.now(timezone.utc)
            log.info(f"Updated dynamic events: {len(events)} fetched")
        except Exception as e:
            log.error(f"Failed to update events: {e}")
            # Do not clear existing events; keep the last known data
            if self._last_update is None:
                log.warning("No events available, will use static windows.")

    def update_events_loop(self, interval_hours=24):
        """Run in a background thread to refresh events periodically."""
        while True:
            self._update_events()
            time.sleep(interval_hours * 3600)

    def start_background_updater(self):
        """Start a daemon thread that updates events every 24 hours."""
        t = threading.Thread(target=self.update_events_loop, daemon=True)
        t.start()

    # ------------------------------------------------------------
    # Blackout detection
    # ------------------------------------------------------------
    def _is_within_window(self, start_min: int, end_min: int, now_min: int) -> bool:
        """Check if now_min is inside [start, end] (handles overnight windows)."""
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        else:  # window crosses midnight
            return now_min >= start_min or now_min <= end_min

    def _event_to_window(self, event: Dict, now: datetime) -> Optional[tuple]:
        """
        Convert an event dict into a (start_min, end_min, currencies, impact) tuple
        if the event is within the next 24 hours.
        """
        time_str = event["time"]
        if time_str == "All Day":
            return None
        try:
            # Parse time like "12:30pm" -> 12:30 in 24h format
            t = datetime.strptime(time_str, "%I:%M%p").time()
            event_dt = datetime.combine(now.date(), t).replace(tzinfo=timezone.utc)
            # If the event time is earlier than now, assume it's for tomorrow (if within next 24h)
            if event_dt < now:
                event_dt += timedelta(days=1)
            # Create a window 15 minutes before and after the event
            start_min = (event_dt - timedelta(minutes=15)).hour * 60 + (event_dt - timedelta(minutes=15)).minute
            end_min   = (event_dt + timedelta(minutes=15)).hour * 60 + (event_dt + timedelta(minutes=15)).minute
            # If window crosses midnight, we handle it in _is_within_window
            return (start_min, end_min, event["currency"], event["impact"])
        except Exception as e:
            log.debug(f"Error parsing event time {time_str}: {e}")
            return None

    def is_news_time(self, market: str) -> tuple:
        """Return (blocked, reason) for the given market."""
        try:
            if not self._enabled:
                return False, ""

            now = datetime.now(timezone.utc)
            weekday = now.weekday()   # 0=Mon ... 5=Sat, 6=Sun
            is_synth = not market.startswith("frx")
            is_wknd = weekday >= 5

            # Weekend rules
            if is_wknd:
                if is_synth:
                    return False, ""   # synthetics open
                return True, "Weekend — forex closed"

            # Weekday: check dynamic events first
            now_min = now.hour * 60 + now.minute
            affected_currencies = self.MARKET_CURRENCIES.get(market, [])

            with self._lock:
                events = self._dynamic_events[:]   # copy

            for event in events:
                window = self._event_to_window(event, now)
                if not window:
                    continue
                start, end, cur, impact = window
                if not self._is_within_window(start, end, now_min):
                    continue

                # Synthetics: block only if impact is High
                if is_synth:
                    if impact == "High":
                        reason = f"📰 {event['event']} ({impact}) — 15‑min window"
                        log.info(f"[NEWS] {market} blocked — {reason}")
                        return True, reason
                    else:
                        continue

                # Forex: block if any currency matches and impact is at least Medium
                if impact in ("High", "Medium"):
                    # Event currency can be e.g., "USD", or "EUR,GBP" (comma separated)
                    event_currencies = [c.strip() for c in event["currency"].split(",")]
                    if any(c in event_currencies for c in affected_currencies):
                        reason = f"📰 {event['event']} ({impact}) — {cur}"
                        log.info(f"[NEWS] {market} blocked — {reason}")
                        return True, reason

            # Fallback: if no dynamic events apply, use static windows
            for (wd, sh, sm, eh, em, currencies, impact, label) in self.STATIC_WINDOWS:
                if wd is not None and weekday != wd:
                    continue
                if is_synth:
                    # Synthetics: only block for the specific labels originally defined
                    if label not in ["NFP / Payrolls", "FOMC Window", "ADP Employment"]:
                        continue
                else:
                    if "ALL" not in currencies:
                        if not any(c in currencies for c in affected_currencies):
                            continue
                start = sh * 60 + sm
                end = eh * 60 + em
                if start <= now_min <= end:
                    mins_left = end - now_min
                    reason = f"📰 {label} ({impact.upper()}) — resumes in {mins_left}m"
                    log.info(f"[NEWS] {market} blocked — {reason}")
                    return True, reason

            return False, ""

        except Exception as e:
            log.debug(f"[NEWS] error: {e}")
            return False, ""

    def get_upcoming_events(self, hours: int = 4) -> list:
        """Return upcoming dynamic events within the next `hours` hours."""
        now = datetime.now(timezone.utc)
        now_min = now.hour * 60 + now.minute
        upcoming = []
        with self._lock:
            events = self._dynamic_events
        for event in events:
            window = self._event_to_window(event, now)
            if not window:
                continue
            start, _, cur, impact = window
            if start > now_min and start - now_min <= hours * 60:
                upcoming.append({
                    "title": event["event"],
                    "currency": cur,
                    "impact": impact,
                    "time_utc": event["time"],
                    "mins_away": start - now_min
                })
        return sorted(upcoming, key=lambda x: x["mins_away"])[:6]

    def disable(self):
        """Disable news filtering."""
        self._enabled = False

    def enable(self):
        """Enable news filtering."""
        self._enabled = True


# Singleton instance for easy import
news_filter = NewsFilter()
