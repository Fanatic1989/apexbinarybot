"""
Multi-Regime Strategy Engine v4.1

Changes from v4.0:
  - Tier 3 BB threshold lowered 0.92 → 0.88 (catches more near-extreme signals)
  - ADX > 35 accepted as alternative to engulfing candle for Tier 3
  - regime passed through signal dict so Thompson Sampling gets regime context
"""
import logging
import time
import numpy as np
import pandas as pd
import datetime

import config
from sniper_filter import sniper_confirm
from strategy_ai import tracker, selector

log = logging.getLogger(__name__)

# ── HTF cache ─────────────────────────────
_htf_cache = {}
_HTF_TTL   = 1800

def _get_htf_trend(market):
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
    _htf_cache[market] = ([], now - _HTF_TTL + 300)
    return 0

def _htf_from_candles(candles):
    try:
        df  = _to_df(candles)
        e200= _ema(df["close"], 200).iloc[-1]
        last= df["close"].iloc[-1]
        return 1 if float(last) > float(e200)*1.0002 else -1 if float(last) < float(e200)*0.9998 else 0
    except:
        return 0


# ─────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────
def analyze_market(candles: list, market: str) -> dict:
    if not candles or len(candles) < 50:
        return None
    try:
        df = _to_df(candles)
    except Exception as e:
        log.error(f"[STRATEGY] {market}: {e}")
        return None
    if len(df) < 50:
        return None

    regime = _detect_regime(df, market)
    if regime == "choppy":
        log.info(f"[REGIME] {market} CHOPPY — no trade")
        return _no_signal(market)

    log.info(f"[REGIME] {market} {regime.upper()}")

    if config.is_commodity(market):
        result = _commodity_strategy(df, candles, market, regime)
    elif config.is_forex(market):
        result = _forex_strategy(df, candles, market, regime)
    else:
        result = _synthetic_strategy(df, candles, market, regime)

    # Pass regime through so bot.py can record it for Thompson Sampling
    if result:
        result["regime"] = regime
    return result


# ─────────────────────────────────────────
# REGIME DETECTION
# ─────────────────────────────────────────
def _detect_regime(df, market) -> str:
    try:
        adx_val = _adx(df)
        atr_val = float(_atr(df).iloc[-1])
        atr_avg = float(_atr(df).rolling(20).mean().iloc[-1])
        atr_spike = atr_val > atr_avg * 2.0

        if atr_spike:
            log.debug(f"[REGIME] {market} ATR spike {atr_val:.4f} vs avg {atr_avg:.4f}")
            try:
                tracker.record_volatility_spike(market)
            except:
                pass
            return "choppy"

        if adx_val > 25:
            return "trending"
        if adx_val < 20:
            return "ranging"
        return "choppy"

    except Exception as e:
        log.debug(f"[REGIME] Detection error: {e}")
        return "ranging"


