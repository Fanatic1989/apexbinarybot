"""
Economic News Filter — Forex Factory (primary) + FMP API (fallback) + static windows

Source priority:
  1. Forex Factory JSON feed  (free, no key, high quality)
  2. FMP API                  (requires FMP_API_KEY env var)
  3. Static fallback windows  (always active as last resort)

Blocking rules:
  - Weekends:  synthetics always trade, forex always blocked
  - Weekdays:  synthetics blocked for HIGH impact news only
  - Weekdays:  forex blocked for HIGH + MEDIUM if currency matches
"""

import logging
import time
import threading
import os
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

# FF uses title-case impact; keep only what we care about
FF_IMPACT_MAP = {"High": "High", "Medium": "Medium", "Low": "Low"}


def fetch_forex_factory() -> List[Dict]:
    """
    Fetch this week from Forex Factory's public JSON feed.
    Also attempts next week — but that URL only exists Mon–Fri of the
    preceding week, so a 404 is normal and silently skipped.

    Returns normalised event dicts with keys:
        timestamp (datetime, UTC), currency, impact, event (str), source
    """
    events = []

    for key in ("thisweek", "nextweek"):
        try:
            r = requests.get(FF_URLS[key], headers=FF_HEADERS, timeout=10)

            # nextweek returns 404 outside its availability window — that's fine
            if r.status_code == 404:
                log.debug(f"[FF] {key} not available yet (404) — skipping")
                continue

            r.raise_for_status()
            raw = r.json()

            for item in raw:
                impact_raw = item.get("impact", "")
                impact = FF_IMPACT_MAP.get(impact_raw)
                if impact not in ("High", "Medium"):
                    continue  # skip Low / Holiday

                date_str = item.get("date")
                if not date_str:
                    continue

                try:
                    # FF dates are ISO-8601 UTC, e.g. "2024-04-05T12:30:00+00:00"
                    event_dt = datetime.fromisoformat(
                        date_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    log.debug(f"[FF] Bad date format: {date_str}")
                    continue

                events.append({
                    "timestamp": event_dt,
                    "currency":  item.get("currency", ""),
                    "impact":    impact,
                    "event":     item.get("title", item.get("name", "")),
                    "source":    "forex_factory",
                })

        except Exception as e:
            log.warning(f"[FF] Fetch failed for {key}: {e}")

    log.info(f"[FF] Fetched {len(events)} High/Medium events")
    return events


# ─────────────────────────────────────────────────────────────
# FMP API FETCH  (fallback)
# ─────────────────────────────────────────────────────────────

FMP_CURRENCY_MAP = {
    "US": "USD", "United States": "USD",
    "UK": "GBP", "United Kingdom": "GBP",
    "EU": "EUR", "Euro Zone": "EUR",
    "Japan": "JPY",
    "Canada": "CAD",
    "Australia": "AUD",
    "Switzerland": "CHF",
    "China": "CNY",
}


def fetch_fmp_news() -> List[Dict]:
    """
    Fetch today + tomorrow from FMP economic calendar.
    Returns same normalised schema as fetch_forex_factory().
    Requires FMP_API_KEY environment variable.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        log.debug("[FMP] FMP_API_KEY not set — skipping FMP fetch")
        return []

    # v3 endpoint is available on the free FMP plan
    url = "https://financialmodelingprep.com/api/v3/economic_calendar"
    params = {
        "from":   datetime.utcnow().strftime("%Y-%m-%d"),
        "to":     (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d"),
        "apikey": api_key,
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log.error(f"[FMP] Fetch failed: {e}")
        return []

    events = []
    for item in raw:
        impact_raw = item.get("impact", "")
        if impact_raw in ("High", 3):
            impact = "High"
        elif impact_raw in ("Medium", 2):
            impact = "Medium"
        else:
            continue

        date_str = item.get("date")
        if not date_str:
            continue

        try:
            event_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        country  = item.get("country", "")
        currency = FMP_CURRENCY_MAP.get(country, country)

        events.append({
            "timestamp": event_dt,
            "currency":  currency,
            "impact":    impact,
            "event":     item.get("event", ""),
            "source":    "fmp",
        })

    log.info(f"[FMP] Fetched {len(events)} High/Medium events")
    return events


# ─────────────────────────────────────────────────────────────
# MERGE + DEDUPLICATE
# ─────────────────────────────────────────────────────────────

def _dedup_key(event: Dict) -> tuple:
    """Two events are the same if currency + rounded time match."""
    ts = event["timestamp"]
    # Round to nearest 5 minutes to handle minor time differences between sources
    rounded = ts.replace(second=0, microsecond=0, minute=(ts.minute // 5) * 5)
    return (event["currency"], rounded)


def merge_events(ff_events: List[Dict], fmp_events: List[Dict]) -> List[Dict]:
    """
    Merge FF and FMP events, preferring FF when both carry the same event.
    FF is preferred because it has better impact ratings and event titles.
    """
    seen = {}

    for event in ff_events:
        seen[_dedup_key(event)] = event

    for event in fmp_events:
        key = _dedup_key(event)
        if key not in seen:          # only add FMP events not already in FF
            seen[key] = event

    merged = sorted(seen.values(), key=lambda e: e["timestamp"])
    log.info(f"[NEWS] Merged total: {len(merged)} events ({len(ff_events)} FF + {len(fmp_events)} FMP, deduped)")
    return merged


# ─────────────────────────────────────────────────────────────
# MAIN NEWS FILTER
# ─────────────────────────────────────────────────────────────

class NewsFilter:

    # Static fallback windows — used when dynamic fetch returns nothing
    # (weekday, start_h, start_m, end_h, end_m, currencies, impact, label)
    STATIC_WINDOWS = [
        (None, 7,  45, 8,  30, ["EUR", "GBP", "CHF"], "high",   "London Open"),
        (None, 12, 15, 14, 0,  ["USD", "CAD"],         "high",   "NY Open"),
        (0,    0,  0,  2,  0,  ["ALL"],                 "medium", "Monday Open"),
        (1,    13, 30, 14, 30, ["USD"],                 "medium", "USD Tuesday"),
        (2,    12, 0,  13, 0,  ["USD"],                 "high",   "ADP Employment"),
        (2,    18, 45, 20, 30, ["USD"],                 "high",   "FOMC Window"),
        (3,    12, 0,  13, 30, ["USD", "EUR"],          "high",   "ECB / Jobless Claims"),
        (4,    12, 0,  14, 0,  ["USD"],                 "high",   "NFP / Payrolls"),
        (4,    20, 0,  23, 59, ["ALL"],                 "medium", "Friday Close"),
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
        self._enabled        = True
        self._dynamic_events: List[Dict] = []
        self._last_update:    Optional[datetime] = None
        self._lock           = threading.Lock()
        self._update_events()

    # ── FETCH & UPDATE ─────────────────────────────────────────

    def _update_events(self):
        """
        Fetch from Forex Factory first.
        If FF returns nothing (network error, rate-limit), fall back to FMP.
        Merge and store.
        """
        try:
            ff_events  = fetch_forex_factory()
            fmp_events = fetch_fmp_news()

            if not ff_events and not fmp_events:
                log.warning("[NEWS] Both sources returned empty — static fallback only")
                return

            merged = merge_events(ff_events, fmp_events)

            with self._lock:
                self._dynamic_events = merged
                self._last_update    = datetime.now(timezone.utc)

            sources = []
            if ff_events:  sources.append(f"FF:{len(ff_events)}")
            if fmp_events: sources.append(f"FMP:{len(fmp_events)}")
            log.info(f"[NEWS] Events updated — {' + '.join(sources)} → {len(merged)} merged")

        except Exception as e:
            log.error(f"[NEWS] Update error: {e}")
            if self._last_update is None:
                log.warning("[NEWS] No data yet — static fallback only")

    def update_events_loop(self, interval_hours: float = 6):
        while True:
            self._update_events()
            time.sleep(interval_hours * 3600)

    def start_background_updater(self):
        t = threading.Thread(target=self.update_events_loop, daemon=True)
        t.start()
        log.info("[NEWS] Background updater started (6h interval)")

    # ── WINDOW HELPERS ─────────────────────────────────────────

    def _event_to_window(self, event: Dict, now: datetime) -> Optional[tuple]:
        """Convert an event dict into a (start_min, end_min, currency, impact, title) window."""
        try:
            event_dt = event.get("timestamp")
            if not event_dt:
                return None

            # Skip events already more than their buffer in the past
            buffer = 30 if event["impact"] == "High" else 15
            if event_dt < now - timedelta(minutes=buffer):
                return None

            start_dt = event_dt - timedelta(minutes=buffer)
            end_dt   = event_dt + timedelta(minutes=buffer)

            return (
                start_dt.hour * 60 + start_dt.minute,
                end_dt.hour   * 60 + end_dt.minute,
                event["currency"],
                event["impact"],
                event["event"],
                event.get("source", "unknown"),
            )

        except Exception as e:
            log.debug(f"[NEWS] Window error: {e}")
            return None

    @staticmethod
    def _is_within_window(start_min: int, end_min: int, now_min: int) -> bool:
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        # Overnight window (wraps midnight)
        return now_min >= start_min or now_min <= end_min

    # ── MAIN CHECK ─────────────────────────────────────────────

    def is_news_time(self, market: str) -> tuple[bool, str]:
        """
        Returns (blocked: bool, reason: str).

        Synthetics (non-frx):  blocked on HIGH impact only, never on weekends
        Forex (frx*):          blocked on HIGH + MEDIUM if currency matches,
                               always blocked on weekends
        """
        try:
            if not self._enabled:
                return False, ""

            now     = datetime.now(timezone.utc)
            weekday = now.weekday()
            now_min = now.hour * 60 + now.minute

            is_synth = not market.startswith("frx")
            is_wknd  = weekday >= 5

            # ── Weekend rules ──────────────────────────────────
            if is_wknd:
                if is_synth:
                    return False, ""
                return True, "Weekend — forex closed"

            affected = self.MARKET_CURRENCIES.get(market, [])

            # ── Dynamic events (FF + FMP) ──────────────────────
            with self._lock:
                events = self._dynamic_events[:]

            for event in events:
                window = self._event_to_window(event, now)
                if not window:
                    continue

                start, end, cur, impact, title, source = window

                if cur not in self.MAJOR_CURRENCIES:
                    continue

                if not self._is_within_window(start, end, now_min):
                    continue

                if is_synth:
                    if impact == "High":
                        return True, f"📰 {title} (HIGH) [{source}]"
                    continue

                # Forex — HIGH or MEDIUM if currency matches
                if impact in ("High", "Medium") and cur in affected:
                    return True, f"📰 {title} ({impact}) [{source}]"

            # ── Static fallback ────────────────────────────────
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

    def get_upcoming_events(self, hours: float = 4) -> List[Dict]:
        """Return next N hours of High/Medium events, sorted by time."""
        now = datetime.now(timezone.utc)
        upcoming = []

        with self._lock:
            events = self._dynamic_events[:]

        for event in events:
            event_dt = event.get("timestamp")
            if not event_dt:
                continue

            delta_mins = (event_dt - now).total_seconds() / 60
            if 0 < delta_mins <= hours * 60:
                upcoming.append({
                    "title":     event["event"],
                    "currency":  event["currency"],
                    "impact":    event["impact"],
                    "mins_away": int(delta_mins),
                    "source":    event.get("source", "unknown"),
                })

        return sorted(upcoming, key=lambda x: x["mins_away"])[:6]

    def get_source_summary(self) -> Dict:
        """Diagnostic helper — shows event counts per source."""
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

    # ── CONTROLS ───────────────────────────────────────────────

    def disable(self):
        self._enabled = False
        log.info("[NEWS] Filter disabled")

    def enable(self):
        self._enabled = True
        log.info("[NEWS] Filter enabled")

    def force_refresh(self):
        """Manually trigger an immediate data refresh."""
        log.info("[NEWS] Force refresh triggered")
        self._update_events()


# ─────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────

news_filter = NewsFilter()
