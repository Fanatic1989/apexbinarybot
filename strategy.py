import logging
import pandas as pd
import numpy as np

import config
from sniper_filter import sniper_confirm

log = logging.getLogger(__name__)


# ─────────────────────────────────────────
# Indicator functions
# ─────────────────────────────────────────
def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _bollinger(df: pd.DataFrame, period: int = 20, std_dev: int = 2) -> pd.DataFrame:
    df["bb_mid"]   = df["close"].rolling(period).mean()
    df["bb_std"]   = df["close"].rolling(period).std()
    df["bb_upper"] = df["bb_mid"] + (df["bb_std"] * std_dev)
    df["bb_lower"] = df["bb_mid"] - (df["bb_std"] * std_dev)
    df["bb_width"] = df["bb_upper"] - df["bb_lower"]
    return df


# ─────────────────────────────────────────
# Main strategy engine
# ─────────────────────────────────────────
def analyze_market(candles: list, market: str) -> dict:
    """
    Three signal modes from strictest to most relaxed:

    Mode A — EMA crossover (strictest, highest confidence)
      EMA9 just crossed EMA21 + RSI in range + candle confirms

    Mode B — EMA trend + RSI momentum (medium, fires more often)
      EMA9 already above/below EMA21 for 2+ candles + RSI confirms

    Mode C — RSI + candle momentum (most relaxed, catches fast moves)
      RSI in strong zone + last 2 candles both same direction
    """
    if not candles or len(candles) < 30:
        log.warning(f"[STRATEGY] {market} — insufficient candles")
        return None

    try:
        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.dropna(subset=["open", "high", "low", "close"], inplace=True)
        df.reset_index(drop=True, inplace=True)
    except Exception as e:
        log.error(f"[STRATEGY] {market} — DataFrame error: {e}")
        return None

    if len(df) < 30:
        return None

    # ── Indicators ───────────────────────
    df["ema9"]  = _ema(df["close"], 9)
    df["ema21"] = _ema(df["close"], 21)
    df["rsi"]   = _rsi(df["close"], 14)
    df          = _bollinger(df)

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    prev2 = df.iloc[-3]

    for field in ["ema9", "ema21", "rsi", "bb_mid"]:
        if pd.isna(last[field]):
            return _no_signal(market)

    rsi_val = float(last["rsi"])

    # ── Special markets ──────────────────
    if market in ("BOOM500", "BOOM1000"):
        return _boom_signal(df, market, rsi_val, candles)
    if market in ("CRASH500", "CRASH1000"):
        return _crash_signal(df, market, rsi_val, candles)
    if market.startswith("JD"):
        return _jump_signal(df, market, candles)

    # ── Hard RSI block (only extremes) ───
    # Widened from 70/30 to 75/25 to allow more signals
    if rsi_val > 75 or rsi_val < 25:
        log.debug(f"[STRATEGY] {market} — RSI extreme ({rsi_val:.1f})")
        return _no_signal(market)

    ema9_last  = float(last["ema9"])
    ema21_last = float(last["ema21"])
    ema9_prev  = float(prev["ema9"])
    ema21_prev = float(prev["ema21"])
    ema9_prev2 = float(prev2["ema9"])
    ema21_prev2= float(prev2["ema21"])

    bullish_candle = float(last["close"]) > float(last["open"])
    bearish_candle = float(last["close"]) < float(last["open"])
    bullish_prev   = float(prev["close"]) > float(prev["open"])
    bearish_prev   = float(prev["close"]) < float(prev["open"])

    # ─────────────────────────────────────
    # Mode A — Fresh EMA crossover
    # Confidence: HIGH
    # ─────────────────────────────────────
    cross_up   = (ema9_prev <= ema21_prev and ema9_last > ema21_last)
    cross_down = (ema9_prev >= ema21_prev and ema9_last < ema21_last)

    if cross_up and 42 <= rsi_val <= 68 and bullish_candle:
        log.info(f"[STRATEGY] {market} — Mode A CALL (fresh cross, RSI {rsi_val:.1f})")
        return _build_signal(market, "CALL", "high", candles)

    if cross_down and 32 <= rsi_val <= 58 and bearish_candle:
        log.info(f"[STRATEGY] {market} — Mode A PUT (fresh cross, RSI {rsi_val:.1f})")
        return _build_signal(market, "PUT", "high", candles)

    # ─────────────────────────────────────
    # Mode B — EMA trend continuation
    # EMA9 has been above/below EMA21 for 2+ candles
    # Confidence: NORMAL
    # ─────────────────────────────────────
    trend_up   = (ema9_last > ema21_last and
                  ema9_prev > ema21_prev and
                  ema9_prev2 > ema21_prev2)

    trend_down = (ema9_last < ema21_last and
                  ema9_prev < ema21_prev and
                  ema9_prev2 < ema21_prev2)

    if trend_up and 40 <= rsi_val <= 65 and bullish_candle:
        log.info(f"[STRATEGY] {market} — Mode B CALL (trend, RSI {rsi_val:.1f})")
        return _build_signal(market, "CALL", "normal", candles)

    if trend_down and 35 <= rsi_val <= 60 and bearish_candle:
        log.info(f"[STRATEGY] {market} — Mode B PUT (trend, RSI {rsi_val:.1f})")
        return _build_signal(market, "PUT", "normal", candles)

    # ─────────────────────────────────────
    # Mode C — RSI momentum + candle streak
    # No EMA cross needed — pure momentum
    # Confidence: NORMAL
    # ─────────────────────────────────────
    strong_bull = (rsi_val >= 55 and bullish_candle and bullish_prev)
    strong_bear = (rsi_val <= 45 and bearish_candle and bearish_prev)

    if strong_bull and ema9_last > ema21_last:
        log.info(f"[STRATEGY] {market} — Mode C CALL (momentum, RSI {rsi_val:.1f})")
        return _build_signal(market, "CALL", "normal", candles)

    if strong_bear and ema9_last < ema21_last:
        log.info(f"[STRATEGY] {market} — Mode C PUT (momentum, RSI {rsi_val:.1f})")
        return _build_signal(market, "PUT", "normal", candles)

    log.debug(f"[STRATEGY] {market} — No signal (RSI {rsi_val:.1f})")
    return _no_signal(market)


