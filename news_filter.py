"""
Economic News Filter — FMP API + static fallback

Rules:
- Weekends: synthetics always trade, forex always blocked
- Weekdays: synthetics blocked for HIGH impact news only
- Weekdays: forex blocked for HIGH + MEDIUM if currency matches
"""

import logging
import time
import threading
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

import requests

log = logging.getLogger(__name__)

# ------------------------------------------------------------
# FMP API FETCH
# ------------------------------------------------------------
def fetch_fmp_news():
    url = "https://financialmodelingprep.com/stable/economic-calendar"

    params = {
        "from": datetime.utcnow().strftime("%Y-%m-%d"),
        "to": (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d"),
        "apikey": os.getenv("FMP_API_KEY")
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()

        events = []

        currency_map = {
            "US": "USD", "United States": "USD",
            "UK": "GBP", "United Kingdom": "GBP",
            "EU": "EUR", "Euro Zone": "EUR",
            "Japan": "JPY",
            "Canada": "CAD",
            "Australia": "AUD",
            "Switzerland": "CHF",
            "China": "CNY"
        }

        for item in data:
            dt_str = item.get("date")
            if not dt_str:
                continue

            event_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))

            country = item.get("country", "")
            currency = currency_map.get(country, country)

            impact_raw = item.get("impact", "")

            if impact_raw in ["High", 3]:
                impact = "High"
            elif impact_raw in ["Medium", 2]:
                impact = "Medium"
            else:
                continue  # ignore low impact

            events.append({
                "timestamp": event_dt,
                "currency": currency,
                "impact": impact,
                "event": item.get("event", "")
            })

        return events

    except Exception as e:
        log.error(f"FMP fetch failed: {e}")
        return []


# ------------------------------------------------------------
# MAIN NEWS FILTER
# ------------------------------------------------------------
class NewsFilter:

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

    MARKET_CURRENCIES = {
        "frxEURUSD": ["EUR","USD"], "frxGBPUSD": ["GBP","USD"],
        "frxUSDJPY": ["USD","JPY"], "frxAUDUSD": ["AUD","USD"],
        "frxUSDCAD": ["USD","CAD"], "frxUSDCHF": ["USD","CHF"],
        "frxEURGBP": ["EUR","GBP"], "frxEURJPY": ["EUR","JPY"],
        "frxGBPJPY": ["GBP","JPY"], "frxXAUUSD": ["USD","XAU"],
        "frxXAGUSD": ["USD","XAG"],
    }

    MAJOR_CURRENCIES = ["USD","EUR","GBP","JPY","AUD","CAD","CHF"]

    def __init__(self):
        self._enabled = True
        self._dynamic_events = []
        self._last_update = None
        self._lock = threading.Lock()
        self._update_events()

    # ------------------------------------------------------------
    # UPDATE EVENTS
    # ------------------------------------------------------------
    def _update_events(self):
        try:
            events = fetch_fmp_news()

            with self._lock:
                self._dynamic_events = events
                self._last_update = datetime.now(timezone.utc)

            log.info(f"[NEWS] Updated events: {len(events)}")

        except Exception as e:
            log.error(f"[NEWS] Update failed: {e}")
            if self._last_update is None:
                log.warning("[NEWS] Using static fallback only")

    def update_events_loop(self, interval_hours=6):
        while True:
            self._update_events()
            time.sleep(interval_hours * 3600)

    def start_background_updater(self):
        t = threading.Thread(target=self.update_events_loop, daemon=True)
        t.start()

    # ------------------------------------------------------------
    # EVENT WINDOW
    # ------------------------------------------------------------
    def _event_to_window(self, event: Dict, now: datetime) -> Optional[tuple]:
        try:
            event_dt = event.get("timestamp")
            if not event_dt or event_dt < now:
                return None

            if event["impact"] == "High":
                buffer = 30
            elif event["impact"] == "Medium":
                buffer = 15
            else:
                return None

            start_dt = event_dt - timedelta(minutes=buffer)
            end_dt = event_dt + timedelta(minutes=buffer)

            start_min = start_dt.hour * 60 + start_dt.minute
            end_min = end_dt.hour * 60 + end_dt.minute

            return (start_min, end_min, event["currency"], event["impact"], event["event"])

        except Exception as e:
            log.debug(f"[NEWS] Window error: {e}")
            return None

    def _is_within_window(self, start_min, end_min, now_min):
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        return now_min >= start_min or now_min <= end_min

    # ------------------------------------------------------------
    # MAIN CHECK
    # ------------------------------------------------------------
    def is_news_time(self, market: str):
        try:
            if not self._enabled:
                return False, ""

            now = datetime.now(timezone.utc)
            weekday = now.weekday()
            now_min = now.hour * 60 + now.minute

            is_synth = not market.startswith("frx")
            is_wknd = weekday >= 5

            # Weekend rules
            if is_wknd:
                if is_synth:
                    return False, ""
                return True, "Weekend — forex closed"

            affected = self.MARKET_CURRENCIES.get(market, [])

            with self._lock:
                events = self._dynamic_events[:]

            for event in events:
                window = self._event_to_window(event, now)
                if not window:
                    continue

                start, end, cur, impact, title = window

                if cur not in self.MAJOR_CURRENCIES:
                    continue

                if not self._is_within_window(start, end, now_min):
                    continue

                # SYNTHETICS
                if is_synth:
                    if impact == "High":
                        return True, f"📰 {title} (HIGH)"
                    continue

                # FOREX
                if impact in ("High", "Medium"):
                    if cur in affected:
                        return True, f"📰 {title} ({impact})"

            # FALLBACK
            for (wd, sh, sm, eh, em, currencies, impact, label) in self.STATIC_WINDOWS:
                if wd is not None and weekday != wd:
                    continue

                start = sh * 60 + sm
                end = eh * 60 + em

                if not self._is_within_window(start, end, now_min):
                    continue

                if is_synth:
                    if label not in ["NFP / Payrolls", "FOMC Window"]:
                        continue
                else:
                    if "ALL" not in currencies:
                        if not any(c in currencies for c in affected):
                            continue

                return True, f"📰 {label} ({impact})"

            return False, ""

        except Exception as e:
            log.debug(f"[NEWS] Error: {e}")
            return False, ""

    # ------------------------------------------------------------
    # UPCOMING EVENTS
    # ------------------------------------------------------------
    def get_upcoming_events(self, hours=4):
        now = datetime.now(timezone.utc)
        upcoming = []

        with self._lock:
            events = self._dynamic_events

        for event in events:
            event_dt = event.get("timestamp")
            if not event_dt:
                continue

            delta = (event_dt - now).total_seconds() / 60

            if 0 < delta <= hours * 60:
                upcoming.append({
                    "title": event["event"],
                    "currency": event["currency"],
                    "impact": event["impact"],
                    "mins_away": int(delta)
                })

        return sorted(upcoming, key=lambda x: x["mins_away"])[:6]

    def disable(self):
        self._enabled = False

    def enable(self):
        self._enabled = True


# ------------------------------------------------------------
# SINGLETON
# ------------------------------------------------------------
news_filter = NewsFilter()