# ─────────────────────────────────────────
# SYNTHETIC STRATEGY
# ─────────────────────────────────────────
def _synthetic_strategy(df, candles, market, regime):
    close = df["close"]
    bb    = _bollinger_bands(close)
    if bb is None:
        return _no_signal(market)

    upper, lower, mid = bb
    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    last_close = float(last["close"])
    last_open  = float(last["open"])
    prev_close = float(prev["close"])
    prev_open  = float(prev["open"])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])
    prev_upper = float(upper.iloc[-2])
    prev_lower = float(lower.iloc[-2])

    bb_range   = last_upper - last_lower
    bb_pct     = (last_close - last_lower) / bb_range if bb_range > 0 else 0.5
    adx_val    = _adx(df)
    last_bull  = last_close > last_open
    last_bear  = last_close < last_open
    engulfing  = abs(last_close-last_open) > abs(prev_close-prev_open) * 0.8
    strong_dir = (last_bull and last_close > prev_close) or \
                 (last_bear and last_close < prev_close)

    e9  = float(_ema(close, 9).iloc[-1])
    e21 = float(_ema(close, 21).iloc[-1])
    e50 = float(_ema(close, 50).iloc[-1])
    short_trend = 1 if e9 > e21 else -1
    mid_trend   = 1 if e21 > e50 else -1

    if regime == "trending":
        put_allowed  = short_trend == -1
        call_allowed = short_trend == 1
    else:
        put_allowed  = True
        call_allowed = True

    log.info(f"[BB] {market} | bb%={bb_pct:.2f} ADX={adx_val:.1f} "
             f"{'BULL' if last_bull else 'BEAR'} engulf={engulfing} "
             f"trend={'UP' if short_trend==1 else 'DOWN'} "
             f"put={'✓' if put_allowed else '✗'} call={'✓' if call_allowed else '✗'}")

    # ── TIER 1: Price outside band ────────────────────────────
    if bb_pct > 1.0 and put_allowed:
        conf = "high" if bb_pct > 1.05 else "normal"
        log.info(f"[SYNTH] {market} PUT | Above upper band {bb_pct:.2f}")
        return _build(market, "PUT", conf, candles, "bb_bounce")

    if bb_pct < 0.0 and call_allowed:
        conf = "high" if bb_pct < -0.05 else "normal"
        log.info(f"[SYNTH] {market} CALL | Below lower band {bb_pct:.2f}")
        return _build(market, "CALL", conf, candles, "bb_bounce")

    # ── TIER 2: Previous candle outside band + reversal ───────
    if prev_close > prev_upper and last_bear and put_allowed:
        log.info(f"[SYNTH] {market} PUT | Prev above upper + reversal")
        return _build(market, "PUT", "high", candles, "bb_bounce")

    if prev_close < prev_lower and last_bull and call_allowed:
        log.info(f"[SYNTH] {market} CALL | Prev below lower + reversal")
        return _build(market, "CALL", "high", candles, "bb_bounce")

    # ── TIER 3: Near extremes with confirmation ───────────────
    # Lowered threshold 0.92 → 0.88 so bb%=0.90/0.92 qualifies
    # ADX > 35 accepted as alternative to engulfing (strong trend = confirmation)
    tier3_confirm = engulfing or strong_dir or adx_val > 35

    if bb_pct > 0.88 and tier3_confirm and last_bear and put_allowed:
        log.info(f"[SYNTH] {market} PUT | Near upper {bb_pct:.2f} ADX={adx_val:.1f}")
        return _build(market, "PUT", "normal", candles, "bb_bounce")

    if bb_pct < 0.12 and tier3_confirm and last_bull and call_allowed:
        log.info(f"[SYNTH] {market} CALL | Near lower {bb_pct:.2f} ADX={adx_val:.1f}")
        return _build(market, "CALL", "normal", candles, "bb_bounce")

    # ── Regime-specific strategies ────────────────────────────
    if regime == "trending":
        return _synth_trending(df, candles, market, adx_val)
    elif regime == "ranging":
        return _synth_ranging(df, candles, market, adx_val)

    return _no_signal(market)


def _synth_trending(df, candles, market, adx_val):
    close  = df["close"]
    rsi    = float(_rsi(close).iloc[-1])
    e9     = float(_ema(close, 9).iloc[-1])
    e21    = float(_ema(close, 21).iloc[-1])

    def bull(i): return float(df.iloc[i]["close"]) > float(df.iloc[i]["open"])
    def bear(i): return float(df.iloc[i]["close"]) < float(df.iloc[i]["open"])

    if bull(-1) and bull(-2) and bull(-3) and e9 > e21 and 42 <= rsi <= 75:
        log.info(f"[SYNTH] {market} CALL | Trending momentum RSI {rsi:.1f}")
        return _build(market, "CALL", "normal", candles, "momentum_streak")

    if bear(-1) and bear(-2) and bear(-3) and e9 < e21 and 25 <= rsi <= 58:
        log.info(f"[SYNTH] {market} PUT | Trending momentum RSI {rsi:.1f}")
        return _build(market, "PUT", "normal", candles, "momentum_streak")

    recent_high = float(df["high"].tail(10).iloc[:-1].max())
    recent_low  = float(df["low"].tail(10).iloc[:-1].min())
    last_high   = float(df["high"].iloc[-1])
    last_low    = float(df["low"].iloc[-1])

    if last_high > recent_high and e9 > e21 and rsi > 50:
        log.info(f"[SYNTH] {market} CALL | Micro-breakout high")
        return _build(market, "CALL", "high", candles, "false_breakout")

    if last_low < recent_low and e9 < e21 and rsi < 50:
        log.info(f"[SYNTH] {market} PUT | Micro-breakout low")
        return _build(market, "PUT", "high", candles, "false_breakout")

    return _no_signal(market)


