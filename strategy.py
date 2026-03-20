"""
Multi-strategy engine with AI selection.

The AI selector analyses current market conditions
and picks the best strategy mode for each market.
Performance is tracked per strategy per market
and fed back to Claude for continuous improvement.
"""
import logging
import pandas as pd
import numpy as np
import time

import config
from sniper_filter import sniper_confirm
from strategy_ai import tracker, selector

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# HTF cache
# ─────────────────────────────────────────
_htf_cache = {}
_HTF_TTL   = 1800

def _get_htf_trend(market):
    now = time.time()
    if market in _htf_cache:
        candles, ts = _htf_cache[market]
        if now - ts < _HTF_TTL:
            return _compute_trend(candles)
    try:
        from deriv_api import get_htf_candles
        candles = get_htf_candles(market, retries=1)
    except Exception as e:
        log.debug(f"[STRATEGY] HTF failed {market}: {e}")
        return 0
    if candles and len(candles) >= 25:
        _htf_cache[market] = (candles, now)
        return _compute_trend(candles)
    _htf_cache[market] = ([], now - _HTF_TTL + 300)
    return 0

def _compute_trend(candles):
    try:
        df  = _to_df(candles)
        e9  = _ema(df["close"], 9).iloc[-1]
        e21 = _ema(df["close"], 21).iloc[-1]
        return 1 if e9 > e21 * 1.0001 else -1 if e9 < e21 * 0.9999 else 0
    except:
        return 0


# ─────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────
def _ema(s, p):    return s.ewm(span=p, adjust=False).mean()
def _rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=p, adjust=False).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))
def _bollinger(df, p=20, std=2):
    m = df["close"].rolling(p).mean()
    s = df["close"].rolling(p).std()
    df["bb_upper"] = m + s*std
    df["bb_lower"] = m - s*std
    df["bb_mid"]   = m
    df["bb_width"] = df["bb_upper"] - df["bb_lower"]
    df["bb_pct"]   = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])
    return df
