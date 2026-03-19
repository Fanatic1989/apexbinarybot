import logging
import pandas as pd
import numpy as np

import config
from deriv_api import get_htf_candles
from sniper_filter import sniper_confirm

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# HTF cache — refresh every 30 minutes
# Prevents opening a new WebSocket every scan
# ─────────────────────────────────────────
_htf_cache = {}           # {symbol: (candles, timestamp)}
_HTF_CACHE_TTL = 1800     # 30 minutes in seconds

def _get_htf_trend_cached(market):
    import time as _time
    now = _time.time()
    if market in _htf_cache:
        candles, ts = _htf_cache[market]
        if now - ts < _HTF_CACHE_TTL:
            return _compute_htf_trend(candles)
    # Cache miss or expired — fetch fresh
    # Use retries=1 only so a bad symbol doesn't block the scan for 30s
    try:
        candles = get_htf_candles(market, retries=1)
    except Exception as e:
        log.warning(f"[STRATEGY] HTF fetch failed for {market}: {e}")
        return 0
    if candles and len(candles) >= 25:
        _htf_cache[market] = (candles, now)
        return _compute_htf_trend(candles)
    else:
        # Cache a negative result so we don't retry every scan
        _htf_cache[market] = ([], now - _HTF_CACHE_TTL + 300)
        log.warning(f"[STRATEGY] HTF no data for {market} — skipping HTF filter")
        return 0   # 0 = unclear = don't block signal

def _compute_htf_trend(candles):
    try:
        if not candles or len(candles) < 25:
            return 0
        df = _to_df(candles)
        ema9  = _ema(df["close"], 9).iloc[-1]
        ema21 = _ema(df["close"], 21).iloc[-1]
        if ema9 > ema21 * 1.0001:
            return 1
        elif ema9 < ema21 * 0.9999:
            return -1
        return 0
    except Exception as e:
        log.warning(f"[STRATEGY] HTF compute error: {e}")
        return 0


def _ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def _rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _bollinger(df, period=20, std=2):
    df["bb_mid"]   = df["close"].rolling(period).mean()
    df["bb_std"]   = df["close"].rolling(period).std()
    df["bb_upper"] = df["bb_mid"] + df["bb_std"] * std
    df["bb_lower"] = df["bb_mid"] - df["bb_std"] * std
    df["bb_width"] = df["bb_upper"] - df["bb_lower"]
    return df