def _synth_ranging(df, candles, market, adx_val):
    close   = df["close"]
    rsi_val = float(_rsi(close).iloc[-1])
    stoch   = _stoch_rsi(close)

    rsi_series   = _rsi(close)
    rsi_prev     = float(rsi_series.iloc[-2])
    rsi_prev2    = float(rsi_series.iloc[-3])
    rsi_turning_up   = rsi_val > rsi_prev > rsi_prev2
    rsi_turning_down = rsi_val < rsi_prev < rsi_prev2

    if rsi_val <= 22 and rsi_turning_up and stoch and stoch < 25:
        log.info(f"[SYNTH] {market} CALL | RSI reversal {rsi_val:.1f} turning up")
        return _build(market, "CALL", "high", candles, "rsi_reversal")

    if rsi_val >= 78 and rsi_turning_down and stoch and stoch > 75:
        log.info(f"[SYNTH] {market} PUT | RSI reversal {rsi_val:.1f} turning down")
        return _build(market, "PUT", "high", candles, "rsi_reversal")

    return _no_signal(market)


# ─────────────────────────────────────────
# FOREX STRATEGY
# ─────────────────────────────────────────
def _forex_strategy(df, candles, market, regime):
    hour = datetime.datetime.utcnow().hour
    if not (6 <= hour < 17):
        log.debug(f"[FOREX] {market} outside session ({hour}h UTC)")
        return _no_signal(market)

    if regime == "trending":
        return _forex_trending(df, candles, market)
    else:
        return _forex_ranging(df, candles, market)


def _forex_trending(df, candles, market):
    close = df["close"]
    htf   = _get_htf_trend(market)

    if htf == 0:
        return _no_signal(market)

    e200      = float(_ema(close, 200).iloc[-1])
    last_close = float(close.iloc[-1])
    stoch     = _stoch_rsi(close)
    fvg       = _find_fvg(df)
    ob        = _find_order_block(df, htf)

    if htf == 1 and last_close > e200:
        if fvg and fvg["type"] == "bullish":
            in_fvg = float(fvg["low"]) <= last_close <= float(fvg["high"])
            if in_fvg and stoch and stoch < 35:
                log.info(f"[FOREX] {market} CALL | Bullish FVG retest StochRSI {stoch:.1f}")
                return _build(market, "CALL", "high", candles, "fvg_retest")

        if ob and ob["type"] == "bullish":
            in_ob = float(ob["low"]) <= last_close <= float(ob["high"])
            if in_ob and stoch and stoch < 40:
                log.info(f"[FOREX] {market} CALL | Order Block retest")
                return _build(market, "CALL", "high", candles, "fvg_retest")

    if htf == -1 and last_close < e200:
        if fvg and fvg["type"] == "bearish":
            in_fvg = float(fvg["low"]) <= last_close <= float(fvg["high"])
            if in_fvg and stoch and stoch > 65:
                log.info(f"[FOREX] {market} PUT | Bearish FVG retest StochRSI {stoch:.1f}")
                return _build(market, "PUT", "high", candles, "fvg_retest")

        if ob and ob["type"] == "bearish":
            in_ob = float(ob["low"]) <= last_close <= float(ob["high"])
            if in_ob and stoch and stoch > 60:
                log.info(f"[FOREX] {market} PUT | Bearish Order Block retest")
                return _build(market, "PUT", "high", candles, "fvg_retest")

    return _no_signal(market)


