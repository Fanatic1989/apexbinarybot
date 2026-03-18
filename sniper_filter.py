import logging
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# ─────────────────────────────────────────
# Main sniper confirmation entry point
# ─────────────────────────────────────────
def sniper_confirm(candles: list, signal: dict) -> dict:
    """
    Runs multiple confirmation filters on a signal.
    Returns an enriched signal dict with:
      - confirmed : bool   (True = safe to trade)
      - confidence: str    ("high" | "normal" | "low")
      - score     : int    (0–5, how many filters passed)
      - reasons   : list   (which filters passed/failed)
    """
    if not candles or len(candles) < 20:
        return _reject(signal, "Not enough candle data for sniper filter")

    direction = signal.get("direction", "NONE")
    if direction == "NONE":
        return _reject(signal, "No direction to confirm")

    df      = _to_df(candles)
    score   = 0
    reasons = []

    # ── Filter 1: Trend alignment ─────────
    passed, reason = _check_trend(df, direction)
    reasons.append(reason)
    if passed:
        score += 1

    # ── Filter 2: RSI zone ────────────────
    passed, reason = _check_rsi_zone(df, direction)
    reasons.append(reason)
    if passed:
        score += 1

    # ── Filter 3: Candle momentum ─────────
    passed, reason = _check_candle_momentum(df, direction)
    reasons.append(reason)
    if passed:
        score += 1

    # ── Filter 4: Bollinger Band position ─
    passed, reason = _check_bb_position(df, direction)
    reasons.append(reason)
    if passed:
        score += 1

    # ── Filter 5: Volume / body strength ──
    passed, reason = _check_body_strength(df, direction)
    reasons.append(reason)
    if passed:
        score += 1

    # ── Confidence grading ────────────────
    if score >= 4:
        confidence = "high"
        confirmed  = True
    elif score >= 2:
        confidence = "normal"
        confirmed  = True
    else:
        confidence = "low"
        confirmed  = False

    result = {
        **signal,
        "confirmed":  confirmed,
        "confidence": confidence,
        "score":      score,
        "reasons":    reasons
    }

    _log_result(signal.get("market", "?"), direction, score, confidence, confirmed, reasons)
    return result


# ─────────────────────────────────────────
# Filter 1 — Trend alignment (EMA 9/21/50)
# ─────────────────────────────────────────
def _check_trend(df: pd.DataFrame, direction: str) -> tuple:
    """
    CALL: EMA9 > EMA21 > EMA50  (uptrend stack)
    PUT : EMA9 < EMA21 < EMA50  (downtrend stack)
    """
    try:
        ema9  = df["close"].ewm(span=9,  adjust=False).mean().iloc[-1]
        ema21 = df["close"].ewm(span=21, adjust=False).mean().iloc[-1]
        ema50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]

        if direction == "CALL":
            passed = ema9 > ema21 > ema50
        else:
            passed = ema9 < ema21 < ema50

        label = f"Trend stack {'✓' if passed else '✗'} (EMA9={ema9:.4f} EMA21={ema21:.4f} EMA50={ema50:.4f})"
        return passed, label

    except Exception as e:
        return False, f"Trend check error: {e}"


# ─────────────────────────────────────────
# Filter 2 — RSI zone check
# ─────────────────────────────────────────
def _check_rsi_zone(df: pd.DataFrame, direction: str) -> tuple:
    """
    CALL: RSI between 45–65 (momentum building upward, not overbought)
    PUT : RSI between 35–55 (momentum building downward, not oversold)
    Reject if RSI > 75 or RSI < 25 (extreme zones)
    """
    try:
        rsi = _calc_rsi(df["close"], 14)

        # Hard reject at extremes
        if rsi > 75 or rsi < 25:
            return False, f"RSI extreme ✗ ({rsi:.1f}) — skipping"

        if direction == "CALL":
            passed = 45 <= rsi <= 65
        else:
            passed = 35 <= rsi <= 55

        label = f"RSI zone {'✓' if passed else '✗'} ({rsi:.1f})"
        return passed, label

    except Exception as e:
        return False, f"RSI check error: {e}"


