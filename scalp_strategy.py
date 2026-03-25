"""
Apex Scalping Strategy Engine v1.0
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
        # Bullish: price above 50 EMA, 50 EMA above 200 EMA
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
    Signal includes: direction, strategy, sl_pips, tp_pips,
                     confidence, multiplier, entry_reason
    """
    if not candles or len(candles) < 40:
        return None
    try:
        df = _to_df(candles)
    except Exception as e:
        log.error(f"[SCALP] {market}: {e}")
        return None
    if len(df) < 60:
        return None

    # Session filter
    if not _in_trading_session(market):
        return None

    # ATR dead market filter
    atr_series = _atr(df)
    atr_now    = float(atr_series.iloc[-1])
    atr_avg    = float(atr_series.tail(50).mean())
    atr_ratio  = atr_now / atr_avg if atr_avg > 0 else 1.0
    if atr_ratio < 0.20:
        log.debug(f"[SCALP] {market} dead market ATR ratio={atr_ratio:.2f}")
        return None

    htf = _get_htf_bias(market)
    log.info(f"[SCALP] {market} | candles={len(df)} ATR={atr_now:.5f} "
             f"ratio={atr_ratio:.2f} HTF={'bull' if htf==1 else 'bear' if htf==-1 else 'neut'}")


    # Run strategies in priority order
    signal = (_ema_trend_follow(df, candles, market, htf, atr_now) or
              _rsi_stoch_reversal(df, candles, market, htf, atr_now) or
              _bb_squeeze_breakout(df, candles, market, htf, atr_now) or
              _order_block_retest(df, candles, market, htf, atr_now))

    return signal