def _forex_ranging(df, candles, market):
    close   = df["close"]
    rsi_val = float(_rsi(close).iloc[-1])
    rsi_prev= float(_rsi(close).iloc[-2])
    stoch   = _stoch_rsi(close)
    pivot   = _calc_pivot(df)
    sr      = _find_sr_levels(df)
    fib     = _find_fib_levels(df)

    if not pivot:
        return _no_signal(market)

    last_close = float(close.iloc[-1])
    pp, r1, s1 = pivot["pp"], pivot["r1"], pivot["s1"]

    def count_support_confluences(price):
        conf = 0
        if _near_level(price, s1):  conf += 1
        if _near_level(price, pp):  conf += 1
        if sr.get("near_support") and _near_level(price, sr["near_support"]): conf += 1
        if fib.get("at_fib") and fib.get("nearest") and _near_level(price, fib["nearest"]): conf += 1
        return conf

    def count_resistance_confluences(price):
        conf = 0
        if _near_level(price, r1): conf += 1
        if sr.get("near_resistance") and _near_level(price, sr["near_resistance"]): conf += 1
        if fib.get("at_fib") and fib.get("nearest") and _near_level(price, fib["nearest"]): conf += 1
        return conf

    sup_conf = count_support_confluences(last_close)
    res_conf = count_resistance_confluences(last_close)

    if rsi_val < 32 and rsi_val > rsi_prev:
        if sup_conf >= 2:
            log.info(f"[FOREX] {market} CALL | RSI {rsi_val:.1f} + {sup_conf} support confluences")
            return _build(market, "CALL", "high", candles, "pivot_stochrsi")
        if sup_conf >= 1 and stoch and stoch < 25:
            log.info(f"[FOREX] {market} CALL | RSI {rsi_val:.1f} + StochRSI {stoch:.1f} + support")
            return _build(market, "CALL", "normal", candles, "pivot_stochrsi")

    if rsi_val > 68 and rsi_val < rsi_prev:
        if res_conf >= 2:
            log.info(f"[FOREX] {market} PUT | RSI {rsi_val:.1f} + {res_conf} resistance confluences")
            return _build(market, "PUT", "high", candles, "pivot_stochrsi")
        if res_conf >= 1 and stoch and stoch > 75:
            log.info(f"[FOREX] {market} PUT | RSI {rsi_val:.1f} + StochRSI {stoch:.1f} + resistance")
            return _build(market, "PUT", "normal", candles, "pivot_stochrsi")

    if fib.get("at_fib") and fib.get("nearest_name") in ("0.618", "0.500"):
        if fib.get("uptrend") and stoch and stoch < 30:
            log.info(f"[FOREX] {market} CALL | Fib {fib['nearest_name']} golden zone")
            return _build(market, "CALL", "high", candles, "pivot_stochrsi")
        if not fib.get("uptrend") and stoch and stoch > 70:
            log.info(f"[FOREX] {market} PUT | Fib {fib['nearest_name']} golden zone")
            return _build(market, "PUT", "high", candles, "pivot_stochrsi")

    return _no_signal(market)


# ─────────────────────────────────────────
# COMMODITY STRATEGY
# ─────────────────────────────────────────
def _commodity_strategy(df, candles, market, regime):
    if regime == "trending":
        return _commodity_trending(df, candles, market)
    else:
        return _commodity_ranging(df, candles, market)


def _commodity_trending(df, candles, market):
    close = df["close"]
    sma9  = close.rolling(9).mean()
    e20   = _ema(close, 20)
    e200  = _ema(close, 200)

    sma9_now  = float(sma9.iloc[-1])
    sma9_prev = float(sma9.iloc[-2])
    e20_now   = float(e20.iloc[-1])
    e20_prev  = float(e20.iloc[-2])
    e200_val  = float(e200.iloc[-1])
    last_close= float(close.iloc[-1])
    adx_val   = _adx(df)

    crossed_up   = sma9_prev <= e20_prev and sma9_now > e20_now
    crossed_down = sma9_prev >= e20_prev and sma9_now < e20_now

    if crossed_up and last_close > e200_val and adx_val > 25:
        log.info(f"[COMM] {market} CALL | 9SMA/20EMA Golden Cross ADX {adx_val:.1f}")
        return _build(market, "CALL", "high", candles, "fvg_retest")

    if crossed_down and last_close < e200_val and adx_val > 25:
        log.info(f"[COMM] {market} PUT | 9SMA/20EMA Death Cross ADX {adx_val:.1f}")
        return _build(market, "PUT", "high", candles, "fvg_retest")

    htf  = _get_htf_trend(market)
    fvg  = _find_fvg(df)
    stoch= _stoch_rsi(close)

    if htf == 1 and fvg and fvg["type"] == "bullish":
        if float(fvg["low"]) <= last_close <= float(fvg["high"]):
            if stoch and stoch < 40:
                log.info(f"[COMM] {market} CALL | FVG retest in uptrend")
                return _build(market, "CALL", "high", candles, "fvg_retest")

    if htf == -1 and fvg and fvg["type"] == "bearish":
        if float(fvg["low"]) <= last_close <= float(fvg["high"]):
            if stoch and stoch > 60:
                log.info(f"[COMM] {market} PUT | FVG retest in downtrend")
                return _build(market, "PUT", "high", candles, "fvg_retest")

    return _no_signal(market)


