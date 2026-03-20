"""
Multi-Regime Strategy Engine — v3.0

Three completely separate strategy regimes:
  1. Forex (15m)     — Pivot Point + Supply/Demand + Stoch RSI + 200 EMA
  2. Synthetics (1-3m)— Bollinger Band Scalping + Engulfing confirmation
  3. Commodities (15m)— Structure Break + FVG Retest (Smart Money)

ADX filter separates trending vs ranging conditions per market.
Thompson Sampling (in strategy_ai.py) selects best indicator per market.
"""
import logging
import time
import numpy as np
import pandas as pd

import config
from sniper_filter import sniper_confirm
from strategy_ai import tracker, selector

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# HTF cache (30 min TTL)
# ─────────────────────────────────────────
_htf_cache = {}
_HTF_TTL   = 1800

def _get_htf_trend(market):
    now = time.time()
    if market in _htf_cache:
        candles, ts = _htf_cache[market]
        if now - ts < _HTF_TTL:
            return _htf_trend_from_candles(candles)
    try:
        from deriv_api import get_htf_candles
        candles = get_htf_candles(market, retries=1)
    except Exception as e:
        log.debug(f"[HTF] {market} failed: {e}")
        return 0
    if candles and len(candles) >= 50:
        _htf_cache[market] = (candles, now)
        return _htf_trend_from_candles(candles)
    _htf_cache[market] = ([], now - _HTF_TTL + 300)
    return 0

def _htf_trend_from_candles(candles):
    try:
        df   = _to_df(candles)
        e200 = _ema(df["close"], 200).iloc[-1]
        last = df["close"].iloc[-1]
        if last > e200 * 1.0002: return 1
        if last < e200 * 0.9998: return -1
        return 0
    except:
        return 0


# ─────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────
def analyze_market(candles: list, market: str) -> dict:
    if not candles or len(candles) < 50:
        return None
    try:
        df = _to_df(candles)
    except Exception as e:
        log.error(f"[STRATEGY] {market} df error: {e}")
        return None
    if len(df) < 50:
        return None

    # Route to correct regime
    if config.is_commodity(market):
        return _commodity_regime(df, candles, market)
    elif config.is_forex(market):
        return _forex_regime(df, candles, market)
    else:
        return _synthetic_regime(df, candles, market)


# ─────────────────────────────────────────
# REGIME 1: FOREX
# Pivot Point + Supply/Demand + Stoch RSI
# 200 EMA trend filter
# ADX: only trade if ADX < 30 (avoid strong trends for mean reversion)
# ─────────────────────────────────────────
def _forex_regime(df, candles, market):
    # Session filter — only trade high liquidity windows
    # 06:00-12:00 UTC (London) and 13:00-17:00 UTC (NY overlap)
    import datetime
    hour = datetime.datetime.utcnow().hour
    if not (6 <= hour < 17):
        log.debug(f"[FOREX] {market} outside liquidity window ({hour}h UTC)")
        return _no_signal(market)

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]

    # 200 EMA — major trend direction
    ema200    = _ema(close, 200).iloc[-1]
    last_close= close.iloc[-1]
    major_trend = 1 if last_close > ema200 else -1

    # ADX — market regime
    adx_val = _adx(df)
    # For forex mean reversion: prefer ADX 15-30 (some structure but not runaway)
    if adx_val > 35:
        log.debug(f"[FOREX] {market} ADX {adx_val:.1f} too strong — skip")
        return _no_signal(market)

    # Daily pivot points
    pivot = _calc_pivot(df)
    if not pivot:
        return _no_signal(market)

    pp, r1, r2, s1, s2 = pivot["pp"], pivot["r1"], pivot["r2"], pivot["s1"], pivot["s2"]

    # Stochastic RSI
    stoch_rsi = _stoch_rsi(close)
    if stoch_rsi is None:
        return _no_signal(market)

    # Supply/Demand zone detection
    sd_zone = _find_sd_zone(df)

    # ── Entry logic ──────────────────────
    tolerance = abs(last_close) * 0.0003  # 0.03% tolerance for pivot hit

    # PUT signal: price at resistance (R1/R2 or pivot) + stoch RSI overbought + downtrend pullback
    near_resistance = (abs(last_close - r1) < tolerance or
                      abs(last_close - r2) < tolerance or
                      (abs(last_close - pp) < tolerance and major_trend == -1))

    if near_resistance and stoch_rsi > 78:
        # Only trade with major trend (pullback entry)
        if major_trend == -1 or adx_val < 20:  # ranging = trade any direction
            zone_conf = sd_zone == "supply"
            conf = "high" if zone_conf else "normal"
            log.info(f"[FOREX] {market} PUT | Pivot resistance | "
                     f"StochRSI {stoch_rsi:.1f} | ADX {adx_val:.1f} | "
                     f"Trend {'DOWN' if major_trend==-1 else 'neutral'}")
            signal = _build(market, "PUT", conf, candles)
            signal["strategy"] = "pivot_stochrsi"
            return signal

    # CALL signal: price at support (S1/S2 or pivot) + stoch RSI oversold + uptrend pullback
    near_support = (abs(last_close - s1) < tolerance or
                   abs(last_close - s2) < tolerance or
                   (abs(last_close - pp) < tolerance and major_trend == 1))

    if near_support and stoch_rsi < 22:
        if major_trend == 1 or adx_val < 20:
            zone_conf = sd_zone == "demand"
            conf = "high" if zone_conf else "normal"
            log.info(f"[FOREX] {market} CALL | Pivot support | "
                     f"StochRSI {stoch_rsi:.1f} | ADX {adx_val:.1f} | "
                     f"Trend {'UP' if major_trend==1 else 'neutral'}")
            signal = _build(market, "CALL", conf, candles)
            signal["strategy"] = "pivot_stochrsi"
            return signal

    return _no_signal(market)


