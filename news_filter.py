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
# Cache — refresh calendar every 4 hours
# ─────────────────────────────────────────
_news_cache     = []        # list of news events
_cache_ts       = 0
_CACHE_TTL      = 14400     # 4 hours
_BLOCK_BEFORE   = 15 * 60  # 15 min before news
_BLOCK_AFTER    = 15 * 60  # 15 min after news

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
        """Get economic calendar events. Cached for 4 hours."""
        now = time.time()
        if self._cache and (now - self._cache_ts) < _CACHE_TTL:
            return self._cache

        events = self._fetch_forexfactory()
        if events:
            self._cache    = events
            self._cache_ts = now
            log.info(f"[NEWS] Calendar updated — {len(events)} events loaded")
        elif not self._cache:
            # First fetch failed — try backup method
            events = self._fetch_backup()
            if events:
                self._cache    = events
                self._cache_ts = now

        return self._cache

    def _fetch_forexfactory(self) -> list:
        """Fetch ForexFactory RSS calendar."""
        try:
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 ApexBot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
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
