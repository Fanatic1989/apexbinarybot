"""
Economic News Filter

Fetches the economic calendar from ForexFactory and blocks
trading 15 minutes before and after high-impact news events.

Impact levels:
  🔴 Red    = HIGH impact   — always block
  🟠 Orange = MEDIUM impact — block for forex/commodities only
  🟡 Yellow = LOW impact    — ignore

No API key needed — uses ForexFactory public RSS feed.
"""
import json
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Module-level cache — shared across all calls
# Only fetches once per 4 hours regardless of
# how many markets are scanning simultaneously
# ─────────────────────────────────────────
_news_cache     = []
_cache_ts       = 0
_fetch_lock     = None   # set on first use
_CACHE_TTL      = 14400  # 4 hours
_RETRY_AFTER    = 3600   # if fetch fails, retry after 1 hour not every scan
_last_fail_ts   = 0
_BLOCK_BEFORE   = 15 * 60
_BLOCK_AFTER    = 15 * 60

# Currency to market mapping
CURRENCY_MARKETS = {
    "USD": ["frxEURUSD","frxGBPUSD","frxUSDJPY","frxUSDCAD","frxUSDCHF",
            "frxEURJPY","frxGBPJPY","frxXAUUSD","frxXAGUSD"],
    "EUR": ["frxEURUSD","frxEURGBP","frxEURJPY"],
    "GBP": ["frxGBPUSD","frxEURGBP","frxGBPJPY"],
    "JPY": ["frxUSDJPY","frxEURJPY","frxGBPJPY"],
    "AUD": ["frxAUDUSD"],
    "CAD": ["frxUSDCAD"],
    "CHF": ["frxUSDCHF"],
    "XAU": ["frxXAUUSD"],  # Gold affected by USD news too
    "XAG": ["frxXAGUSD"],
}