def _in_trading_session(market: str) -> bool:
    """
    Gold: 24/7 — trades globally around the clock
    JPY pairs: Asian session (00-09 UTC) + London/NY
    All other forex: London (07-16 UTC) + NY (12-22 UTC)
    """
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
    HTF bias must agree.
    """
    close = df["close"]
    e9    = _ema(close, 9)
    e21   = _ema(close, 21)
    e50   = float(_ema(close, 50).iloc[-1])

    e9_now  = float(e9.iloc[-1])
    e9_prev = float(e9.iloc[-2])
    e21_now = float(e21.iloc[-1])
    e21_prev= float(e21.iloc[-2])

    last_close = float(close.iloc[-1])
    rsi_val    = float(_rsi(close).iloc[-1])
    adx_val    = _adx(df)

    crossed_up   = e9_prev <= e21_prev and e9_now > e21_now
    crossed_down = e9_prev >= e21_prev and e9_now < e21_now

    log.debug(f"[EMA] {market} ADX={adx_val:.1f} RSI={rsi_val:.1f} "
              f"HTF={htf} cross_up={crossed_up} cross_dn={crossed_down} "
              f"e9={e9_now:.5f} e21={e21_now:.5f} e50={e50:.5f}")

    # Need ADX > 15 to confirm trend is real (lowered from 20)
    if adx_val < 15:
        return None

    # Bullish cross: 9 EMA crossed above 21 EMA
    crossed_up   = e9_prev <= e21_prev and e9_now > e21_now
    # Bearish cross: 9 EMA crossed below 21 EMA
    crossed_down = e9_prev >= e21_prev and e9_now < e21_now

    if crossed_up and e9_now > e50 and e21_now > e50:
        if htf >= 0 and 40 <= rsi_val <= 70:
            sl = atr_now * 1.5
            tp = sl * 2.0
            log.info(f"[SCALP] {market} LONG | EMA cross bullish | ADX={adx_val:.1f} RSI={rsi_val:.1f}")
            return _build_signal(market, "LONG", "ema_trend",
                                 sl, tp, "high" if htf == 1 else "normal",
                                 f"9/21 EMA cross UP, above 50 EMA, ADX={adx_val:.1f}")

    if crossed_down and e9_now < e50 and e21_now < e50:
        if htf <= 0 and 30 <= rsi_val <= 60:
            sl = atr_now * 1.5
            tp = sl * 2.0
            log.info(f"[SCALP] {market} SHORT | EMA cross bearish | ADX={adx_val:.1f} RSI={rsi_val:.1f}")
            return _build_signal(market, "SHORT", "ema_trend",
                                 sl, tp, "high" if htf == -1 else "normal",
                                 f"9/21 EMA cross DOWN, below 50 EMA, ADX={adx_val:.1f}")
    return None


# ─────────────────────────────────────────
# STRATEGY 2: RSI/STOCH REVERSAL
# ─────────────────────────────────────────
def _rsi_stoch_reversal(df, candles, market, htf, atr_now) -> dict:
    """
    RSI < 30 + Stoch < 20 + bullish candle at support → LONG
    RSI > 70 + Stoch > 80 + bearish candle at resistance → SHORT
    Only trade reversals aligned with HTF bias.
    """
    close   = df["close"]
    rsi     = _rsi(close)
    rsi_now = float(rsi.iloc[-1])
    rsi_prev= float(rsi.iloc[-2])
    stoch   = _stoch(df)

    last      = df.iloc[-1]
    prev      = df.iloc[-2]
    last_bull = float(last["close"]) > float(last["open"])
    last_bear = float(last["close"]) < float(last["open"])

    # RSI turning up from oversold + Stoch oversold + bullish candle
    if (rsi_now < 35 and rsi_now > rsi_prev and
            stoch and stoch < 30 and last_bull and htf >= 0):
        sl = atr_now * 1.5
        tp = sl * 2.2  # slightly better RR for reversals
        log.info(f"[SCALP] {market} LONG | RSI reversal {rsi_now:.1f} Stoch {stoch:.1f}")
        return _build_signal(market, "LONG", "rsi_reversal",
                             sl, tp, "high",
                             f"RSI {rsi_now:.1f} oversold turning up, Stoch {stoch:.1f}")

    # RSI turning down from overbought + Stoch overbought + bearish candle
    if (rsi_now > 65 and rsi_now < rsi_prev and
            stoch and stoch > 70 and last_bear and htf <= 0):
        sl = atr_now * 1.5
        tp = sl * 2.2
        log.info(f"[SCALP] {market} SHORT | RSI reversal {rsi_now:.1f} Stoch {stoch:.1f}")
        return _build_signal(market, "SHORT", "rsi_reversal",
                             sl, tp, "high",
                             f"RSI {rsi_now:.1f} overbought turning down, Stoch {stoch:.1f}")
    return None


# ─────────────────────────────────────────
# STRATEGY 3: BB SQUEEZE BREAKOUT
# ─────────────────────────────────────────
def _bb_squeeze_breakout(df, candles, market, htf, atr_now) -> dict:
    """
    Bollinger Band width contracts to 6-month low (squeeze).
    When price breaks out of squeeze in trend direction → trade.
    """
    close = df["close"]
    bb    = _bollinger_bands(close)
    if bb is None:
        return None

    upper, lower, mid = bb
    bb_width_now  = float(upper.iloc[-1] - lower.iloc[-1])
    bb_width_avg  = float((upper - lower).tail(50).mean())
    bb_width_prev = float(upper.iloc[-2] - lower.iloc[-2])

    # Squeeze: current width < 70% of average
    in_squeeze = bb_width_now < bb_width_avg * 0.70

    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])

    if not in_squeeze:
        # Check if we JUST exited squeeze (breakout)
        was_squeezed = bb_width_prev < bb_width_avg * 0.70
        if not was_squeezed:
            return None

    # Breakout above upper band in squeeze
    if last_close > last_upper and htf >= 0:
        sl = atr_now * 1.8  # wider SL for breakouts
        tp = sl * 2.0
        conf = "high" if htf == 1 else "normal"
        log.info(f"[SCALP] {market} LONG | BB squeeze breakout up")
        return _build_signal(market, "LONG", "bb_squeeze",
                             sl, tp, conf,
                             f"BB squeeze breakout UP, width={bb_width_now:.5f}")

    # Breakout below lower band in squeeze
    if last_close < last_lower and htf <= 0:
        sl = atr_now * 1.8
        tp = sl * 2.0
        conf = "high" if htf == -1 else "normal"
        log.info(f"[SCALP] {market} SHORT | BB squeeze breakout down")
        return _build_signal(market, "SHORT", "bb_squeeze",
                             sl, tp, conf,
                             f"BB squeeze breakout DOWN, width={bb_width_now:.5f}")
    return None


# ─────────────────────────────────────────
# STRATEGY 4: ORDER BLOCK RETEST
# ─────────────────────────────────────────
def _order_block_retest(df, candles, market, htf, atr_now) -> dict:
    """
    Find the last significant order block (large bullish/bearish candle
    before a strong move). When price retests that zone → trade.
    """
    close      = df["close"]
    last_close = float(close.iloc[-1])
    rsi_val    = float(_rsi(close).iloc[-1])

    # Find bullish order block (last big bearish candle before upward move)
    bull_ob = _find_order_block(df, 1)
    if bull_ob and htf >= 0:
        ob_low  = float(bull_ob["low"])
        ob_high = float(bull_ob["high"])
        in_zone = ob_low <= last_close <= ob_high
        if in_zone and rsi_val < 55:
            last = df.iloc[-1]
            if float(last["close"]) > float(last["open"]):  # bullish confirmation
                sl = last_close - ob_low + atr_now * 0.5
                tp = sl * 2.0
                log.info(f"[SCALP] {market} LONG | OB retest {ob_low:.5f}-{ob_high:.5f}")
                return _build_signal(market, "LONG", "order_block",
                                     sl, tp, "high",
                                     f"Bullish OB retest {ob_low:.4f}-{ob_high:.4f}")

    # Find bearish order block (last big bullish candle before downward move)
    bear_ob = _find_order_block(df, -1)
    if bear_ob and htf <= 0:
        ob_low  = float(bear_ob["low"])
        ob_high = float(bear_ob["high"])
        in_zone = ob_low <= last_close <= ob_high
        if in_zone and rsi_val > 45:
            last = df.iloc[-1]
            if float(last["close"]) < float(last["open"]):  # bearish confirmation
                sl = ob_high - last_close + atr_now * 0.5
                tp = sl * 2.0
                log.info(f"[SCALP] {market} SHORT | OB retest {ob_low:.5f}-{ob_high:.5f}")
                return _build_signal(market, "SHORT", "order_block",
                                     sl, tp, "high",
                                     f"Bearish OB retest {ob_low:.4f}-{ob_high:.4f}")
    return None


# ─────────────────────────────────────────
# SIGNAL BUILDER
# ─────────────────────────────────────────
def _build_signal(market, direction, strategy, sl_price, tp_price,
                  confidence, reason) -> dict:
    """
    Build standardised signal dict for the scalping bot.

    sl_price / tp_price are ATR-based PRICE DISTANCES (e.g. 0.00012).
    Deriv Multipliers take dollar SL/TP amounts, so we calculate:

      Dollar SL = stake × (sl_distance / entry_price) × multiplier
      Dollar TP = stake × (tp_distance / entry_price) × multiplier

    Since we don't know stake at signal time, we store the ATR distances
    and let the bot calculate dollar amounts at order time.
    """
    multiplier = _select_multiplier(market)
    # Store both: raw ATR distance AND ratio for dollar conversion at order time
    return {
        "market":     market,
        "direction":  direction,
        "strategy":   strategy,
        "sl_distance": round(float(sl_price), 6),   # ATR-based price distance
        "tp_distance": round(float(tp_price), 6),   # ATR-based price distance
        "sl_ratio":   round(float(sl_price), 6),    # kept for compatibility
        "tp_ratio":   round(float(tp_price), 6),
        "confidence": confidence,
        "multiplier": multiplier,
        "reason":     reason,
        "confirmed":  True,
        "type":       "multiplier",
    }


def _select_multiplier(market: str) -> int:
    """
    Select appropriate multiplier based on asset volatility.
    Lower multiplier = safer, higher = more profit per pip.
    Bot will use a fixed conservative multiplier per asset type.
    """
    # Gold is more volatile — use lower multiplier
    if market in ("frxXAUUSD", "frxXAGUSD"):
        return 10
    # Major forex pairs
    multiplier_map = {
        "frxEURUSD": 100,
        "frxGBPUSD": 50,
        "frxUSDJPY": 100,
        "frxAUDUSD": 100,
        "frxUSDCHF": 100,
        "frxUSDCAD": 100,
        "frxGBPJPY": 20,
        "frxEURJPY": 50,
    }
    return multiplier_map.get(market, 50)


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
    """Stochastic %K"""
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
        return mid + std*sigma, mid - std*sigma, mid
    except:
        return None

def _adx(df, p=14) -> float:
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr  = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
        dp  = (h.diff()).clip(lower=0)
        dm  = (-l.diff()).clip(lower=0)
        dp  = dp.where(dp > dm, 0)
        dm  = dm.where(dm > dp, 0)
        atr = tr.ewm(span=p, adjust=False).mean()
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

def _find_order_block(df, direction: int) -> dict:
    """Find last significant order block in given direction."""
    try:
        closes = df["close"].values
        opens  = df["open"].values
        highs  = df["high"].values
        lows   = df["low"].values
        bodies = [abs(float(closes[i]) - float(opens[i])) for i in range(len(df))]
        avg_body = sum(bodies[-20:]) / 20 if len(bodies) >= 20 else 0.001

        for i in range(-5, -25, -1):
            body = bodies[i]
            if body < avg_body * 1.5:
                continue
            c_bull = float(closes[i]) > float(opens[i])
            # Bullish OB: last big bearish candle before upward move
            if direction == 1 and not c_bull:
                return {
                    "low":  float(opens[i]),
                    "high": float(closes[i]),
                }
            # Bearish OB: last big bullish candle before downward move
            if direction == -1 and c_bull:
                return {
                    "low":  float(closes[i]),
                    "high": float(opens[i]),
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