def _commodity_ranging(df, candles, market):
    close = df["close"]
    bb    = _bollinger_bands(close)
    if bb is None:
        return _no_signal(market)

    upper, lower, mid = bb
    last_close = float(close.iloc[-1])
    bb_range   = float(upper.iloc[-1]) - float(lower.iloc[-1])
    bb_pct     = (last_close - float(lower.iloc[-1])) / bb_range if bb_range > 0 else 0.5
    stoch      = _stoch_rsi(close)
    last_bear  = last_close < float(df["open"].iloc[-1])
    last_bull  = last_close > float(df["open"].iloc[-1])

    if bb_pct > 0.95 and stoch and stoch > 75 and last_bear:
        log.info(f"[COMM] {market} PUT | BB upper + stoch {stoch:.1f}")
        return _build(market, "PUT", "high", candles, "fvg_retest")

    if bb_pct < 0.05 and stoch and stoch < 25 and last_bull:
        log.info(f"[COMM] {market} CALL | BB lower + stoch {stoch:.1f}")
        return _build(market, "CALL", "high", candles, "fvg_retest")

    return _no_signal(market)


# ─────────────────────────────────────────
# S/R + FIBONACCI
# ─────────────────────────────────────────
def _find_sr_levels(df) -> dict:
    try:
        highs  = df["high"].values
        lows   = df["low"].values
        closes = df["close"].values
        n      = len(df)

        swing_highs, swing_lows = [], []

        for i in range(2, min(n-2, 100)):
            idx = n - 1 - i
            if idx < 2 or idx >= n-2:
                continue
            if (highs[idx] > highs[idx-1] and highs[idx] > highs[idx-2] and
                highs[idx] > highs[idx+1] and highs[idx] > highs[idx+2]):
                swing_highs.append(float(highs[idx]))
            if (lows[idx] < lows[idx-1] and lows[idx] < lows[idx-2] and
                lows[idx] < lows[idx+1] and lows[idx] < lows[idx+2]):
                swing_lows.append(float(lows[idx]))

        def cluster(levels, tolerance=0.001):
            if not levels: return []
            levels = sorted(levels)
            clustered, group = [], [levels[0]]
            for l in levels[1:]:
                if (l - group[-1]) / group[-1] < tolerance:
                    group.append(l)
                else:
                    clustered.append(sum(group)/len(group))
                    group = [l]
            if group: clustered.append(sum(group)/len(group))
            return clustered

        resistance = cluster(swing_highs)
        support    = cluster(swing_lows)
        last_close = float(closes[-1])

        near_res = min(resistance, key=lambda x: abs(x-last_close)) if resistance else None
        near_sup = min(support,    key=lambda x: abs(x-last_close)) if support    else None

        return {
            "resistance":      resistance[-3:] if resistance else [],
            "support":         support[-3:]    if support    else [],
            "near_resistance": near_res,
            "near_support":    near_sup,
            "last_close":      last_close,
        }
    except Exception as e:
        log.debug(f"[S/R] Detection error: {e}")
        return {}


def _find_fib_levels(df) -> dict:
    try:
        recent     = df.tail(50)
        swing_high = float(recent["high"].max())
        swing_low  = float(recent["low"].min())
        last_close = float(df["close"].iloc[-1])
        rng        = swing_high - swing_low

        if rng == 0: return {}

        high_idx = recent["high"].idxmax()
        low_idx  = recent["low"].idxmin()
        uptrend  = low_idx < high_idx

        if uptrend:
            fib_236 = swing_high - rng * 0.236
            fib_382 = swing_high - rng * 0.382
            fib_500 = swing_high - rng * 0.500
            fib_618 = swing_high - rng * 0.618
            fib_786 = swing_high - rng * 0.786
        else:
            fib_236 = swing_low + rng * 0.236
            fib_382 = swing_low + rng * 0.382
            fib_500 = swing_low + rng * 0.500
            fib_618 = swing_low + rng * 0.618
            fib_786 = swing_low + rng * 0.786

        levels = {"0.236":fib_236,"0.382":fib_382,"0.500":fib_500,"0.618":fib_618,"0.786":fib_786}

        tolerance    = last_close * 0.0005
        nearest_fib  = None
        nearest_dist = float('inf')
        nearest_name = None

        for name, level in levels.items():
            dist = abs(last_close - level)
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_fib  = level
                nearest_name = name

        return {
            "levels":       levels,
            "nearest":      nearest_fib,
            "nearest_name": nearest_name,
            "at_fib":       nearest_dist < tolerance,
            "uptrend":      uptrend,
            "swing_high":   swing_high,
            "swing_low":    swing_low,
        }
    except Exception as e:
        log.debug(f"[FIB] Detection error: {e}")
        return {}


