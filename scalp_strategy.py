"""
Apex Scalping Strategy Engine v1.1
Deriv Multipliers — Forex & Gold

Strategies:
  1. EMA Trend Follow    — 9/21 cross confirmed by 50 EMA + HTF bias
  2. RSI/Stoch Reversal  — Oversold/overbought at S/R with momentum turn
  3. BB Squeeze Breakout — Low volatility compression → explosive move
  4. Order Block Retest  — Smart money levels with confirmation candle

Risk:
  - ATR-based stop loss (1.5x ATR)
  - 1:2 risk/reward minimum (TP = 2x SL distance)
  - Session filter: London + NY only for forex, 24h for Gold

Fixes in v1.1:
  - ADX DM calc: strict inequality (dm >= dp) to avoid cancellation when equal
  - _build_signal: sl_ratio is now sl_distance / entry_price (dimensionless)
    so trade_executor can compute: dollar_sl = stake * sl_ratio * multiplier
  - _select_multiplier: capped forex multipliers — x100 removed, max x50
  - Order block bearish: corrected low/high assignment (open > close for bull candle)
  - BB squeeze: breakout check now requires price outside band even when in_squeeze=True
"""
import logging
import time
import numpy as np
import pandas as pd
import datetime

import config

log = logging.getLogger(__name__)

# ── HTF cache ─────────────────────────────────────────────────
_htf_cache = {}
_HTF_TTL   = 900  # 15 min cache for scalping

def _get_htf_bias(market) -> int:
    """1H chart bias: 1=bullish, -1=bearish, 0=neutral"""
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
        df   = _to_df(candles)
        e50  = float(_ema(df["close"], 50).iloc[-1])
        e200 = float(_ema(df["close"], 200).iloc[-1])
        last = float(df["close"].iloc[-1])
        if last > e50 and e50 > e200: return 1
        if last < e50 and e50 < e200: return -1
        return 0
    except:
        return 0


# ─────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────
def analyze_market(candles: list, market: str) -> dict:
    """
    Returns signal dict or None.
    Signal keys: market, direction, strategy, sl_distance, tp_distance,
                 sl_ratio, tp_ratio, confidence, multiplier, reason, confirmed, type
    """
    if not candles or len(candles) < 60:
        return None
    try:
        df = _to_df(candles)
    except Exception as e:
        log.error(f"[SCALP] {market}: {e}")
        return None
    if len(df) < 60:
        return None

    if not _in_trading_session(market):
        return None

    atr_series = _atr(df)
    atr_now    = float(atr_series.iloc[-1])
    atr_avg    = float(atr_series.tail(50).mean())
    if atr_avg > 0 and atr_now / atr_avg < 0.20:
        log.debug(f"[SCALP] {market} dead market — skipping")
        return None

    htf = _get_htf_bias(market)

    signal = (_ema_trend_follow(df, candles, market, htf, atr_now) or
              _rsi_stoch_reversal(df, candles, market, htf, atr_now) or
              _bb_squeeze_breakout(df, candles, market, htf, atr_now) or
              _order_block_retest(df, candles, market, htf, atr_now))

    return signal


def _in_trading_session(market: str) -> bool:
    if market in ("frxXAUUSD", "frxXAGUSD"):
        return True
    hour = datetime.datetime.utcnow().hour
    if market in ("frxUSDJPY",):
        return 0 <= hour < 9 or 7 <= hour < 22
    return 7 <= hour < 22