# ─────────────────────────────────────────
# Filter 3 — Candle momentum (last 3 candles)
# ─────────────────────────────────────────
def _check_candle_momentum(df: pd.DataFrame, direction: str) -> tuple:
    """
    CALL: At least 2 of the last 3 candles are bullish
    PUT : At least 2 of the last 3 candles are bearish
    """
    try:
        last3 = df.tail(3)
        bullish_count = (last3["close"] > last3["open"]).sum()
        bearish_count = (last3["close"] < last3["open"]).sum()

        if direction == "CALL":
            passed = bullish_count >= 2
            label  = f"Candle momentum {'✓' if passed else '✗'} ({bullish_count}/3 bullish)"
        else:
            passed = bearish_count >= 2
            label  = f"Candle momentum {'✓' if passed else '✗'} ({bearish_count}/3 bearish)"

        return passed, label

    except Exception as e:
        return False, f"Candle momentum check error: {e}"


# ─────────────────────────────────────────
# Filter 4 — Bollinger Band position
# ─────────────────────────────────────────
def _check_bb_position(df: pd.DataFrame, direction: str) -> tuple:
    """
    CALL: Price above BB midline (above 20-period MA) — bullish territory
    PUT : Price below BB midline — bearish territory
    Bonus: BB squeeze detected = higher confidence breakout setup
    """
    try:
        close  = df["close"]
        sma20  = close.rolling(20).mean()
        std20  = close.rolling(20).std()
        upper  = sma20 + (2 * std20)
        lower  = sma20 - (2 * std20)
        mid    = sma20

        last_close = close.iloc[-1]
        last_mid   = mid.iloc[-1]
        last_upper = upper.iloc[-1]
        last_lower = lower.iloc[-1]

        # Squeeze: band width narrowing (current width < 50% of 20-period avg width)
        band_width     = (upper - lower) / mid
        avg_band_width = band_width.rolling(20).mean().iloc[-1]
        curr_width     = band_width.iloc[-1]
        squeeze        = curr_width < (avg_band_width * 0.5)

        if direction == "CALL":
            passed = last_close > last_mid
            extra  = " + SQUEEZE ✓" if squeeze and last_close > last_mid else ""
        else:
            passed = last_close < last_mid
            extra  = " + SQUEEZE ✓" if squeeze and last_close < last_mid else ""

        label = (f"BB position {'✓' if passed else '✗'} "
                 f"(close={last_close:.4f} mid={last_mid:.4f}){extra}")
        return passed, label

    except Exception as e:
        return False, f"BB check error: {e}"


# ─────────────────────────────────────────
# Filter 5 — Candle body strength
# ─────────────────────────────────────────
def _check_body_strength(df: pd.DataFrame, direction: str) -> tuple:
    """
    Checks that the last candle has a meaningful body (not a doji).
    Body must be at least 40% of the total candle range.
    Also checks that the last candle body is larger than the 10-period average body.
    """
    try:
        last = df.iloc[-1]
        body  = abs(last["close"] - last["open"])
        range_ = last["high"] - last["low"]

        if range_ == 0:
            return False, "Doji candle ✗ (zero range)"

        body_ratio = body / range_

        # Average body over last 10 candles
        avg_body = (df["close"] - df["open"]).abs().tail(10).mean()

        strong_body   = body_ratio >= 0.4
        above_average = body >= avg_body * 0.8

        passed = strong_body and above_average

        # Direction check — body must point the right way
        if direction == "CALL" and last["close"] <= last["open"]:
            return False, f"Body direction ✗ (bearish candle on CALL signal)"
        if direction == "PUT" and last["close"] >= last["open"]:
            return False, f"Body direction ✗ (bullish candle on PUT signal)"

        label = (f"Body strength {'✓' if passed else '✗'} "
                 f"(ratio={body_ratio:.0%}, avg={avg_body:.5f})")
        return passed, label

    except Exception as e:
        return False, f"Body strength check error: {e}"


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def _to_df(candles: list) -> pd.DataFrame:
    """Convert candle list of dicts to a pandas DataFrame."""
    df = pd.DataFrame(candles)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _calc_rsi(series: pd.Series, period: int = 14) -> float:
    """Calculate RSI manually (no external dependency)."""
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def _reject(signal: dict, reason: str) -> dict:
    """Return a rejected signal dict."""
    log.debug(f"[SNIPER] Rejected — {reason}")
    return {
        **signal,
        "confirmed":  False,
        "confidence": "low",
        "score":      0,
        "reasons":    [reason]
    }


def _log_result(market, direction, score, confidence, confirmed, reasons):
    status = "✅ CONFIRMED" if confirmed else "❌ REJECTED"
    log.info(
        f"[SNIPER] {market} {direction} | {status} | "
        f"Score: {score}/5 | Confidence: {confidence}"
    )
    for r in reasons:
        log.debug(f"[SNIPER]   {r}")