def _near_level(price, level, tolerance_pct=0.04) -> bool:
    if level is None or level == 0: return False
    return abs(price - level) / level < (tolerance_pct / 100)


# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def _ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=p, adjust=False).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def _stoch_rsi(close, rsi_p=14, stoch_p=14):
    try:
        rsi = _rsi(close, rsi_p)
        mn  = rsi.rolling(stoch_p).min()
        mx  = rsi.rolling(stoch_p).max()
        st  = 100 * (rsi - mn) / (mx - mn).replace(0, np.nan)
        v   = float(st.iloc[-1])
        return None if np.isnan(v) else v
    except:
        return None

def _bollinger_bands(close, p=20, std=2):
    try:
        mid   = close.rolling(p).mean()
        sigma = close.rolling(p).std()
        return mid + std*sigma, mid - std*sigma, mid
    except:
        return None

def _adx(df, p=14):
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr  = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
        dp  = (h.diff()).clip(lower=0)
        dm  = (-l.diff()).clip(lower=0)
        dp  = dp.where(dp > dm, 0)
        dm  = dm.where(dm > dp, 0)
        atr = tr.ewm(span=p,adjust=False).mean()
        dip = 100*dp.ewm(span=p,adjust=False).mean()/atr.replace(0,np.nan)
        dim = 100*dm.ewm(span=p,adjust=False).mean()/atr.replace(0,np.nan)
        dx  = 100*(dip-dim).abs()/(dip+dim).replace(0,np.nan)
        v   = float(dx.ewm(span=p,adjust=False).mean().iloc[-1])
        return 0.0 if np.isnan(v) else v
    except:
        return 0.0

def _atr(df, p=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def _calc_pivot(df):
    try:
        prior = df.tail(48).head(24)
        H = float(prior["high"].max())
        L = float(prior["low"].min())
        C = float(prior["close"].iloc[-1])
        pp = (H+L+C)/3
        return {"pp":pp,"r1":2*pp-L,"r2":pp+(H-L),"s1":2*pp-H,"s2":pp-(H-L)}
    except:
        return None

def _find_fvg(df):
    try:
        for i in range(-8, -3):
            c1h = float(df["high"].iloc[i-1])
            c1l = float(df["low"].iloc[i-1])
            c3h = float(df["high"].iloc[i+1])
            c3l = float(df["low"].iloc[i+1])
            if c3l > c1h:
                return {"type":"bullish","low":c1h,"high":c3l}
            if c1l > c3h:
                return {"type":"bearish","low":c3h,"high":c1l}
        return None
    except:
        return None

def _find_order_block(df, trend_dir):
    try:
        for i in range(-5, -20, -1):
            candle  = df.iloc[i]
            c_bull  = float(candle["close"]) > float(candle["open"])
            c_bear  = not c_bull
            body    = abs(float(candle["close"]) - float(candle["open"]))
            avg_body= float((df["close"]-df["open"]).abs().tail(20).mean())
            if body < avg_body * 1.5: continue
            if trend_dir == 1 and c_bear:
                return {"type":"bullish","low":float(candle["low"]),"high":float(candle["open"])}
            if trend_dir == -1 and c_bull:
                return {"type":"bearish","low":float(candle["open"]),"high":float(candle["high"])}
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
# SIGNAL BUILDER
# ─────────────────────────────────────────
def _build(market, direction, base_conf, candles, strategy_name):
    raw    = {"market":market,"direction":direction,"expiry":config.get_expiry(market)}
    result = sniper_confirm(candles, raw)
    score  = result.get("score", 0)

    if base_conf == "high":
        if score >= 2:
            result["confirmed"]  = True
            result["confidence"] = "high" if score == 3 else "normal"
        else:
            result["confirmed"]  = False
    else:
        if score >= 2:
            result["confirmed"]  = True
            result["confidence"] = "normal"
        else:
            result["confirmed"]  = False

    result["strategy"] = strategy_name
    return result

def _no_signal(market):
    return {
        "market":market,"direction":"NONE","confidence":"low",
        "confirmed":False,"score":0,"reasons":[],
        "expiry":config.get_expiry(market)
    }

def record_trade_outcome(market, strategy, result, regime="any"):
    if strategy and result in ("won","lost"):
        tracker.record(strategy, market, result, regime=regime)
        if not selector._ai_active:
            selector.re_enable_ai()
