"""
Economic News Filter — Time-Based Blackout System

Blocks trading during known high-impact economic news windows.
No external API calls — zero rate limiting, zero downtime risk.

For forex pairs: blocks during major data release windows
For synthetics: only blocks during extreme USD events (NFP, FOMC)
"""
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Blackout windows — UTC times
# ─────────────────────────────────────────
# Format: (weekday, start_hour, start_min, end_hour, end_min, currencies, impact, label)
# weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, None=every day

BLACKOUT_WINDOWS = [
    # ── Daily windows (every trading day) ───────────────────────
    # London open — EUR/GBP spike
    (None, 7, 45, 8, 30,  ["EUR","GBP","CHF"],      "high",   "London Open"),
    # NY open — USD data releases cluster here
    (None, 12, 15, 14, 0, ["USD","CAD"],             "high",   "NY Open"),

    # ── Monday ───────────────────────────────────────────────────
    (0, 0, 0, 2, 0,       ["USD","EUR","GBP","JPY"], "medium", "Monday Market Open"),

    # ── Tuesday ──────────────────────────────────────────────────
    (1, 9, 0, 10, 0,      ["GBP"],                  "medium", "UK Data Tuesday"),
    (1, 13, 30, 14, 30,   ["USD"],                  "medium", "USD Tuesday Data"),

    # ── Wednesday ────────────────────────────────────────────────
    (2, 12, 0, 13, 0,     ["USD"],                  "high",   "ADP Employment"),
    (2, 18, 45, 20, 30,   ["USD"],                  "high",   "FOMC Window"),

    # ── Thursday ─────────────────────────────────────────────────
    (3, 12, 0, 13, 30,    ["USD","EUR"],             "high",   "ECB / Jobless Claims"),

    # ── Friday ───────────────────────────────────────────────────
    (4, 12, 0, 14, 0,     ["USD"],                  "high",   "NFP / Payrolls"),
    (4, 20, 0, 23, 59,    ["ALL"],                  "medium", "Friday Close Volatility"),
]

# Currencies that affect each market
MARKET_CURRENCIES = {
    "frxEURUSD": ["EUR","USD"],
    "frxGBPUSD": ["GBP","USD"],
    "frxUSDJPY": ["USD","JPY"],
    "frxAUDUSD": ["AUD","USD"],
    "frxUSDCAD": ["USD","CAD"],
    "frxUSDCHF": ["USD","CHF"],
    "frxEURGBP": ["EUR","GBP"],
    "frxEURJPY": ["EUR","JPY"],
    "frxGBPJPY": ["GBP","JPY"],
    "frxXAUUSD": ["USD","XAU"],
    "frxXAGUSD": ["USD","XAG"],
}

_enabled = True


class NewsFilter:

    def is_news_time(self, market: str) -> tuple:
        """
        Returns (True, reason) if market should be blocked.
        Returns (False, "") if safe to trade.
        Never raises an exception.
        """
        global _enabled
        try:
            if not _enabled:
                return False, ""

            now     = datetime.now(timezone.utc)
            weekday = now.weekday()
            hour    = now.hour
            minute  = now.minute
            now_min = hour * 60 + minute

            # Get currencies that affect this market
            affected = MARKET_CURRENCIES.get(market, [])
            is_synth = not market.startswith("frx")

            for (wd, sh, sm, eh, em, currencies, impact, label) in BLACKOUT_WINDOWS:
                # Check weekday
                if wd is not None and weekday != wd:
                    continue

                # Check if this window affects our market
                if "ALL" not in currencies:
                    if is_synth:
                        # Synthetics only block on HIGH impact USD events
                        if impact != "high" or "USD" not in currencies:
                            continue
                    else:
                        # Forex: check if any affected currency matches
                        if not any(c in currencies for c in affected):
                            continue

                # Check time window
                start_min = sh * 60 + sm
                end_min   = eh * 60 + em

                if start_min <= now_min <= end_min:
                    mins_left = end_min - now_min
                    reason = (f"📰 {label} ({impact.upper()}) — "
                              f"resumes in {mins_left}m")
                    log.info(f"[NEWS] {market} blocked — {reason}")
                    return True, reason

            return False, ""

        except Exception as e:
            log.debug(f"[NEWS] Filter error: {e}")
            return False, ""

    def get_upcoming_events(self, hours: int = 4) -> list:
        """Return upcoming blackout windows for dashboard display."""
        try:
            now     = datetime.now(timezone.utc)
            weekday = now.weekday()
            now_min = now.hour * 60 + now.minute
            cutoff  = now_min + (hours * 60)
            upcoming = []

            for (wd, sh, sm, eh, em, currencies, impact, label) in BLACKOUT_WINDOWS:
                # Check next 2 days
                for day_offset in range(3):
                    check_wd = (weekday + day_offset) % 7
                    if wd is not None and check_wd != wd:
                        continue

                    start_abs = day_offset * 1440 + sh * 60 + sm
                    if now_min < start_abs <= cutoff:
                        mins_away = start_abs - now_min
                        upcoming.append({
                            "title":    label,
                            "currency": ", ".join(currencies[:3]),
                            "impact":   impact,
                            "time_utc": f"{sh:02d}:{sm:02d} UTC",
                            "mins_away": mins_away
                        })
                        break

            return sorted(upcoming, key=lambda x: x["mins_away"])[:6]
        except:
            return []

    def disable(self):
        global _enabled
        _enabled = False
        log.info("[NEWS] Filter disabled")

    def enable(self):
        global _enabled
        _enabled = True
        log.info("[NEWS] Filter enabled")


# Global instance
news_filter = NewsFilter()