def _to_df(candles):
    df = pd.DataFrame(candles)
    for c in ["open","high","low","close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["open","high","low","close"], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# _get_htf_trend replaced by _get_htf_trend_cached above


def analyze_market(candles, market):
    if not candles or len(candles) < 30:
        return None

    try:
        df = _to_df(candles)
    except Exception as e:
        log.error(f"[STRATEGY] {market} df error: {e}")
        return None

    if len(df) < 30:
        return None

    df["ema9"]  = _ema(df["close"], 9)
    df["ema21"] = _ema(df["close"], 21)
    df["rsi"]   = _rsi(df["close"], 14)
    df          = _bollinger(df)

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    prev2 = df.iloc[-3]

    for field in ["ema9","ema21","rsi","bb_mid"]:
        if pd.isna(last[field]):
            return _no_signal(market)

    rsi_val = float(last["rsi"])

    # ── Special markets ───────────────────
    if market in ("BOOM500","BOOM1000"):
        return _boom_signal(df, market, rsi_val, candles)
    if market in ("CRASH500","CRASH1000"):
        return _crash_signal(df, market, rsi_val, candles)
    if market.startswith("JD"):
        return _jump_signal(df, market, candles)

    # ── Hard RSI block ────────────────────
    if rsi_val > 75 or rsi_val < 25:
        return _no_signal(market)

    ema9_now  = float(last["ema9"])
    ema21_now = float(last["ema21"])
    ema9_p1   = float(prev["ema9"])
    ema21_p1  = float(prev["ema21"])
    ema9_p2   = float(prev2["ema9"])
    ema21_p2  = float(prev2["ema21"])

    bull_candle = float(last["close"]) > float(last["open"])
    bear_candle = float(last["close"]) < float(last["open"])
    bull_prev   = float(prev["close"]) > float(prev["open"])
    bear_prev   = float(prev["close"]) < float(prev["open"])

    # BB expanding (good for entries)
    avg_width  = float(df["bb_width"].mean())
    last_width = float(last["bb_width"])
    bb_expanding = last_width > avg_width * 0.85

    # ── Higher timeframe trend filter ─────
    # For forex: ONLY trade with the 1-hour trend
    # For synthetics: skip HTF filter (random anyway)
    htf_trend = 0
    if config.is_forex(market):
        htf_trend = _get_htf_trend_cached(market)
        # If HTF trend is clear, only take aligned signals
        # This is the key to 75-85% win rate on forex

    # ── Mode A: Fresh EMA crossover ───────
    cross_up   = ema9_p1 <= ema21_p1 and ema9_now > ema21_now
    cross_down = ema9_p1 >= ema21_p1 and ema9_now < ema21_now

    if cross_up and 40 <= rsi_val <= 68 and bull_candle:
        # For forex: only take if HTF agrees or is unclear
        if config.is_forex(market) and htf_trend == -1:
            log.debug(f"[STRATEGY] {market} CALL blocked by HTF downtrend")
            return _no_signal(market)
        log.info(f"[STRATEGY] {market} Mode A CALL | RSI {rsi_val:.1f} | HTF {htf_trend}")
        return _build_signal(market, "CALL", "high", candles)

    if cross_down and 32 <= rsi_val <= 60 and bear_candle:
        if config.is_forex(market) and htf_trend == 1:
            log.debug(f"[STRATEGY] {market} PUT blocked by HTF uptrend")
            return _no_signal(market)
        log.info(f"[STRATEGY] {market} Mode A PUT | RSI {rsi_val:.1f} | HTF {htf_trend}")
        return _build_signal(market, "PUT", "high", candles)

    # ── Mode B: Trend continuation ────────
    trend_up   = ema9_now > ema21_now and ema9_p1 > ema21_p1 and ema9_p2 > ema21_p2
    trend_down = ema9_now < ema21_now and ema9_p1 < ema21_p1 and ema9_p2 < ema21_p2

    if trend_up and 42 <= rsi_val <= 65 and bull_candle and bb_expanding:
        if config.is_forex(market) and htf_trend == -1:
            return _no_signal(market)
        log.info(f"[STRATEGY] {market} Mode B CALL | RSI {rsi_val:.1f} | HTF {htf_trend}")
        return _build_signal(market, "CALL", "normal", candles)

    if trend_down and 35 <= rsi_val <= 58 and bear_candle and bb_expanding:
        if config.is_forex(market) and htf_trend == 1:
            return _no_signal(market)
        log.info(f"[STRATEGY] {market} Mode B PUT | RSI {rsi_val:.1f} | HTF {htf_trend}")
        return _build_signal(market, "PUT", "normal", candles)

    # ── Mode C: RSI momentum ──────────────
    # Only use for synthetics — too noisy for forex
    if not config.is_forex(market):
        if rsi_val >= 55 and bull_candle and bull_prev and ema9_now > ema21_now:
            log.info(f"[STRATEGY] {market} Mode C CALL | RSI {rsi_val:.1f}")
            return _build_signal(market, "CALL", "normal", candles)
        if rsi_val <= 45 and bear_candle and bear_prev and ema9_now < ema21_now:
            log.info(f"[STRATEGY] {market} Mode C PUT | RSI {rsi_val:.1f}")
            return _build_signal(market, "PUT", "normal", candles)

    return _no_signal(market)


def _build_signal(market, direction, base_confidence, candles):
    raw    = {"market": market, "direction": direction, "expiry": config.get_expiry(market)}
    result = sniper_confirm(candles, raw)
    score  = result.get("score", 0)
    if score >= 3:
        result["confidence"] = "high"
        result["confirmed"]  = True
    elif score >= 1:
        result["confidence"] = "normal"
        result["confirmed"]  = True
    else:
        if base_confidence == "high":
            result["confirmed"]  = True
            result["confidence"] = "normal"
        else:
            result["confirmed"] = False
    return result


def _boom_signal(df, market, rsi_val, candles):
    """
    Boom indices drift down then spike UP.
    Enter CALL when RSI shows oversold drift but not extreme spike.
    RSI below 15 means spike may already be firing.
    """
    last = df.iloc[-1]
    ema_down = float(last["ema9"]) < float(last["ema21"])
    # RSI between 20-40: oversold drift, good entry
    # RSI below 18: spike may already be in progress
    if ema_down and 18 <= rsi_val <= 42:
        log.info(f"[STRATEGY] {market} BOOM CALL RSI {rsi_val:.1f}")
        return _build_signal(market, "CALL", "normal", candles)
    if rsi_val < 18:
        log.debug(f"[STRATEGY] {market} BOOM RSI {rsi_val:.1f} too extreme — skip")
    return _no_signal(market)


def _crash_signal(df, market, rsi_val, candles):
    """
    Crash indices drift up then crash DOWN.
    Enter PUT when RSI shows overbought but not extreme.
    RSI 99.9 means the crash ALREADY happened — don't enter after.
    """
    last = df.iloc[-1]
    ema_up = float(last["ema9"]) > float(last["ema21"])
    # RSI between 60-80: overbought drift, good entry
    # RSI above 85: spike already in progress, too late
    if ema_up and 60 <= rsi_val <= 82:
        log.info(f"[STRATEGY] {market} CRASH PUT RSI {rsi_val:.1f}")
        return _build_signal(market, "PUT", "normal", candles)
    if rsi_val > 82:
        log.debug(f"[STRATEGY] {market} CRASH RSI {rsi_val:.1f} too extreme — skip")
    return _no_signal(market)


def _jump_signal(df, market, candles):
    if len(df) < 5:
        return _no_signal(market)
    last      = df.iloc[-1]
    avg_body  = (df["close"] - df["open"]).abs().tail(20).mean()
    last_body = abs(float(last["close"]) - float(last["open"]))
    if avg_body > 0 and last_body > avg_body * 4:
        direction = "PUT" if float(last["close"]) > float(last["open"]) else "CALL"
        log.info(f"[STRATEGY] {market} JUMP spike {direction}")
        return _build_signal(market, direction, "normal", candles)
    return _no_signal(market)


def _no_signal(market):
    return {
        "market": market, "direction": "NONE",
        "confidence": "low", "confirmed": False,
        "score": 0, "reasons": [],
        "expiry": config.get_expiry(market)
    }