# ─────────────────────────────────────────
# REGIME 2: SYNTHETICS
# Bollinger Band Scalping + Engulfing candles
# ADX: trade ranging markets (ADX < 25) for BB bounce
#      trade trending markets (ADX > 25) for momentum
# Thompson Sampling selects best strategy per synthetic
# ─────────────────────────────────────────
def _synthetic_regime(df, candles, market):
    # AI selects which synthetic strategy to use
    chosen = selector.select_strategy(candles, market)

    if chosen in ("rsi_reversal", "bb_bounce"):
        result = _synth_bb_scalp(df, candles, market)
    elif chosen == "false_breakout":
        result = _synth_false_breakout(df, candles, market)
    else:
        result = _synth_momentum(df, candles, market)

    # Fallback if nothing found
    if not result or result.get("direction") == "NONE":
        result = _synth_bb_scalp(df, candles, market)

    if result and result.get("direction") != "NONE":
        result["strategy"] = chosen
    return result


def _synth_bb_scalp(df, candles, market):
    """
    BB Scalping — the primary synthetic strategy.
    Price closes OUTSIDE BB band + engulfing candle forms = entry.
    R_100 and JD100 are faster — accept lower score threshold.
    """
    close = df["close"]
    bb    = _bollinger_bands(close)
    if bb is None:
        return _no_signal(market)

    upper, lower, mid = bb
    last   = df.iloc[-1]
    prev   = df.iloc[-2]
    prev2  = df.iloc[-3]

    last_close = float(last["close"])
    last_open  = float(last["open"])
    prev_close = float(prev["close"])
    prev_open  = float(prev["open"])

    last_bull = last_close > last_open
    last_bear = last_close < last_open
    prev_bull = prev_close > prev_open
    prev_bear = prev_close < prev_open

    last_upper = upper.iloc[-1]
    last_lower = lower.iloc[-1]
    prev_upper = upper.iloc[-2]
    prev_lower = lower.iloc[-2]

    # Engulfing candle check
    body_last = abs(last_close - last_open)
    body_prev = abs(prev_close - prev_open)
    engulfing = body_last > body_prev * 0.8  # last candle engulfs previous

    adx_val = _adx(df)

    # CALL: previous candle closed below lower BB + current is bullish engulfing
    if prev_close < prev_lower and last_bull and engulfing:
        # ADX filter: prefer ranging market for mean reversion
        if adx_val < 30:
            conf = "high" if adx_val < 20 else "normal"
            log.info(f"[SYNTH] {market} CALL | BB lower pierce + engulf | "
                     f"ADX {adx_val:.1f}")
            return _build(market, "CALL", conf, candles)

    # PUT: previous candle closed above upper BB + current is bearish engulfing
    if prev_close > prev_upper and last_bear and engulfing:
        if adx_val < 30:
            conf = "high" if adx_val < 20 else "normal"
            log.info(f"[SYNTH] {market} PUT | BB upper pierce + engulf | "
                     f"ADX {adx_val:.1f}")
            return _build(market, "PUT", conf, candles)

    # Wider tolerance for faster indices (R_100, JD100)
    fast_market = market in ("R_100", "JD100", "1HZ100V")
    if fast_market:
        bb_pct = (last_close - float(lower.iloc[-1])) / (float(upper.iloc[-1]) - float(lower.iloc[-1]))
        if bb_pct > 0.92 and last_bear:
            log.info(f"[SYNTH] {market} PUT | Fast index BB extreme {bb_pct:.2f}")
            return _build(market, "PUT", "normal", candles)
        if bb_pct < 0.08 and last_bull:
            log.info(f"[SYNTH] {market} CALL | Fast index BB extreme {bb_pct:.2f}")
            return _build(market, "CALL", "normal", candles)

    return _no_signal(market)


