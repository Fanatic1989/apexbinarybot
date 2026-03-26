"""
Scalp Strategy v4.0 – Trend-Following Scalper with Trailing Stop
Deriv Multipliers – Forex & Gold

Entry: Pullback to 9 EMA in direction of HTF trend + ADX > 20
Exit: 1:2 initial target, but trailing stop locks in profit as trend continues

Trailing logic:
- After price moves 1× ATR in profit, stop trails at 1.5× ATR from peak
- This allows trades to ride trends while protecting profits
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


# ─────────────────────────────────────────
# MAIN ENTRY FUNCTION
# ─────────────────────────────────────────
def analyze_market(candles: list, market: str) -> dict:
    if not candles or len(candles) < 50:
        return None
    try:
        df = _to_df(candles)
    except Exception as e:
        log.error(f"[SCALP] {market}: {e}")
        return None
    if len(df) < 60:
        return None

    # ATR for stop sizing
    atr_series = _atr(df)
    atr_now = float(atr_series.iloc[-1])
    atr_avg = float(atr_series.tail(50).mean())
    atr_ratio = atr_now / atr_avg if atr_avg > 0 else 1.0
    if atr_ratio < 0.25:
        log.debug(f"[SCALP] {market} dead market ATR ratio={atr_ratio:.2f}")
        return None

    # HTF bias
    htf = _get_htf_bias(market)
    if htf == 0:
        return None

    # ADX trend strength
    adx_val = _adx(df)
    if adx_val < 20:
        log.debug(f"[SCALP] {market} ADX={adx_val:.1f} – weak trend")
        return None

    # Calculate EMAs on 1-min chart
    close = df["close"]
    e9 = _ema(close, 9)
    e21 = _ema(close, 21)

    # Check for pullback to 9 EMA
    last_close = float(close.iloc[-1])
    e9_now = float(e9.iloc[-1])
    e21_now = float(e21.iloc[-1])
    prev_close = float(close.iloc[-2])
    prev_e9 = float(e9.iloc[-2])

    # Bullish conditions
    if htf == 1:
        # Price above 9 EMA, but pulled back to touch it (within 0.5 ATR)
        pullback = abs(last_close - e9_now) < 0.5 * atr_now
        # Confirmation: bullish engulfing or close > previous high
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        bullish_conf = (last_candle["close"] > last_candle["open"] and
                        last_candle["close"] > prev_candle["high"])
        if pullback and bullish_conf and last_close > e21_now:
            sl = atr_now * 1.0
            tp = sl * 2.0
            log.info(f"[SCALP] {market} LONG | pullback to 9 EMA, ADX={adx_val:.1f}")
            return _build_signal(market, "LONG", "pullback",
                                 sl, tp, "high",
                                 f"Pullback to 9 EMA, ADX={adx_val:.1f}")

    # Bearish conditions
    if htf == -1:
        pullback = abs(last_close - e9_now) < 0.5 * atr_now
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        bearish_conf = (last_candle["close"] < last_candle["open"] and
                        last_candle["close"] < prev_candle["low"])
        if pullback and bearish_conf and last_close < e21_now:
            sl = atr_now * 1.0
            tp = sl * 2.0
            log.info(f"[SCALP] {market} SHORT | pullback to 9 EMA, ADX={adx_val:.1f}")
            return _build_signal(market, "SHORT", "pullback",
                                 sl, tp, "high",
                                 f"Pullback to 9 EMA, ADX={adx_val:.1f}")

    return None


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
    # High volatility pairs get lower multiplier
    high_vol = ("frxGBPJPY", "frxEURJPY", "frxGBPUSD")
    return 50 if market in high_vol else 100


# ─────────────────────────────────────────
# INDICATORS (same as before)
# ─────────────────────────────────────────
def _ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=p, adjust=False).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

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