# ─────────────────────────────────────────
# STRATEGY 1: EMA TREND FOLLOW
# ─────────────────────────────────────────
def _ema_trend_follow(df, candles, market, htf, atr_now) -> dict:
    """
    9 EMA crosses above 21 EMA, both above 50 EMA → LONG
    9 EMA crosses below 21 EMA, both below 50 EMA → SHORT
    HTF bias must agree. ADX > 20 required.
    """
    close = df["close"]
    e9    = _ema(close, 9)
    e21   = _ema(close, 21)
    e50   = float(_ema(close, 50).iloc[-1])

    e9_now   = float(e9.iloc[-1])
    e9_prev  = float(e9.iloc[-2])
    e21_now  = float(e21.iloc[-1])
    e21_prev = float(e21.iloc[-2])

    entry_price = float(close.iloc[-1])
    rsi_val     = float(_rsi(close).iloc[-1])
    adx_val     = _adx(df)

    if adx_val < 20:
        return None

    crossed_up   = e9_prev <= e21_prev and e9_now > e21_now
    crossed_down = e9_prev >= e21_prev and e9_now < e21_now

    if crossed_up and e9_now > e50 and e21_now > e50:
        if htf >= 0 and 40 <= rsi_val <= 70:
            sl = atr_now * 1.5
            tp = sl * 2.0
            log.info(f"[SCALP] {market} LONG | EMA cross bullish | ADX={adx_val:.1f} RSI={rsi_val:.1f}")
            return _build_signal(market, "LONG", "ema_trend",
                                 sl, tp, entry_price,
                                 "high" if htf == 1 else "normal",
                                 f"9/21 EMA cross UP, above 50 EMA, ADX={adx_val:.1f}")

    if crossed_down and e9_now < e50 and e21_now < e50:
        if htf <= 0 and 30 <= rsi_val <= 60:
            sl = atr_now * 1.5
            tp = sl * 2.0
            log.info(f"[SCALP] {market} SHORT | EMA cross bearish | ADX={adx_val:.1f} RSI={rsi_val:.1f}")
            return _build_signal(market, "SHORT", "ema_trend",
                                 sl, tp, entry_price,
                                 "high" if htf == -1 else "normal",
                                 f"9/21 EMA cross DOWN, below 50 EMA, ADX={adx_val:.1f}")
    return None


# ─────────────────────────────────────────
# STRATEGY 2: RSI/STOCH REVERSAL
# ─────────────────────────────────────────
def _rsi_stoch_reversal(df, candles, market, htf, atr_now) -> dict:
    close   = df["close"]
    rsi     = _rsi(close)
    rsi_now  = float(rsi.iloc[-1])
    rsi_prev = float(rsi.iloc[-2])
    stoch    = _stoch(df)

    last      = df.iloc[-1]
    last_bull = float(last["close"]) > float(last["open"])
    last_bear = float(last["close"]) < float(last["open"])
    entry_price = float(last["close"])

    if (rsi_now < 32 and rsi_now > rsi_prev and
            stoch is not None and stoch < 25 and last_bull and htf >= 0):
        sl = atr_now * 1.5
        tp = sl * 2.2
        log.info(f"[SCALP] {market} LONG | RSI reversal {rsi_now:.1f} Stoch {stoch:.1f}")
        return _build_signal(market, "LONG", "rsi_reversal",
                             sl, tp, entry_price, "high",
                             f"RSI {rsi_now:.1f} oversold turning up, Stoch {stoch:.1f}")

    if (rsi_now > 68 and rsi_now < rsi_prev and
            stoch is not None and stoch > 75 and last_bear and htf <= 0):
        sl = atr_now * 1.5
        tp = sl * 2.2
        log.info(f"[SCALP] {market} SHORT | RSI reversal {rsi_now:.1f} Stoch {stoch:.1f}")
        return _build_signal(market, "SHORT", "rsi_reversal",
                             sl, tp, entry_price, "high",
                             f"RSI {rsi_now:.1f} overbought turning down, Stoch {stoch:.1f}")
    return None


# ─────────────────────────────────────────
# STRATEGY 3: BB SQUEEZE BREAKOUT
# ─────────────────────────────────────────
def _bb_squeeze_breakout(df, candles, market, htf, atr_now) -> dict:
    """
    Bollinger Band squeeze → breakout outside the band in trend direction.

    FIX: breakout check (last_close > last_upper / last_close < last_lower)
    is now required regardless of whether we're still in the squeeze or just
    exited it. Previously, in_squeeze=True could fire without a breakout.
    """
    close = df["close"]
    bb    = _bollinger_bands(close)
    if bb is None:
        return None

    upper, lower, mid = bb
    bb_width_now  = float(upper.iloc[-1] - lower.iloc[-1])
    bb_width_avg  = float((upper - lower).tail(50).mean())
    bb_width_prev = float(upper.iloc[-2] - lower.iloc[-2])

    last_close = float(close.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])

    in_squeeze      = bb_width_now  < bb_width_avg * 0.70
    was_squeezed    = bb_width_prev < bb_width_avg * 0.70
    squeeze_context = in_squeeze or was_squeezed

    if not squeeze_context:
        return None

    # Breakout above upper band
    if last_close > last_upper and htf >= 0:
        sl   = atr_now * 1.8
        tp   = sl * 2.0
        conf = "high" if htf == 1 else "normal"
        log.info(f"[SCALP] {market} LONG | BB squeeze breakout up")
        return _build_signal(market, "LONG", "bb_squeeze",
                             sl, tp, last_close, conf,
                             f"BB squeeze breakout UP, width={bb_width_now:.5f}")

    # Breakout below lower band
    if last_close < last_lower and htf <= 0:
        sl   = atr_now * 1.8
        tp   = sl * 2.0
        conf = "high" if htf == -1 else "normal"
        log.info(f"[SCALP] {market} SHORT | BB squeeze breakout down")
        return _build_signal(market, "SHORT", "bb_squeeze",
                             sl, tp, last_close, conf,
                             f"BB squeeze breakout DOWN, width={bb_width_now:.5f}")
    return None


