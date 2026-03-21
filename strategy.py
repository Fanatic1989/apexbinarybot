"""
Multi-Regime Strategy Engine v4.0

Three regimes × three asset classes = 9 distinct strategies
Thompson Sampling selects best arm per market per regime

Regime detection:
  Trending : ADX > 25
  Ranging  : ADX < 20
  Choppy   : ADX 20-25 OR ATR > 2x average → NO TRADE

Asset classes:
  Forex      : Pivot/Order Block + Stoch RSI mean reversion
  Commodities: 9SMA/20EMA crossover (trending) | Triangle/BB (ranging)
  Synthetics : BB scalp first, then regime-specific
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

    # Detect regime first
    regime = _detect_regime(df, market)
    if regime == "choppy":
        log.info(f"[REGIME] {market} CHOPPY — no trade")
        return _no_signal(market)

    log.info(f"[REGIME] {market} {regime.upper()}")

    # Route to asset class
    if config.is_commodity(market):
        return _commodity_strategy(df, candles, market, regime)
    elif config.is_forex(market):
        return _forex_strategy(df, candles, market, regime)
    else:
        return _synthetic_strategy(df, candles, market, regime)


# ─────────────────────────────────────────
# REGIME DETECTION
# ─────────────────────────────────────────
def _detect_regime(df, market) -> str:
    """
    Trending : ADX > 25
    Ranging  : ADX < 20
    Choppy   : ADX 20-25 OR ATR spike (>2x avg)
               → Thompson Sampling reward set to zero
    """
    try:
        adx_val = _adx(df)
        atr_val = float(_atr(df).iloc[-1])
        atr_avg = float(_atr(df).rolling(20).mean().iloc[-1])

        # ATR spike = extreme noise, especially on Volatility 100
        atr_spike = atr_val > atr_avg * 2.0

        if atr_spike:
            log.debug(f"[REGIME] {market} ATR spike {atr_val:.4f} vs avg {atr_avg:.4f}")
            # Zero out this market's Thompson Sampling reward temporarily
            try:
                tracker.record_volatility_spike(market)
            except:
                pass
            return "choppy"

        if adx_val > 25:
            return "trending"
        if adx_val < 20:
            return "ranging"
        return "choppy"  # ADX 20-25 = unclear direction

    except Exception as e:
        log.debug(f"[REGIME] Detection error: {e}")
        return "ranging"


# ─────────────────────────────────────────
# SYNTHETIC STRATEGY
# BB scalp always checked first (highest frequency signal)
# Then regime-specific strategy
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

    log.info(f"[BB] {market} | bb%={bb_pct:.2f} ADX={adx_val:.1f} "
             f"{'BULL' if last_bull else 'BEAR'} engulf={engulfing}")

    # ── Trend alignment check ─────────────────────────────────────
    # Don't fight the trend — only take BB signals that go WITH
    # or against the short-term EMA direction
    e9  = float(_ema(close, 9).iloc[-1])
    e21 = float(_ema(close, 21).iloc[-1])
    e50 = float(_ema(close, 50).iloc[-1])
    short_trend = 1 if e9 > e21 else -1  # short term
    mid_trend   = 1 if e21 > e50 else -1 # medium term

    # For trending markets: only trade WITH the trend
    # For ranging markets: trade both directions freely
    if regime == "trending":
        put_allowed  = short_trend == -1  # only PUT if trending down
        call_allowed = short_trend == 1   # only CALL if trending up
    else:
        put_allowed  = True   # ranging = both directions OK
        call_allowed = True

    log.info(f"[BB] {market} | bb%={bb_pct:.2f} ADX={adx_val:.1f} "
             f"{'BULL' if last_bull else 'BEAR'} engulf={engulfing} "
             f"trend={'UP' if short_trend==1 else 'DOWN'} "
             f"put={'✓' if put_allowed else '✗'} call={'✓' if call_allowed else '✗'}")

    # ── TIER 1: Price outside band ───────────────────────────────
    if bb_pct > 1.0 and put_allowed:
        conf = "high" if bb_pct > 1.05 else "normal"
        log.info(f"[SYNTH] {market} PUT | Above upper band {bb_pct:.2f}")
        return _build(market, "PUT", conf, candles, "bb_bounce")

    if bb_pct < 0.0 and call_allowed:
        conf = "high" if bb_pct < -0.05 else "normal"
        log.info(f"[SYNTH] {market} CALL | Below lower band {bb_pct:.2f}")
        return _build(market, "CALL", conf, candles, "bb_bounce")

    # ── TIER 2: Previous candle outside band + reversal ──────────
    if prev_close > prev_upper and last_bear and put_allowed:
        log.info(f"[SYNTH] {market} PUT | Prev above upper + reversal")
        return _build(market, "PUT", "high", candles, "bb_bounce")

    if prev_close < prev_lower and last_bull and call_allowed:
        log.info(f"[SYNTH] {market} CALL | Prev below lower + reversal")
        return _build(market, "CALL", "high", candles, "bb_bounce")

    # ── TIER 3: Near extremes with confirmation ───────────────────
    if bb_pct > 0.92 and (engulfing or strong_dir) and last_bear and put_allowed:
        log.info(f"[SYNTH] {market} PUT | Near upper {bb_pct:.2f}")
        return _build(market, "PUT", "normal", candles, "bb_bounce")

    if bb_pct < 0.08 and (engulfing or strong_dir) and last_bull and call_allowed:
        log.info(f"[SYNTH] {market} CALL | Near lower {bb_pct:.2f}")
        return _build(market, "CALL", "normal", candles, "bb_bounce")

    # ── Regime-specific strategies ────────────────────────────────
    if regime == "trending":
        return _synth_trending(df, candles, market, adx_val)
    elif regime == "ranging":
        return _synth_ranging(df, candles, market, adx_val)

    return _no_signal(market)


def _synth_trending(df, candles, market, adx_val):
    """
    Trending synthetics: Price Action Micro-Breakouts
    15m trend filter + 1m breakout in same direction
    """
    close  = df["close"]
    rsi    = float(_rsi(close).iloc[-1])
    e9     = float(_ema(close, 9).iloc[-1])
    e21    = float(_ema(close, 21).iloc[-1])

    def bull(i): return float(df.iloc[i]["close"]) > float(df.iloc[i]["open"])
    def bear(i): return float(df.iloc[i]["close"]) < float(df.iloc[i]["open"])

    # 3-candle streak with EMA confirmation
    if bull(-1) and bull(-2) and bull(-3) and e9 > e21 and 42 <= rsi <= 75:
        log.info(f"[SYNTH] {market} CALL | Trending momentum RSI {rsi:.1f}")
        return _build(market, "CALL", "normal", candles, "momentum_streak")

    if bear(-1) and bear(-2) and bear(-3) and e9 < e21 and 25 <= rsi <= 58:
        log.info(f"[SYNTH] {market} PUT | Trending momentum RSI {rsi:.1f}")
        return _build(market, "PUT", "normal", candles, "momentum_streak")

    # Micro-breakout: recent swing high/low break
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
    """
    Ranging synthetics: BB fade fake-outs (20-min cycle on Jump indices)
    RSI mean reversion at band extremes
    """
    close   = df["close"]
    rsi_val = float(_rsi(close).iloc[-1])
    stoch   = _stoch_rsi(close)

    # RSI extreme reversal — need BOTH RSI extreme AND actively turning
    rsi_series = _rsi(close)
    rsi_prev   = float(rsi_series.iloc[-2])
    rsi_prev2  = float(rsi_series.iloc[-3])
    rsi_turning_up   = rsi_val > rsi_prev > rsi_prev2  # consecutive rising
    rsi_turning_down = rsi_val < rsi_prev < rsi_prev2  # consecutive falling

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
    """
    Trending: Order Block + FVG Continuation
    Ranging:  RSI/Stochastic Mean Reversion at pivot levels
    """
    hour = datetime.datetime.utcnow().hour
    if not (6 <= hour < 17):
        log.debug(f"[FOREX] {market} outside session ({hour}h UTC)")
        return _no_signal(market)

    if regime == "trending":
        return _forex_trending(df, candles, market)
    else:
        return _forex_ranging(df, candles, market)


def _forex_trending(df, candles, market):
    """
    Order Block + FVG Continuation.
    Enter when price retraces into an Order Block or FVG
    in the direction of the major trend.
    """
    close = df["close"]
    htf   = _get_htf_trend(market)

    if htf == 0:
        return _no_signal(market)

    e200     = float(_ema(close, 200).iloc[-1])
    last_close= float(close.iloc[-1])
    stoch    = _stoch_rsi(close)
    adx_val  = _adx(df)

    # FVG detection
    fvg = _find_fvg(df)

    # Order Block: last strong opposing candle before the trend move
    ob = _find_order_block(df, htf)

    if htf == 1 and last_close > e200:  # Uptrend
        # Price retesting into bullish FVG or Order Block
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

    if htf == -1 and last_close < e200:  # Downtrend
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
    """
    RSI/Stochastic Mean Reversion.
    Buy at support, sell at resistance when RSI oversold/overbought.
    """
    close   = df["close"]
    rsi_val = float(_rsi(close).iloc[-1])
    rsi_prev= float(_rsi(close).iloc[-2])
    stoch   = _stoch_rsi(close)
    pivot   = _calc_pivot(df)

    if not pivot:
        return _no_signal(market)

    last_close = float(close.iloc[-1])
    pp, r1, s1 = pivot["pp"], pivot["r1"], pivot["s1"]
    tolerance  = last_close * 0.0004

    # Oversold at support
    near_support = abs(last_close - s1) < tolerance or abs(last_close - pp) < tolerance
    if rsi_val < 30 and rsi_val > rsi_prev and near_support:
        log.info(f"[FOREX] {market} CALL | RSI oversold {rsi_val:.1f} at support")
        return _build(market, "CALL", "high", candles, "pivot_stochrsi")

    # Overbought at resistance
    near_resist = abs(last_close - r1) < tolerance
    if rsi_val > 70 and rsi_val < rsi_prev and near_resist:
        log.info(f"[FOREX] {market} PUT | RSI overbought {rsi_val:.1f} at resistance")
        return _build(market, "PUT", "high", candles, "pivot_stochrsi")

    # Stoch RSI extremes
    if stoch is not None:
        if stoch < 20 and rsi_val < 45:
            log.info(f"[FOREX] {market} CALL | StochRSI {stoch:.1f} oversold")
            return _build(market, "CALL", "normal", candles, "pivot_stochrsi")
        if stoch > 80 and rsi_val > 55:
            log.info(f"[FOREX] {market} PUT | StochRSI {stoch:.1f} overbought")
            return _build(market, "PUT", "normal", candles, "pivot_stochrsi")

    return _no_signal(market)


# ─────────────────────────────────────────
# COMMODITY STRATEGY (Gold/Silver)
# ─────────────────────────────────────────
def _commodity_strategy(df, candles, market, regime):
    if regime == "trending":
        return _commodity_trending(df, candles, market)
    else:
        return _commodity_ranging(df, candles, market)


def _commodity_trending(df, candles, market):
    """
    9 SMA / 20 EMA Crossover.
    Enter when 9 SMA crosses above/below 20 EMA after candle close.
    Confirmed by ADX > 25 and price above/below 200 EMA.
    """
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

    # Golden cross: 9 SMA crosses above 20 EMA + price above 200 EMA
    crossed_up   = sma9_prev <= e20_prev and sma9_now > e20_now
    crossed_down = sma9_prev >= e20_prev and sma9_now < e20_now

    if crossed_up and last_close > e200_val and adx_val > 25:
        log.info(f"[COMM] {market} CALL | 9SMA/20EMA Golden Cross ADX {adx_val:.1f}")
        return _build(market, "CALL", "high", candles, "fvg_retest")

    if crossed_down and last_close < e200_val and adx_val > 25:
        log.info(f"[COMM] {market} PUT | 9SMA/20EMA Death Cross ADX {adx_val:.1f}")
        return _build(market, "PUT", "high", candles, "fvg_retest")

    # Also check FVG retest in trend direction
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
    """
    Bollinger Band fade + Stoch RSI.
    Gold consolidates in tight ranges — fade the extremes.
    """
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
    """
    Order Block: last strong opposing candle before the trend move.
    For uptrend: find last bearish candle before strong bullish move.
    For downtrend: find last bullish candle before strong bearish move.
    """
    try:
        for i in range(-5, -20, -1):
            candle  = df.iloc[i]
            c_bull  = float(candle["close"]) > float(candle["open"])
            c_bear  = not c_bull
            body    = abs(float(candle["close"]) - float(candle["open"]))
            avg_body= float((df["close"]-df["open"]).abs().tail(20).mean())

            if body < avg_body * 1.5:
                continue  # not a strong candle

            if trend_dir == 1 and c_bear:  # bearish OB in uptrend
                return {
                    "type": "bullish",
                    "low":  float(candle["low"]),
                    "high": float(candle["open"])  # top of bearish candle body
                }
            if trend_dir == -1 and c_bull:  # bullish OB in downtrend
                return {
                    "type": "bearish",
                    "low":  float(candle["open"]),
                    "high": float(candle["high"])
                }
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

    # STRICT MODE: quality over quantity
    # High base confidence (strong setup) needs score >= 2
    # Normal base confidence needs score >= 3
    # This filters out all marginal signals
    if base_conf == "high":
        if score >= 3:
            result["confirmed"]  = True
            result["confidence"] = "high"
        elif score >= 2:
            result["confirmed"]  = True
            result["confidence"] = "normal"
        else:
            result["confirmed"]  = False
    else:
        # Normal strategies need strong sniper confirmation
        if score >= 3:
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

def record_trade_outcome(market, strategy, result):
    if strategy and result in ("won","lost"):
        tracker.record(strategy, market, result)
        if not selector._ai_active:
            selector.re_enable_ai()
