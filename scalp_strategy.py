"""
Scalp Strategy v5.0 – Fast EMA + Trend Follow
Deriv Multipliers – Forex & Gold

Strategies:
  1. Fast EMA (5/13) – crossovers, any market condition
  2. EMA Trend Follow (9/21) – requires HTF bias + ADX > 15

All signals have 1:2 risk/reward.
"""
import logging
import time
import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)

# HTF cache
_htf_cache = {}
_HTF_TTL = 900  # 15 min

def _get_htf_bias(market) -> int:
    now = time.time()
    if market in _htf_cache:
        candles, ts = _htf_cache[market]
        if now - ts < _HTF_TTL:
            return _htf_from_candles(candles)
    try:
        from deriv_api import get_htf_candles
        candles = get_htf_candles(market, retries=1)
    except Exception as e:
        log.debug(f"[HTF] {market}: {e}")
        return 0
    if candles and len(candles) >= 50:
        _htf_cache[market] = (candles, now)
        return _htf_from_candles(candles)
    return 0

def _htf_from_candles(candles) -> int:
    try:
        df = _to_df(candles)
        e50 = float(_ema(df["close"], 50).iloc[-1])
        e200 = float(_ema(df["close"], 200).iloc[-1])
        last = float(df["close"].iloc[-1])
        if last > e50 and e50 > e200:
            return 1
        if last < e50 and e50 < e200:
            return -1
        return 0
    except:
        return 0


def analyze_market(candles: list, market: str) -> dict:
    """Try all strategies, return first signal."""
    if not candles or len(candles) < 50:
        return None
    try:
        df = _to_df(candles)
    except Exception as e:
        log.error(f"[SCALP] {market}: {e}")
        return None
    if len(df) < 50:
        return None

    # ATR for stop sizing
    atr_series = _atr(df)
    atr_now = float(atr_series.iloc[-1])
    atr_avg = float(atr_series.tail(50).mean())
    atr_ratio = atr_now / atr_avg if atr_avg > 0 else 1.0
    if atr_ratio < 0.25:
        log.debug(f"[SCALP] {market} dead market ATR ratio={atr_ratio:.2f}")
        return None

    # Try fast EMA (5/13) first – no HTF needed
    signal = _fast_ema_crossover(df, market, atr_now)
    if signal:
        return signal

    # Then trend follow with HTF bias and ADX
    htf = _get_htf_bias(market)
    if htf != 0:
        signal = _ema_trend_follow(df, market, htf, atr_now)
        if signal:
            return signal

    return None


# ─────────────────────────────────────────
# FAST EMA CROSSOVER (5/13)
# ─────────────────────────────────────────
def _fast_ema_crossover(df, market, atr_now) -> dict:
    close = df["close"]
    e5 = _ema(close, 5)
    e13 = _ema(close, 13)

    e5_now = float(e5.iloc[-1])
    e5_prev = float(e5.iloc[-2])
    e13_now = float(e13.iloc[-1])
    e13_prev = float(e13.iloc[-2])

    crossed_up = e5_prev <= e13_prev and e5_now > e13_now
    crossed_down = e5_prev >= e13_prev and e5_now < e13_now

    if crossed_up:
        sl = atr_now * 1.0  # tight stop for scalps
        tp = sl * 2.0
        log.info(f"[SCALP] {market} LONG | fast EMA (5/13) cross")
        return _build_signal(market, "LONG", "fast_ema", sl, tp, "high", "5/13 EMA cross up")
    if crossed_down:
        sl = atr_now * 1.0
        tp = sl * 2.0
        log.info(f"[SCALP] {market} SHORT | fast EMA (5/13) cross")
        return _build_signal(market, "SHORT", "fast_ema", sl, tp, "high", "5/13 EMA cross down")
    return None


# ─────────────────────────────────────────
# EMA TREND FOLLOW (9/21) – requires HTF bias + ADX
# ─────────────────────────────────────────
def _ema_trend_follow(df, market, htf, atr_now) -> dict:
    close = df["close"]
    e9 = _ema(close, 9)
    e21 = _ema(close, 21)
    e50 = float(_ema(close, 50).iloc[-1])

    e9_now = float(e9.iloc[-1])
    e9_prev = float(e9.iloc[-2])
    e21_now = float(e21.iloc[-1])
    e21_prev = float(e21.iloc[-2])

    crossed_up = e9_prev <= e21_prev and e9_now > e21_now
    crossed_down = e9_prev >= e21_prev and e9_now < e21_now

    adx_val = _adx(df)
    if adx_val < 15:  # lower threshold
        return None

    # Bullish cross: both above 50 EMA, HTF bullish
    if crossed_up and e9_now > e50 and e21_now > e50 and htf == 1:
        sl = atr_now * 1.5
        tp = sl * 2.0
        log.info(f"[SCALP] {market} LONG | EMA trend, ADX={adx_val:.1f}")
        return _build_signal(market, "LONG", "ema_trend", sl, tp, "high", f"9/21 EMA cross up, ADX={adx_val:.1f}")

    # Bearish cross: both below 50 EMA, HTF bearish
    if crossed_down and e9_now < e50 and e21_now < e50 and htf == -1:
        sl = atr_now * 1.5
        tp = sl * 2.0
        log.info(f"[SCALP] {market} SHORT | EMA trend, ADX={adx_val:.1f}")
        return _build_signal(market, "SHORT", "ema_trend", sl, tp, "high", f"9/21 EMA cross down, ADX={adx_val:.1f}")
    return None


# ─────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────
def _build_signal(market, direction, strategy, sl_distance, tp_distance,
                  confidence, reason) -> dict:
    multiplier = _select_multiplier(market)
    return {
        "market": market,
        "direction": direction,
        "strategy": strategy,
        "sl_distance": round(float(sl_distance), 6),
        "tp_distance": round(float(tp_distance), 6),
        "confidence": confidence,
        "multiplier": multiplier,
        "reason": reason,
        "confirmed": True,
        "type": "multiplier",
    }

def _select_multiplier(market: str) -> int:
    if market in ("frxXAUUSD", "frxXAGUSD"):
        return 10
    high_vol = ("frxGBPJPY", "frxEURJPY", "frxGBPUSD")
    return 50 if market in high_vol else 100


# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def _ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def _adx(df, p=14) -> float:
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        dp = (h.diff()).clip(lower=0)
        dm = (-l.diff()).clip(lower=0)
        dp = dp.where(dp > dm, 0)
        dm = dm.where(dm > dp, 0)
        atr = tr.ewm(span=p, adjust=False).mean()
        dip = 100 * dp.ewm(span=p, adjust=False).mean() / atr.replace(0, np.nan)
        dim = 100 * dm.ewm(span=p, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (dip - dim).abs() / (dip + dim).replace(0, np.nan)
        v = float(dx.ewm(span=p, adjust=False).mean().iloc[-1])
        return 0.0 if np.isnan(v) else v
    except:
        return 0.0

def _atr(df, p=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def _to_df(candles):
    df = pd.DataFrame(candles)
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    return df.reset_index(drop=True)