def _synth_false_breakout(df, candles, market):
    """False breakout reversal on synthetics."""
    close = df["close"]
    rsi_val = float(_rsi(close).iloc[-1])
    last    = df.iloc[-1]
    prev    = df.iloc[-2]

    recent_high = float(df["high"].tail(10).iloc[:-1].max())
    recent_low  = float(df["low"].tail(10).iloc[:-1].min())
    prev_high   = float(prev["high"])
    prev_low    = float(prev["low"])
    last_close  = float(last["close"])
    prev_close  = float(prev["close"])
    bull = last_close > float(last["open"])
    bear = last_close < float(last["open"])

    if prev_high > recent_high and last_close < prev_close and bear and 45 <= rsi_val <= 72:
        log.info(f"[SYNTH] {market} PUT | False breakout high")
        return _build(market, "PUT", "high", candles)

    if prev_low < recent_low and last_close > prev_close and bull and 28 <= rsi_val <= 55:
        log.info(f"[SYNTH] {market} CALL | False breakout low")
        return _build(market, "CALL", "high", candles)

    return _no_signal(market)


def _synth_momentum(df, candles, market):
    """Momentum continuation for trending synthetic markets."""
    close   = df["close"]
    rsi_val = float(_rsi(close).iloc[-1])
    adx_val = _adx(df)
    e9      = float(_ema(close, 9).iloc[-1])
    e21     = float(_ema(close, 21).iloc[-1])

    # Only trade momentum when ADX confirms trend
    if adx_val < 22:
        return _no_signal(market)

    def bull(i): return float(df.iloc[i]["close"]) > float(df.iloc[i]["open"])
    def bear(i): return float(df.iloc[i]["close"]) < float(df.iloc[i]["open"])

    bb    = _bollinger_bands(close)
    if bb is None: return _no_signal(market)
    upper, lower, mid = bb
    bb_pct = (float(close.iloc[-1])-float(lower.iloc[-1])) / (float(upper.iloc[-1])-float(lower.iloc[-1]))

    if bull(-1) and bull(-2) and bull(-3) and e9>e21 and 48<=rsi_val<=66 and 0.45<=bb_pct<=0.80:
        log.info(f"[SYNTH] {market} CALL | Momentum ADX {adx_val:.1f}")
        return _build(market, "CALL", "normal", candles)

    if bear(-1) and bear(-2) and bear(-3) and e9<e21 and 34<=rsi_val<=52 and 0.20<=bb_pct<=0.55:
        log.info(f"[SYNTH] {market} PUT | Momentum ADX {adx_val:.1f}")
        return _build(market, "PUT", "normal", candles)

    return _no_signal(market)