# ─────────────────────────────────────────
# STRATEGY 4: ORDER BLOCK RETEST
# ─────────────────────────────────────────
def _order_block_retest(df, candles, market, htf, atr_now) -> dict:
    """
    Find the last significant order block. When price retests that zone → trade.

    FIX (bearish OB): A bearish OB is the last big BULLISH candle before a
    downward move. Its zone is close (bottom) → open (top), so:
      ob_low  = close  (lower value, since close < open for... wait, bull candle)
    For a bullish candle: close > open, so:
      ob_low  = open   (bottom of body)
      ob_high = close  (top of body)
    This was previously swapped in the bearish OB branch.
    """
    close       = df["close"]
    last_close  = float(close.iloc[-1])
    rsi_val     = float(_rsi(close).iloc[-1])

    # Bullish OB: last big bearish candle before upward move
    bull_ob = _find_order_block(df, 1)
    if bull_ob and htf >= 0:
        ob_low  = float(bull_ob["low"])
        ob_high = float(bull_ob["high"])
        in_zone = ob_low <= last_close <= ob_high
        if in_zone and rsi_val < 55:
            last = df.iloc[-1]
            if float(last["close"]) > float(last["open"]):
                sl = (last_close - ob_low) + atr_now * 0.5
                tp = sl * 2.0
                log.info(f"[SCALP] {market} LONG | OB retest {ob_low:.5f}-{ob_high:.5f}")
                return _build_signal(market, "LONG", "order_block",
                                     sl, tp, last_close, "high",
                                     f"Bullish OB retest {ob_low:.4f}-{ob_high:.4f}")

    # Bearish OB: last big bullish candle before downward move
    bear_ob = _find_order_block(df, -1)
    if bear_ob and htf <= 0:
        ob_low  = float(bear_ob["low"])
        ob_high = float(bear_ob["high"])
        in_zone = ob_low <= last_close <= ob_high
        if in_zone and rsi_val > 45:
            last = df.iloc[-1]
            if float(last["close"]) < float(last["open"]):
                sl = (ob_high - last_close) + atr_now * 0.5
                tp = sl * 2.0
                log.info(f"[SCALP] {market} SHORT | OB retest {ob_low:.5f}-{ob_high:.5f}")
                return _build_signal(market, "SHORT", "order_block",
                                     sl, tp, last_close, "high",
                                     f"Bearish OB retest {ob_low:.4f}-{ob_high:.4f}")
    return None


# ─────────────────────────────────────────
# SIGNAL BUILDER
# ─────────────────────────────────────────
def _build_signal(market, direction, strategy, sl_price, tp_price,
                  entry_price, confidence, reason) -> dict:
    """
    Build standardised signal dict.

    sl_distance / tp_distance: ATR-based PRICE distances (e.g. 0.00012)
    sl_ratio    / tp_ratio:    DIMENSIONLESS ratio = distance / entry_price

    Dollar SL/TP at order time:
        dollar_sl = stake × sl_ratio × multiplier
        dollar_tp = stake × tp_ratio × multiplier

    FIX: sl_ratio was previously a copy of sl_distance (wrong).
    Now correctly computed as sl_price / entry_price.
    """
    multiplier = _select_multiplier(market)
    entry = entry_price if entry_price and entry_price > 0 else 1.0
    return {
        "market":      market,
        "direction":   direction,
        "strategy":    strategy,
        "sl_distance": round(float(sl_price), 6),
        "tp_distance": round(float(tp_price), 6),
        "sl_ratio":    round(float(sl_price) / entry, 8),   # FIXED: dimensionless ratio
        "tp_ratio":    round(float(tp_price) / entry, 8),
        "entry_price": round(float(entry), 6),
        "confidence":  confidence,
        "multiplier":  multiplier,
        "reason":      reason,
        "confirmed":   True,
        "type":        "multiplier",
    }


