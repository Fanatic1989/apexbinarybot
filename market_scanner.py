import logging
import time
from datetime import datetime

import config
from deriv_api import get_candles
from strategy import analyze_market

log = logging.getLogger(__name__)


# ─────────────────────────────────────────
# Single market scan
# ─────────────────────────────────────────
def scan_market(market: str) -> dict:
    """
    Scan a single market and return a signal dict or None.

    Returns:
        {
            "market":      str,
            "direction":   "CALL" | "PUT",
            "confidence":  "high" | "normal",
            "expiry":      int  (minutes),
            "timestamp":   str
        }
        or None if no signal.
    """
    try:
        candles = get_candles(market)

        if not candles or len(candles) < 30:
            log.warning(f"[SCANNER] {market} — insufficient candle data "
                        f"({len(candles) if candles else 0} candles). Skipping.")
            return None

        signal = analyze_market(candles, market)

        if not signal or signal.get("direction") == "NONE":
            log.debug(f"[SCANNER] {market} — no signal.")
            return None

        result = {
            "market":     market,
            "direction":  signal["direction"],
            "confidence": signal.get("confidence", "normal"),
            "expiry":     config.get_expiry(market),
            "timestamp":  datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        }

        log.info(
            f"[SCANNER] ⚡ {market} | {result['direction']} | "
            f"Confidence: {result['confidence']} | Expiry: {result['expiry']}m"
        )
        return result

    except Exception as e:
        log.error(f"[SCANNER] Error scanning {market}: {e}", exc_info=True)
        return None


# ─────────────────────────────────────────
# Full market scan — all markets
# ─────────────────────────────────────────
def scan_all_markets(markets: list = None, delay: float = 0.5) -> list:
    """
    Scan all markets sequentially.

    Args:
        markets : list of market symbols (defaults to config.MARKETS)
        delay   : seconds to wait between each market scan
                  (avoids hammering the Deriv WebSocket)

    Returns:
        List of signal dicts for markets that produced a signal.
    """
    markets  = markets or config.MARKETS
    signals  = []
    total    = len(markets)
    scanned  = 0
    skipped  = 0

    log.info(f"[SCANNER] Starting full scan — {total} markets")

    for market in markets:
        result = scan_market(market)
        scanned += 1

        if result:
            signals.append(result)
        else:
            skipped += 1

        # Small delay between markets to avoid WebSocket rate limiting
        if delay > 0 and scanned < total:
            time.sleep(delay)

    # ── Scan summary ─────────────────────
    log.info(
        f"[SCANNER] Scan complete — "
        f"{total} scanned | {len(signals)} signals | {skipped} no-signal"
    )

    if signals:
        log.info("[SCANNER] Signals found:")
        for s in signals:
            log.info(
                f"  → {s['market']:12s} | {s['direction']:4s} | "
                f"Confidence: {s['confidence']:6s} | Expiry: {s['expiry']}m"
            )

    return signals


# ─────────────────────────────────────────
# Scan summary report (for dashboard/logs)
# ─────────────────────────────────────────
def build_scan_report(signals: list, scan_number: int) -> dict:
    """
    Build a structured report from a scan's signal list.
    Used by the Flask dashboard and Telegram reporting.
    """
    calls   = [s for s in signals if s["direction"] == "CALL"]
    puts    = [s for s in signals if s["direction"] == "PUT"]
    high    = [s for s in signals if s["confidence"] == "high"]

    return {
        "scan_number":   scan_number,
        "timestamp":     datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total_signals": len(signals),
        "calls":         len(calls),
        "puts":          len(puts),
        "high_conf":     len(high),
        "signals":       signals
    }


# ─────────────────────────────────────────
# Entry point (manual test run)
# ─────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    print("\n── Manual market scan ──\n")
    found = scan_all_markets()
    print(f"\nTotal signals: {len(found)}")
    for sig in found:
        print(sig)