class NewsFilter:
    def __init__(self):
        self._cache    = []
        self._cache_ts = 0
        self._enabled  = True

    def is_news_time(self, market: str) -> tuple:
        """
        Check if current time is within news blackout window for this market.

        Returns (bool, str) — (is_blocked, reason)
        """
        if not self._enabled:
            return False, ""

        now = datetime.now(timezone.utc)
        events = self._get_events()

        # Determine which currencies affect this market
        affected_currencies = []
        for currency, markets in CURRENCY_MARKETS.items():
            if market in markets:
                affected_currencies.append(currency)

        # Synthetics — only block on major USD/global events
        is_synthetic = not market.startswith("frx")
        if is_synthetic:
            affected_currencies = ["USD"]  # synthetics only block on USD news

        for event in events:
            event_time = event.get("time")
            impact     = event.get("impact", "low")
            currency   = event.get("currency", "")
            title      = event.get("title", "")

            if not event_time:
                continue

            # Check if this event affects our market
            if currency not in affected_currencies:
                continue

            # Skip low impact for most markets
            if impact == "low":
                continue

            # Skip medium impact for synthetics
            if is_synthetic and impact == "medium":
                continue

            # Check time window
            time_to_news   = (event_time - now).total_seconds()
            time_since_news= (now - event_time).total_seconds()

            if -_BLOCK_BEFORE <= time_to_news <= 0:
                # News is coming up
                mins = abs(time_to_news) / 60
                reason = (f"⏰ {currency} {impact.upper()} news in {mins:.0f}m: {title}")
                log.info(f"[NEWS] {market} blocked — {reason}")
                return True, reason

            if 0 <= time_since_news <= _BLOCK_AFTER:
                # News just happened
                mins = time_since_news / 60
                reason = (f"📰 {currency} {impact.upper()} news {mins:.0f}m ago: {title}")
                log.info(f"[NEWS] {market} blocked — {reason}")
                return True, reason

        return False, ""

    def _get_events(self) -> list:
        """
        Get economic calendar events.
        Uses module-level cache shared across all instances.
        Only one HTTP fetch per 4 hours regardless of how many
        markets are scanning simultaneously.
        """
        global _news_cache, _cache_ts, _last_fail_ts, _fetch_lock
        import threading

        now = time.time()

        # Return cached data if still fresh
        if _news_cache and (now - _cache_ts) < _CACHE_TTL:
            return _news_cache

        # Don't retry too soon after a failure (wait 1 hour)
        if not _news_cache and (now - _last_fail_ts) < _RETRY_AFTER:
            return _news_cache

        # Thread lock — only ONE thread fetches at a time
        if _fetch_lock is None:
            _fetch_lock = threading.Lock()

        if not _fetch_lock.acquire(blocking=False):
            # Another thread is already fetching — return existing cache
            return _news_cache

        try:
            events = self._fetch_forexfactory()
            if events:
                _news_cache = events
                _cache_ts   = now
                log.info(f"[NEWS] Calendar updated — {len(events)} events loaded")
                # Save to disk for persistence across restarts
                self._save_cache(events)
            else:
                _last_fail_ts = now
                # Try loading from disk first
                disk = self._load_cache()
                if disk:
                    _news_cache = disk
                    _cache_ts   = now
                    log.info(f"[NEWS] Loaded {len(disk)} events from disk cache")
                elif not _news_cache:
                    backup = self._fetch_backup()
                    if backup:
                        _news_cache = backup
                        _cache_ts   = now
        finally:
            _fetch_lock.release()

        return _news_cache

    def _fetch_forexfactory(self) -> list:
        """Fetch economic calendar from public JSON source."""
        try:
            # Use the investing.com calendar API as primary source
            # Falls back to ForexFactory if needed
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; ApexBot/1.0)",
                    "Accept":     "application/json",
                    "Cache-Control": "no-cache"
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            events = []
            for item in data:
                impact = item.get("impact","").lower()
                if impact not in ("high","medium","low"):
                    continue
                if impact == "low":
                    continue

                # Parse time
                try:
                    date_str = item.get("date","")
                    time_str = item.get("time","")
                    if not date_str:
                        continue

                    # ForexFactory format: "03-17-2025" and "8:30am"
                    if time_str and time_str.lower() != "all day":
                        dt_str = f"{date_str} {time_str}"
                        event_time = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")
                        event_time = event_time.replace(tzinfo=timezone.utc)
                    else:
                        continue  # skip all-day events

                    events.append({
                        "title":    item.get("title",""),
                        "currency": item.get("country","").upper(),
                        "impact":   impact,
                        "time":     event_time,
                    })
                except Exception:
                    continue

            return events

        except Exception as e:
            log.warning(f"[NEWS] ForexFactory fetch failed: {e}")
            return []

    def _save_cache(self, events: list):
        """Save calendar to disk so it survives restarts."""
        try:
            cache_data = [
                {**e, "time": e["time"].isoformat()}
                for e in events if e.get("time")
            ]
            with open("news_cache.json", "w") as f:
                json.dump({"ts": time.time(), "events": cache_data}, f)
        except Exception as e:
            log.debug(f"[NEWS] Cache save failed: {e}")

    def _load_cache(self) -> list:
        """Load calendar from disk if fresh enough."""
        try:
            with open("news_cache.json") as f:
                data = json.load(f)
            # Only use if less than 12 hours old
            if time.time() - data.get("ts", 0) > 43200:
                return []
            events = []
            for e in data.get("events", []):
                try:
                    e["time"] = datetime.fromisoformat(e["time"])
                    events.append(e)
                except:
                    continue
            return events
        except:
            return []

    def _fetch_backup(self) -> list:
        """
        Backup: hardcode known high-impact recurring events.
        Used when calendar fetch fails.
        Only blocks on typical high-impact times (NFP Fridays, etc.)
        """
        now  = datetime.now(timezone.utc)
        hour = now.hour
        day  = now.weekday()  # 0=Monday, 4=Friday

        events = []

        # NFP — first Friday of month, 13:30 UTC
        if day == 4 and hour == 13:
            events.append({
                "title":    "Potential NFP / USD High Impact",
                "currency": "USD",
                "impact":   "high",
                "time":     now.replace(minute=30, second=0, microsecond=0)
            })

        # FOMC — typically Wednesdays 19:00 UTC (approximate)
        if day == 2 and 18 <= hour <= 20:
            events.append({
                "title":    "Potential FOMC",
                "currency": "USD",
                "impact":   "high",
                "time":     now.replace(hour=19, minute=0, second=0, microsecond=0)
            })

        return events

    def get_upcoming_events(self, hours: int = 4) -> list:
        """Return upcoming high-impact events in next N hours — for dashboard."""
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        events = self._get_events()
        upcoming = []
        for e in events:
            t = e.get("time")
            if t and now <= t <= cutoff:
                mins_away = (t - now).total_seconds() / 60
                upcoming.append({
                    "title":     e["title"],
                    "currency":  e["currency"],
                    "impact":    e["impact"],
                    "time_utc":  t.strftime("%H:%M UTC"),
                    "mins_away": round(mins_away)
                })
        return sorted(upcoming, key=lambda x: x["mins_away"])

    def disable(self):
        self._enabled = False
        log.info("[NEWS] News filter disabled")

    def enable(self):
        self._enabled = True
        log.info("[NEWS] News filter enabled")


# Global instance
news_filter = NewsFilter()