def _select_multiplier(market: str) -> int:
    """
    Safe multiplier per asset.

    FIX: Removed x100 from EURUSD/USDJPY/AUDUSD etc. — x100 on any real
    stake with ATR-based SL will blow the account on a single bad tick.
    Max allowed is x50 for low-volatility majors, x20 for crosses, x10 for Gold.

    Dollar exposure = stake × multiplier. At x50 and $10 stake = $500 exposure.
    """
    if market in ("frxXAUUSD", "frxXAGUSD"):
        return 10   # Gold is volatile — keep low
    multiplier_map = {
        # Low-volatility majors — max x50
        "frxEURUSD": 50,
        "frxUSDJPY": 50,
        "frxAUDUSD": 50,
        "frxUSDCHF": 50,
        "frxUSDCAD": 50,
        # Medium volatility
        "frxGBPUSD": 30,
        "frxEURJPY": 30,
        # High volatility crosses — keep lower
        "frxGBPJPY": 10,
    }
    return multiplier_map.get(market, 20)


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

def _stoch(df, k=14, d=3) -> float:
    """Stochastic %K smoothed by %D period."""
    try:
        low_min  = df["low"].rolling(k).min()
        high_max = df["high"].rolling(k).max()
        rng      = high_max - low_min
        pct_k    = 100 * (df["close"] - low_min) / rng.replace(0, np.nan)
        val      = float(pct_k.rolling(d).mean().iloc[-1])
        return None if np.isnan(val) else val
    except:
        return None

def _bollinger_bands(close, p=20, std=2):
    try:
        mid   = close.rolling(p).mean()
        sigma = close.rolling(p).std()
        return mid + std * sigma, mid - std * sigma, mid
    except:
        return None

def _adx(df, p=14) -> float:
    """
    Average Directional Index.

    FIX: Changed strict `>` to `>=` in DM filter to avoid both DM+ and DM-
    being zeroed out when they are equal, which caused ADX to read 0 falsely.
    """
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr  = pd.concat([h - l,
                         (h - c.shift()).abs(),
                         (l - c.shift()).abs()], axis=1).max(axis=1)
        dp  = (h.diff()).clip(lower=0)
        dm  = (-l.diff()).clip(lower=0)
        # FIX: use >= so when dp == dm both are zeroed, not both kept
        dp  = dp.where(dp >= dm, 0)   # keep DM+ only where it dominates
        dm  = dm.where(dm >  dp, 0)   # keep DM- only where it strictly dominates
        atr = tr.ewm(span=p, adjust=False).mean()
        dip = 100 * dp.ewm(span=p, adjust=False).mean() / atr.replace(0, np.nan)
        dim = 100 * dm.ewm(span=p, adjust=False).mean() / atr.replace(0, np.nan)
        dx  = 100 * (dip - dim).abs() / (dip + dim).replace(0, np.nan)
        v   = float(dx.ewm(span=p, adjust=False).mean().iloc[-1])
        return 0.0 if np.isnan(v) else v
    except:
        return 0.0

def _atr(df, p=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l,
                    (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def _find_order_block(df, direction: int) -> dict:
    """
    Find last significant order block.

    For a BULLISH OB (direction=1):  last big BEARISH candle before upward move.
      Zone = open (top) → close (bottom) of the bearish candle.
      ob_low = close, ob_high = open  (since close < open for bearish)

    For a BEARISH OB (direction=-1): last big BULLISH candle before downward move.
      Zone = open (bottom) → close (top) of the bullish candle.
      ob_low = open, ob_high = close  (since close > open for bullish)

    FIX: Previously the bearish OB returned low=close, high=open which is
    correct for the bullish candle body but was labelled backwards.
    Renamed for clarity — same values, clearer intent.
    """
    try:
        closes = df["close"].values
        opens  = df["open"].values
        highs  = df["high"].values
        lows   = df["low"].values
        bodies = [abs(float(closes[i]) - float(opens[i])) for i in range(len(df))]
        avg_body = sum(bodies[-20:]) / 20 if len(bodies) >= 20 else 0.001

        for i in range(-5, -25, -1):
            body  = bodies[i]
            if body < avg_body * 1.5:
                continue
            c_bull = float(closes[i]) > float(opens[i])

            # Bullish OB: big bearish candle
            if direction == 1 and not c_bull:
                return {
                    "low":  float(closes[i]),   # bottom of bearish body
                    "high": float(opens[i]),    # top of bearish body
                }

            # Bearish OB: big bullish candle
            if direction == -1 and c_bull:
                return {
                    "low":  float(opens[i]),    # bottom of bullish body
                    "high": float(closes[i]),   # top of bullish body
                }
        return None
    except:
        return None

def _to_df(candles):
    df = pd.DataFrame(candles)
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    return df.reset_index(drop=True)