def _atr(df, p=14):
    h,l,c = df["high"],df["low"],df["close"]
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()
def _to_df(candles):
    df = pd.DataFrame(candles)
    for c in ["open","high","low","close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["open","high","low","close"], inplace=True)
    return df.reset_index(drop=True)


# ─────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────
def analyze_market(candles: list, market: str) -> dict:
    """
    AI-powered multi-strategy analysis.
    1. AI selector picks the best strategy for current conditions
    2. That strategy runs and generates a signal
    3. Sniper filter confirms or rejects
    4. Result is tracked back to improve future AI selections
    """
    if not candles or len(candles) < 40:
        return None

    try:
        df = _to_df(candles)
    except Exception as e:
        log.error(f"[STRATEGY] {market} error: {e}")
        return None

    if len(df) < 40:
        return None

    # ── AI selects strategy ───────────────
    chosen_strategy = selector.select_strategy(candles, market)
    log.info(f"[AI] {market} → {chosen_strategy}")

    # ── Run chosen strategy ───────────────
    signal = _run_strategy(chosen_strategy, df, candles, market)

    if signal and signal.get("direction") != "NONE":
        # Tag signal with which strategy generated it
        signal["strategy"] = chosen_strategy
        return signal

    # If chosen strategy found nothing, try one fallback
    fallback = _get_fallback(chosen_strategy)
    if fallback:
        signal = _run_strategy(fallback, df, candles, market)
        if signal and signal.get("direction") != "NONE":
            signal["strategy"] = fallback
            log.info(f"[AI] {market} fallback {fallback} found signal")
            return signal

    return _no_signal(market)


def _get_fallback(strategy: str) -> str:
    """Return a complementary fallback strategy."""
    fallbacks = {
        "rsi_reversal":   "bb_bounce",
        "bb_bounce":      "rsi_reversal",
        "ema_triple":     "momentum_streak",
        "false_breakout": "rsi_reversal",
        "momentum_streak":"ema_triple",
    }
    return fallbacks.get(strategy)


# ─────────────────────────────────────────
# Strategy dispatcher
# ─────────────────────────────────────────
def _run_strategy(name: str, df: pd.DataFrame,
                  candles: list, market: str) -> dict:
    """Route to the correct strategy function."""
    fns = {
        "rsi_reversal":   _rsi_reversal,
        "bb_bounce":      _bb_bounce,
        "ema_triple":     _ema_triple,
        "false_breakout": _false_breakout,
        "momentum_streak":_momentum_streak,
    }
    fn = fns.get(name)
    if not fn:
        return _no_signal(market)
    try:
        return fn(df, candles, market)
    except Exception as e:
        log.error(f"[STRATEGY] {name} error on {market}: {e}")
        return _no_signal(market)


# ─────────────────────────────────────────
# Strategy 1: RSI Extreme Reversal
# Target win rate: ~68%
# ─────────────────────────────────────────
def _rsi_reversal(df, candles, market):
    df["rsi"]  = _rsi(df["close"])
    df["ema9"] = _ema(df["close"], 9)
    df["ema21"]= _ema(df["close"], 21)
    last, prev = df.iloc[-1], df.iloc[-2]

    rsi_now  = float(last["rsi"])
    rsi_prev = float(prev["rsi"])
    bull = float(last["close"]) > float(last["open"])
    bear = float(last["close"]) < float(last["open"])
    htf  = _get_htf_trend(market) if config.is_forex(market) else 0

    # RSI was overbought, now turning down — sell signal
    if rsi_prev >= 76 and rsi_now < rsi_prev - 1.5 and bear:
        if not (config.is_forex(market) and htf == 1):
            log.info(f"[S1-RSI] {market} PUT | {rsi_prev:.1f}→{rsi_now:.1f}")
            return _build(market, "PUT", "high", candles)

    # RSI was oversold, now turning up — buy signal
    if rsi_prev <= 24 and rsi_now > rsi_prev + 1.5 and bull:
        if not (config.is_forex(market) and htf == -1):
            log.info(f"[S1-RSI] {market} CALL | {rsi_prev:.1f}→{rsi_now:.1f}")
            return _build(market, "CALL", "high", candles)

    return _no_signal(market)


# ─────────────────────────────────────────
# Strategy 2: Bollinger Band Bounce
# Target win rate: ~64%
# ─────────────────────────────────────────
def _bb_bounce(df, candles, market):
    df = _bollinger(df)
    df["rsi"] = _rsi(df["close"])
    last, prev = df.iloc[-1], df.iloc[-2]

    bb_pct      = float(last["bb_pct"])
    prev_bb_pct = float(prev["bb_pct"])
    rsi_val     = float(last["rsi"])
    bull = float(last["close"]) > float(last["open"])
    bear = float(last["close"]) < float(last["open"])
    htf  = _get_htf_trend(market) if config.is_forex(market) else 0

    avg_w = float(df["bb_width"].tail(20).mean())
    not_wide = float(last["bb_width"]) < avg_w * 1.8

    if prev_bb_pct > 1.0 and bb_pct <= 0.92 and bear and not_wide and 40 <= rsi_val <= 72:
        if not (config.is_forex(market) and htf == 1):
            log.info(f"[S2-BB] {market} PUT | bb_pct {bb_pct:.2f}")
            return _build(market, "PUT", "high", candles)

    if prev_bb_pct < 0.0 and bb_pct >= 0.08 and bull and not_wide and 28 <= rsi_val <= 60:
        if not (config.is_forex(market) and htf == -1):
            log.info(f"[S2-BB] {market} CALL | bb_pct {bb_pct:.2f}")
            return _build(market, "CALL", "high", candles)

    return _no_signal(market)


# ─────────────────────────────────────────
# Strategy 3: EMA Triple Stack
# Target win rate: ~62%
# ─────────────────────────────────────────
def _ema_triple(df, candles, market):
    df["ema9"]  = _ema(df["close"], 9)
    df["ema21"] = _ema(df["close"], 21)
    df["ema50"] = _ema(df["close"], 50)
    df["rsi"]   = _rsi(df["close"])
    last, prev  = df.iloc[-1], df.iloc[-2]

    e9, e21, e50 = float(last["ema9"]), float(last["ema21"]), float(last["ema50"])
    rsi_val = float(last["rsi"])
    bull = float(last["close"]) > float(last["open"])
    bear = float(last["close"]) < float(last["open"])
    htf  = _get_htf_trend(market) if config.is_forex(market) else 0

    crossed_up   = float(prev["ema9"]) <= float(prev["ema21"]) and e9 > e21
    crossed_down = float(prev["ema9"]) >= float(prev["ema21"]) and e9 < e21

    if e9 > e21 > e50 and 42 <= rsi_val <= 64 and bull:
        if not (config.is_forex(market) and htf == -1):
            conf = "high" if crossed_up else "normal"
            log.info(f"[S3-EMA] {market} CALL | stack RSI {rsi_val:.1f}")
            return _build(market, "CALL", conf, candles)

    if e9 < e21 < e50 and 36 <= rsi_val <= 58 and bear:
        if not (config.is_forex(market) and htf == 1):
            conf = "high" if crossed_down else "normal"
            log.info(f"[S3-EMA] {market} PUT | stack RSI {rsi_val:.1f}")
            return _build(market, "PUT", conf, candles)

    return _no_signal(market)


# ─────────────────────────────────────────
# Strategy 4: False Breakout Reversal
# Target win rate: ~61%
# ─────────────────────────────────────────
def _false_breakout(df, candles, market):
    df["rsi"] = _rsi(df["close"])
    last, prev = df.iloc[-1], df.iloc[-2]

    rsi_val    = float(last["rsi"])
    close      = float(last["close"])
    prev_close = float(prev["close"])
    prev_high  = float(prev["high"])
    prev_low   = float(prev["low"])
    bull = close > float(last["open"])
    bear = close < float(last["open"])
    htf  = _get_htf_trend(market) if config.is_forex(market) else 0

    recent_high = float(df["high"].tail(10).iloc[:-1].max())
    recent_low  = float(df["low"].tail(10).iloc[:-1].min())

    if prev_high > recent_high and close < prev_close and bear and 45 <= rsi_val <= 72:
        if not (config.is_forex(market) and htf == 1):
            log.info(f"[S4-FB] {market} PUT | false breakout high")
            return _build(market, "PUT", "high", candles)

    if prev_low < recent_low and close > prev_close and bull and 28 <= rsi_val <= 55:
        if not (config.is_forex(market) and htf == -1):
            log.info(f"[S4-FB] {market} CALL | false breakout low")
            return _build(market, "CALL", "high", candles)

    return _no_signal(market)


# ─────────────────────────────────────────
# Strategy 5: Momentum Streak
# Target win rate: ~58%
# ─────────────────────────────────────────
def _momentum_streak(df, candles, market):
    df["ema9"]  = _ema(df["close"], 9)
    df["ema21"] = _ema(df["close"], 21)
    df["rsi"]   = _rsi(df["close"])
    df = _bollinger(df)
    last = df.iloc[-1]

    rsi_val = float(last["rsi"])
    bb_pct  = float(last["bb_pct"])
    e9, e21 = float(last["ema9"]), float(last["ema21"])

    def is_bull(i): return float(df.iloc[i]["close"]) > float(df.iloc[i]["open"])
    def is_bear(i): return float(df.iloc[i]["close"]) < float(df.iloc[i]["open"])

    if is_bull(-1) and is_bull(-2) and is_bull(-3) and e9>e21 and 48<=rsi_val<=66 and 0.45<=bb_pct<=0.80:
        log.info(f"[S5-MOM] {market} CALL | 3-streak RSI {rsi_val:.1f}")
        return _build(market, "CALL", "normal", candles)

    if is_bear(-1) and is_bear(-2) and is_bear(-3) and e9<e21 and 34<=rsi_val<=52 and 0.20<=bb_pct<=0.55:
        log.info(f"[S5-MOM] {market} PUT | 3-streak RSI {rsi_val:.1f}")
        return _build(market, "PUT", "normal", candles)

    return _no_signal(market)


# ─────────────────────────────────────────
# Signal builder + sniper
# ─────────────────────────────────────────
def _build(market, direction, base_conf, candles):
    raw    = {"market": market, "direction": direction, "expiry": config.get_expiry(market)}
    result = sniper_confirm(candles, raw)
    score  = result.get("score", 0)

    # STRICT MODE: require score >= 2 for all signals
    # Fewer trades but much higher quality = higher win rate
    # This is the key change for 60%+ win rate
    if score >= 3:
        result["confirmed"]  = True
        result["confidence"] = "high"
    elif score >= 2 and base_conf == "high":
        # Score 2 only allowed if strategy itself is HIGH confidence
        # (RSI reversal and BB bounce qualify)
        result["confirmed"]  = True
        result["confidence"] = "normal"
    else:
        # Everything else rejected — quality over quantity
        result["confirmed"]  = False
    return result

def _no_signal(market):
    return {"market": market, "direction": "NONE",
            "confidence": "low", "confirmed": False,
            "score": 0, "reasons": [], "expiry": config.get_expiry(market)}


# ─────────────────────────────────────────
# Record outcome back to AI tracker
# Called from bot.py after trade settles
# ─────────────────────────────────────────
def record_trade_outcome(market: str, strategy: str, result: str):
    """Feed trade result back to AI for learning."""
    if strategy and result in ("won", "lost"):
        tracker.record(strategy, market, result)
        # Re-enable AI if it was disabled
        if not selector._ai_active:
            selector.re_enable_ai()