# ─────────────────────────────────────────
# Build and confirm signal
# ─────────────────────────────────────────
def _build_signal(market: str, direction: str,
                  base_confidence: str, candles: list) -> dict:
    """
    Run sniper filter with relaxed scoring.
    High confidence: score >= 3  (was 4)
    Normal:          score >= 1  (was 2)
    Always trade if score >= 1.
    """
    raw = {
        "market":    market,
        "direction": direction,
        "expiry":    config.get_expiry(market)
    }
    result = sniper_confirm(candles, raw)

    score = result.get("score", 0)

    # Relaxed thresholds — trade if at least 1 filter passes
    if score >= 3:
        result["confidence"] = "high"
        result["confirmed"]  = True
    elif score >= 1:
        result["confidence"] = "normal"
        result["confirmed"]  = True
    else:
        # Score 0 — only skip if base confidence was not high
        if base_confidence == "high":
            result["confirmed"]  = True
            result["confidence"] = "normal"
        else:
            result["confirmed"]  = False

    return result


# ─────────────────────────────────────────
# Boom index signal
# ─────────────────────────────────────────
def _boom_signal(df, market, rsi_val, candles):
    last = df.iloc[-1]
    if float(last["ema9"]) < float(last["ema21"]) and rsi_val < 45:
        log.info(f"[STRATEGY] {market} — BOOM CALL (RSI {rsi_val:.1f})")
        return _build_signal(market, "CALL", "normal", candles)
    return _no_signal(market)


# ─────────────────────────────────────────
# Crash index signal
# ─────────────────────────────────────────
def _crash_signal(df, market, rsi_val, candles):
    last = df.iloc[-1]
    if float(last["ema9"]) > float(last["ema21"]) and rsi_val > 55:
        log.info(f"[STRATEGY] {market} — CRASH PUT (RSI {rsi_val:.1f})")
        return _build_signal(market, "PUT", "normal", candles)
    return _no_signal(market)


# ─────────────────────────────────────────
# Jump index signal
# ─────────────────────────────────────────
def _jump_signal(df, market, candles):
    if len(df) < 5:
        return _no_signal(market)
    last      = df.iloc[-1]
    avg_body  = (df["close"] - df["open"]).abs().tail(20).mean()
    last_body = abs(float(last["close"]) - float(last["open"]))
    if avg_body > 0 and last_body > avg_body * 4:
        spike_up  = float(last["close"]) > float(last["open"])
        direction = "PUT" if spike_up else "CALL"
        log.info(f"[STRATEGY] {market} — JUMP spike → {direction}")
        return _build_signal(market, direction, "normal", candles)
    return _no_signal(market)


# ─────────────────────────────────────────
# No signal
# ─────────────────────────────────────────
def _no_signal(market: str) -> dict:
    return {
        "market":     market,
        "direction":  "NONE",
        "confidence": "low",
        "confirmed":  False,
        "score":      0,
        "reasons":    [],
        "expiry":     config.get_expiry(market)
    }
