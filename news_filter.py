"""
Economic News Filter — Time-Based Blackout

Rules:
- Weekends: synthetics always trade, forex always blocked
- Weekdays: synthetics only blocked for NFP/FOMC/ADP (major events)
- Weekdays: forex blocked during relevant currency windows
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

BLACKOUT_WINDOWS = [
    # (weekday, start_h, start_m, end_h, end_m, currencies, impact, label)
    # None = every weekday
    (None, 7,  45, 8,  30, ["EUR","GBP","CHF"], "high",   "London Open"),
    (None, 12, 15, 14,  0, ["USD","CAD"],        "high",   "NY Open"),
    (0,    0,   0, 2,   0, ["ALL"],              "medium", "Monday Open"),
    (1,   13,  30, 14, 30, ["USD"],              "medium", "USD Tuesday"),
    (2,   12,   0, 13,  0, ["USD"],              "high",   "ADP Employment"),
    (2,   18,  45, 20, 30, ["USD"],              "high",   "FOMC Window"),
    (3,   12,   0, 13, 30, ["USD","EUR"],         "high",   "ECB / Jobless Claims"),
    (4,   12,   0, 14,  0, ["USD"],              "high",   "NFP / Payrolls"),
    (4,   20,   0, 23, 59, ["ALL"],              "medium", "Friday Close"),
]

MARKET_CURRENCIES = {
    "frxEURUSD":["EUR","USD"], "frxGBPUSD":["GBP","USD"],
    "frxUSDJPY":["USD","JPY"], "frxAUDUSD":["AUD","USD"],
    "frxUSDCAD":["USD","CAD"], "frxUSDCHF":["USD","CHF"],
    "frxEURGBP":["EUR","GBP"], "frxEURJPY":["EUR","JPY"],
    "frxGBPJPY":["GBP","JPY"], "frxXAUUSD":["USD","XAU"],
    "frxXAGUSD":["USD","XAG"],
}

# Synthetic indices only block on these major scheduled events
SYNTH_BLOCK_EVENTS = ["NFP / Payrolls", "FOMC Window", "ADP Employment"]

_enabled = True


class NewsFilter:

    def is_news_time(self, market: str) -> tuple:
        try:
            global _enabled
            if not _enabled:
                return False, ""

            now     = datetime.now(timezone.utc)
            weekday = now.weekday()   # 0=Mon ... 5=Sat, 6=Sun
            is_synth= not market.startswith("frx")
            is_wknd = weekday >= 5

            # ── Weekend rules ─────────────────────────────────────
            if is_wknd:
                if is_synth:
                    return False, ""  # synthetics always open
                return True, "Weekend — forex closed"

            # ── Weekday rules ─────────────────────────────────────
            now_min  = now.hour * 60 + now.minute
            affected = MARKET_CURRENCIES.get(market, [])

            for (wd, sh, sm, eh, em, currencies, impact, label) in BLACKOUT_WINDOWS:
                if wd is not None and weekday != wd:
                    continue

                if is_synth:
                    # Synthetics: only block for major scheduled events
                    if label not in SYNTH_BLOCK_EVENTS:
                        continue
                    if impact != "high":
                        continue
                else:
                    # Forex: check currency match
                    if "ALL" not in currencies:
                        if not any(c in currencies for c in affected):
                            continue

                start = sh * 60 + sm
                end   = eh * 60 + em

                if start <= now_min <= end:
                    mins_left = end - now_min
                    reason    = f"📰 {label} ({impact.upper()}) — resumes in {mins_left}m"
                    log.info(f"[NEWS] {market} blocked — {reason}")
                    return True, reason

            return False, ""

        except Exception as e:
            log.debug(f"[NEWS] error: {e}")
            return False, ""

    def get_upcoming_events(self, hours: int = 4) -> list:
        try:
            now     = datetime.now(timezone.utc)
            weekday = now.weekday()
            if weekday >= 5:
                return []   # no events on weekends
            now_min = now.hour * 60 + now.minute
            cutoff  = now_min + hours * 60
            out     = []
            for (wd, sh, sm, eh, em, currencies, impact, label) in BLACKOUT_WINDOWS:
                if wd is not None and wd != weekday:
                    continue
                start = sh * 60 + sm
                if now_min < start <= cutoff:
                    out.append({
                        "title":    label,
                        "currency": ", ".join(currencies[:3]),
                        "impact":   impact,
                        "time_utc": f"{sh:02d}:{sm:02d} UTC",
                        "mins_away": start - now_min
                    })
            return sorted(out, key=lambda x: x["mins_away"])[:6]
        except:
            return []

    def disable(self):
        global _enabled; _enabled = False
    def enable(self):
        global _enabled; _enabled = True


news_filter = NewsFilter()