# ─────────────────────────────────────────
# REGIME 3: COMMODITIES (Gold/Silver)
# Structure Break + FVG Retest
# Smart Money Concepts simplified
# ─────────────────────────────────────────
def _commodity_regime(df, candles, market):
    """
    Gold/Silver — Break of Structure + Fair Value Gap retest.

    1. Identify Break of Structure (BOS): price breaks above/below
       a significant swing high/low
    2. Look for Fair Value Gap (FVG): a 3-candle imbalance left
       by the breakout move
    3. Wait for price to retrace INTO the FVG
    4. Enter in direction of the BOS
    """
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]

    # 200 EMA for major trend
    ema200   = _ema(close, 200).iloc[-1]
    last_close = float(close.iloc[-1])
    major_trend = 1 if last_close > float(ema200) else -1

    # ADX — need some trend for commodity breakout to work
    adx_val = _adx(df)
    if adx_val < 18:
        log.debug(f"[COMM] {market} ADX {adx_val:.1f} too low — skip")
        return _no_signal(market)

    # Find recent swing highs/lows (last 20 candles)
    swing_high = float(df["high"].tail(20).iloc[:-3].max())
    swing_low  = float(df["low"].tail(20).iloc[:-3].min())

    # Check for Break of Structure
    last3_high = float(df["high"].tail(3).max())
    last3_low  = float(df["low"].tail(3).min())

    bullish_bos = last3_high > swing_high   # broke above swing high
    bearish_bos = last3_low  < swing_low    # broke below swing low

    # Fair Value Gap detection (3-candle imbalance)
    fvg = _find_fvg(df)

    # Stochastic RSI for entry timing
    stoch = _stoch_rsi(close)
    if stoch is None:
        return _no_signal(market)

    # CALL: bullish BOS + price in bullish FVG + oversold stoch + uptrend
    if bullish_bos and fvg and fvg["type"] == "bullish":
        in_fvg = float(fvg["low"]) <= last_close <= float(fvg["high"])
        if in_fvg and stoch < 40 and major_trend == 1:
            log.info(f"[COMM] {market} CALL | Bullish BOS + FVG retest | "
                     f"ADX {adx_val:.1f} StochRSI {stoch:.1f}")
            signal = _build(market, "CALL", "high", candles)
            signal["strategy"] = "fvg_retest"
            return signal

    # PUT: bearish BOS + price in bearish FVG + overbought stoch + downtrend
    if bearish_bos and fvg and fvg["type"] == "bearish":
        in_fvg = float(fvg["low"]) <= last_close <= float(fvg["high"])
        if in_fvg and stoch > 60 and major_trend == -1:
            log.info(f"[COMM] {market} PUT | Bearish BOS + FVG retest | "
                     f"ADX {adx_val:.1f} StochRSI {stoch:.1f}")
            signal = _build(market, "PUT", "high", candles)
            signal["strategy"] = "fvg_retest"
            return signal

    return _no_signal(market)


# ─────────────────────────────────────────
# Technical Indicators
# ─────────────────────────────────────────
def _ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=p, adjust=False).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def _stoch_rsi(close, rsi_period=14, stoch_period=14) -> float:
    """Stochastic RSI — RSI normalised between its own min/max."""
    try:
        rsi    = _rsi(close, rsi_period)
        rsi_min= rsi.rolling(stoch_period).min()
        rsi_max= rsi.rolling(stoch_period).max()
        stoch  = 100 * (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
        val    = float(stoch.iloc[-1])
        return None if np.isnan(val) else val
    except:
        return None

def _bollinger_bands(close, p=20, std=2):
    """Returns (upper, lower, mid) Series."""
    try:
        mid   = close.rolling(p).mean()
        sigma = close.rolling(p).std()
        return mid + std*sigma, mid - std*sigma, mid
    except:
        return None

def _adx(df, p=14) -> float:
    """Average Directional Index — measures trend strength."""
    try:
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        tr    = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        dm_pos = (high.diff()).clip(lower=0)
        dm_neg = (-low.diff()).clip(lower=0)
        # Set to 0 where DM+ < DM-
        dm_pos = dm_pos.where(dm_pos > dm_neg, 0)
        dm_neg = dm_neg.where(dm_neg > dm_pos, 0)
        atr    = tr.ewm(span=p, adjust=False).mean()
        di_pos = 100 * dm_pos.ewm(span=p, adjust=False).mean() / atr.replace(0, np.nan)
        di_neg = 100 * dm_neg.ewm(span=p, adjust=False).mean() / atr.replace(0, np.nan)
        dx     = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).replace(0, np.nan)
        adx    = dx.ewm(span=p, adjust=False).mean()
        val    = float(adx.iloc[-1])
        return 0.0 if np.isnan(val) else val
    except:
        return 0.0

