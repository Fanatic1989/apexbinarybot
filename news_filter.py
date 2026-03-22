"""
Economic News Filter — Forex Factory (primary) + static fallback

FF JSON uses "country" for the currency code, not "currency".
Fetch interval raised to 12h to avoid 429 rate limiting.

Blocking rules:
  - Weekends:  synthetics always trade, forex always blocked
  - Weekdays:  synthetics blocked for HIGH impact only
  - Weekdays:  forex blocked for HIGH + MEDIUM if currency matches
"""

import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# FOREX FACTORY SCRAPER
# ─────────────────────────────────────────────────────────────

FF_URLS = {
    "thisweek": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "nextweek":  "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
}

FF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Referer":    "https://www.forexfactory.com/",
}


def fetch_forex_factory() -> List[Dict]:
    """
    Fetch this week + next week from Forex Factory's public JSON feed.

    IMPORTANT: FF JSON uses "country" as the currency code field, not "currency".
    e.g. {"country": "USD", "title": "NFP", "impact": "High", "date": "..."}

    nextweek 404s outside its window — silently skipped.
    429 rate limit — logged as warning, cached data remains in use.
    """
    events = []

    for key in ("thisweek", "nextweek"):
        try:
            r = requests.get(FF_URLS[key], headers=FF_HEADERS, timeout=10)

            if r.status_code == 404:
                log.debug(f"[FF] {key} not available (404) — skipping")
                continue

            if r.status_code == 429:
                log.warning(f"[FF] Rate limited (429) on {key} — using cached data")
                continue

            r.raise_for_status()
            raw = r.json()

            if not isinstance(raw, list):
                log.warning(f"[FF] {key} unexpected format: {type(raw)}")
                continue

            week_count = 0
            for item in raw:
                impact = item.get("impact", "")
                if impact not in ("High", "Medium"):
                    continue

                date_str = item.get("date", "")
                if not date_str:
                    continue

                try:
                    event_dt = datetime.fromisoformat(
                        date_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    log.debug(f"[FF] Bad date: {date_str}")
                    continue

                # FF uses "country" for the currency code (USD, EUR, GBP etc.)
                currency = (
                    item.get("country") or
                    item.get("currency") or
                    ""
                ).strip().upper()

                events.append({
                    "timestamp": event_dt,
                    "currency":  currency,
                    "impact":    impact,
                    "event":     item.get("title", item.get("name", "Unknown")),
                    "source":    "forex_factory",
                })
                week_count += 1

            log.info(f"[FF] {key}: {week_count} High/Medium events loaded")

        except Exception as e:
            log.warning(f"[FF] Fetch failed for {key}: {e}")

    return events


# ─────────────────────────────────────────────────────────────
# MAIN NEWS FILTER
# ─────────────────────────────────────────────────────────────

class NewsFilter:

    STATIC_WINDOWS = [
        (None, 7,  45, 8,  30, ["EUR", "GBP", "CHF"], "High",   "London Open"),
        (None, 12, 15, 14, 0,  ["USD", "CAD"],         "High",   "NY Open"),
        (0,    0,  0,  2,  0,  ["ALL"],                 "Medium", "Monday Open"),
        (1,    13, 30, 14, 30, ["USD"],                 "Medium", "USD Tuesday"),
        (2,    12, 0,  13, 0,  ["USD"],                 "High",   "ADP Employment"),
        (2,    18, 45, 20, 30, ["USD"],                 "High",   "FOMC Window"),
        (3,    12, 0,  13, 30, ["USD", "EUR"],          "High",   "ECB / Jobless Claims"),
        (4,    12, 0,  14, 0,  ["USD"],                 "High",   "NFP / Payrolls"),
        (4,    20, 0,  23, 59, ["ALL"],                 "Medium", "Friday Close"),
    ]

    MARKET_CURRENCIES = {
        "frxEURUSD": ["EUR", "USD"], "frxGBPUSD": ["GBP", "USD"],
        "frxUSDJPY": ["USD", "JPY"], "frxAUDUSD": ["AUD", "USD"],
        "frxUSDCAD": ["USD", "CAD"], "frxUSDCHF": ["USD", "CHF"],
        "frxEURGBP": ["EUR", "GBP"], "frxEURJPY": ["EUR", "JPY"],
        "frxGBPJPY": ["GBP", "JPY"], "frxXAUUSD": ["USD", "XAU"],
        "frxXAGUSD": ["USD", "XAG"],
    }

    MAJOR_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF"]

    def __init__(self):
        self._enabled:        bool               = True
        self._dynamic_events: List[Dict]         = []
        self._last_update:    Optional[datetime] = None
        self._lock            = threading.Lock()
        self._update_events()

    # ── FETCH ──────────────────────────────────────────────────

    def _update_events(self):
        try:
            events = fetch_forex_factory()

            if not events:
                # Don't wipe existing cache if fetch returned nothing (e.g. 429)
                if self._last_update is not None:
                    log.warning("[NEWS] FF returned nothing — keeping cached data")
                else:
                    log.warning("[NEWS] FF returned nothing — static fallback only")
                return

            with self._lock:
                self._dynamic_events = events
                self._last_update    = datetime.now(timezone.utc)

            log.info(f"[NEWS] Updated: {len(events)} events from Forex Factory")

        except Exception as e:
            log.error(f"[NEWS] Update error: {e}")

    def update_events_loop(self, interval_hours: float = 12):
        """Refresh every 12h — FF rate-limits aggressive polling."""
        while True:
            time.sleep(interval_hours * 3600)
            self._update_events()

    def start_background_updater(self):
        t = threading.Thread(target=self.update_events_loop, daemon=True)
        t.start()
        log.info("[NEWS] Background updater started (12h interval)")

    # ── WINDOW HELPERS ─────────────────────────────────────────

    def _event_to_window(self, event: Dict, now: datetime) -> Optional[tuple]:
        try:
            event_dt = event.get("timestamp")
            if not event_dt:
                return None

            buffer = 30 if event["impact"] == "High" else 15

            # Skip if the entire window (pre + post buffer) has passed
            if event_dt + timedelta(minutes=buffer) < now:
                return None

            start_dt = event_dt - timedelta(minutes=buffer)
            end_dt   = event_dt + timedelta(minutes=buffer)

            return (
                start_dt.hour * 60 + start_dt.minute,
                end_dt.hour   * 60 + end_dt.minute,
                event["currency"],
                event["impact"],
                event["event"],
            )

        except Exception as e:
            log.debug(f"[NEWS] Window error: {e}")
            return None

    @staticmethod
    def _is_within_window(start_min: int, end_min: int, now_min: int) -> bool:
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        return now_min >= start_min or now_min <= end_min

    # ── MAIN CHECK ─────────────────────────────────────────────

    def is_news_time(self, market: str) -> tuple:
        try:
            if not self._enabled:
                return False, ""

            now     = datetime.now(timezone.utc)
            weekday = now.weekday()
            now_min = now.hour * 60 + now.minute

            is_synth = not market.startswith("frx")
            is_wknd  = weekday >= 5

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

                if is_synth:
                    if impact == "High":
                        return True, f"📰 {title} (HIGH)"
                    continue

                if impact in ("High", "Medium") and cur in affected:
                    return True, f"📰 {title} ({impact})"

            # Static fallback
            for (wd, sh, sm, eh, em, currencies, impact, label) in self.STATIC_WINDOWS:
                if wd is not None and weekday != wd:
                    continue
                start = sh * 60 + sm
                end   = eh * 60 + em
                if not self._is_within_window(start, end, now_min):
                    continue
                if is_synth:
                    if label not in ("NFP / Payrolls", "FOMC Window"):
                        continue
                else:
                    if "ALL" not in currencies:
                        if not any(c in currencies for c in affected):
                            continue
                return True, f"📰 {label} ({impact}) [static]"

            return False, ""

        except Exception as e:
            log.debug(f"[NEWS] is_news_time error: {e}")
            return False, ""

    # ── UPCOMING EVENTS ────────────────────────────────────────

    def get_upcoming_events(self, hours: float = 8) -> List[Dict]:
        """
        Return upcoming High/Medium events within the next `hours`.
        Sorted soonest first, capped at 10.
        """
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        upcoming = []

        with self._lock:
            events = self._dynamic_events[:]

        for event in events:
            event_dt = event.get("timestamp")
            if not event_dt:
                continue
            if event_dt <= now or event_dt > cutoff:
                continue

            delta_mins = int((event_dt - now).total_seconds() / 60)
            upcoming.append({
                "title":     event["event"],
                "currency":  event["currency"],
                "impact":    event["impact"],
                "mins_away": delta_mins,
                "source":    event.get("source", "forex_factory"),
            })

        return sorted(upcoming, key=lambda x: x["mins_away"])[:10]

    def get_source_summary(self) -> Dict:
        with self._lock:
            events = self._dynamic_events[:]
        summary = {}
        for e in events:
            src = e.get("source", "unknown")
            summary[src] = summary.get(src, 0) + 1
        return {
            "total":       len(events),
            "by_source":   summary,
            "last_update": self._last_update.isoformat() if self._last_update else None,
        }

    def force_refresh(self):
        log.info("[NEWS] Force refresh triggered")
        self._update_events()

    def disable(self):
        self._enabled = False

    def enable(self):
        self._enabled = True


# ─────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────

news_filter = NewsFilter()