def _calc_pivot(df) -> dict:
    """
    Daily Pivot Points from last complete 'day' of candles.
    PP = (H + L + C) / 3
    """
    try:
        # Use last 24 candles as a proxy for prior day (1m candles)
        prior  = df.tail(48).head(24)
        H      = float(prior["high"].max())
        L      = float(prior["low"].min())
        C      = float(prior["close"].iloc[-1])
        pp     = (H + L + C) / 3
        r1     = 2*pp - L
        r2     = pp + (H - L)
        s1     = 2*pp - H
        s2     = pp - (H - L)
        return {"pp":pp, "r1":r1, "r2":r2, "s1":s1, "s2":s2}
    except:
        return None

def _find_sd_zone(df) -> str:
    """
    Simplified Supply/Demand zone detection.
    Supply zone: area where price previously rejected sharply downward
    Demand zone: area where price previously rejected sharply upward
    Returns 'supply', 'demand', or 'none'
    """
    try:
        close = df["close"]
        last  = float(close.iloc[-1])
        # Look for consolidation then move (S/D zone approximation)
        for i in range(-5, -20, -1):
            c = float(close.iloc[i])
            c_before = float(close.iloc[i-1])
            c_after  = float(close.iloc[i+1]) if i+1 < 0 else float(close.iloc[-1])
            # Supply: price was at this level and dropped
            if abs(last - c) / last < 0.001 and c_after < c_before * 0.999:
                return "supply"
            # Demand: price was at this level and rose
            if abs(last - c) / last < 0.001 and c_after > c_before * 1.001:
                return "demand"
        return "none"
    except:
        return "none"

def _find_fvg(df) -> dict:
    """
    Fair Value Gap (FVG) — 3 candle imbalance.
    Bullish FVG: candle 1 high < candle 3 low (gap between them)
    Bearish FVG: candle 1 low > candle 3 high (gap between them)
    Looks back through last 10 candles for a recent FVG.
    """
    try:
        for i in range(-8, -3):
            c1_high = float(df["high"].iloc[i-1])
            c1_low  = float(df["low"].iloc[i-1])
            c3_high = float(df["high"].iloc[i+1])
            c3_low  = float(df["low"].iloc[i+1])

            # Bullish FVG: gap between c1 high and c3 low
            if c3_low > c1_high:
                return {"type":"bullish", "low":c1_high, "high":c3_low}

            # Bearish FVG: gap between c1 low and c3 high
            if c1_low > c3_high:
                return {"type":"bearish", "low":c3_high, "high":c1_low}

        return None
    except:
        return None

def _to_df(candles):
    df = pd.DataFrame(candles)
    for c in ["open","high","low","close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["open","high","low","close"], inplace=True)
    return df.reset_index(drop=True)


# ─────────────────────────────────────────
# Signal builder
# ─────────────────────────────────────────
def _build(market, direction, base_conf, candles):
    raw    = {"market":market,"direction":direction,"expiry":config.get_expiry(market)}
    result = sniper_confirm(candles, raw)
    score  = result.get("score", 0)
    if score >= 3:
        result["confirmed"]  = True
        result["confidence"] = "high"
    elif score >= 2 and base_conf == "high":
        result["confirmed"]  = True
        result["confidence"] = "normal"
    else:
        result["confirmed"]  = False
    return result

def _no_signal(market):
    return {
        "market":market, "direction":"NONE",
        "confidence":"low", "confirmed":False,
        "score":0, "reasons":[], "expiry":config.get_expiry(market)
    }


# ─────────────────────────────────────────
# Feedback to AI tracker
# ─────────────────────────────────────────
def record_trade_outcome(market: str, strategy: str, result: str):
    if strategy and result in ("won","lost"):
        tracker.record(strategy, market, result)
        if not selector._ai_active:
            selector.re_enable_ai()
