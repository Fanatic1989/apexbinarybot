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
    avg_gain = gain.ewm(span=period, adjust=False).mean()   # EWM smoothing (more accurate than rolling)
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
    Analyse a list of candles for a given market.

    Args:
        candles : list of dicts {open, high, low, close, epoch}
        market  : symbol string e.g. "R_75"

    Returns:
        signal dict:
        {
            "market":     str,
            "direction":  "CALL" | "PUT" | "NONE",
            "confidence": "high" | "normal" | "low",
            "confirmed":  bool,
            "score":      int,
            "reasons":    list,
            "expiry":     int
        }
        or None if data is insufficient.
    """
    # ── Data validation ──────────────────
    if not candles or len(candles) < 30:
        log.warning(f"[STRATEGY] {market} — insufficient candles ({len(candles) if candles else 0})")
        return None

    # ── Build DataFrame ──────────────────
    try:
        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.dropna(subset=["open", "high", "low", "close"], inplace=True)
        df.reset_index(drop=True, inplace=True)
    except Exception as e:
        log.error(f"[STRATEGY] {market} — DataFrame build error: {e}")
        return None

    if len(df) < 30:
        log.warning(f"[STRATEGY] {market} — too many NaN rows after clean ({len(df)} left)")
        return None

    # ── Calculate indicators ─────────────
    df["ema9"]  = _ema(df["close"], 9)
    df["ema21"] = _ema(df["close"], 21)
    df["rsi"]   = _rsi(df["close"], 14)
    df          = _bollinger(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # ── Guard: NaN in indicators ─────────
    for field in ["ema9", "ema21", "rsi", "bb_mid", "bb_width"]:
        if pd.isna(last[field]):
            log.debug(f"[STRATEGY] {market} — NaN in {field}, skipping.")
            return _no_signal(market)

    # ─────────────────────────────────────
    # Filter 1: RSI extreme zones
    # ─────────────────────────────────────
    rsi_val = float(last["rsi"])
    if rsi_val > 70 or rsi_val < 30:
        log.debug(f"[STRATEGY] {market} — RSI extreme ({rsi_val:.1f}), no trade.")
        return _no_signal(market)

    # ─────────────────────────────────────
    # Filter 2: EMA crossover
    # ─────────────────────────────────────
    ema_cross_up   = (float(prev["ema9"]) <= float(prev["ema21"]) and
                      float(last["ema9"])  >  float(last["ema21"]))

    ema_cross_down = (float(prev["ema9"]) >= float(prev["ema21"]) and
                      float(last["ema9"])  <  float(last["ema21"]))

    # ─────────────────────────────────────
    # Filter 3: Candle direction
    # ─────────────────────────────────────
    bullish = float(last["close"]) > float(last["open"])
    bearish = float(last["close"]) < float(last["open"])

    # ─────────────────────────────────────
    # Filter 4: Bollinger squeeze
    # ─────────────────────────────────────
    avg_width    = float(df["bb_width"].mean())
    recent_width = float(df["bb_width"].iloc[-5:].mean())
    squeeze      = recent_width < avg_width * 0.8

    # ─────────────────────────────────────
    # Special logic: Boom & Crash
    # ─────────────────────────────────────
    if market in ("BOOM500", "BOOM1000"):
        return _boom_signal(df, market, rsi_val, candles)

    if market in ("CRASH500", "CRASH1000"):
        return _crash_signal(df, market, rsi_val, candles)

    # ─────────────────────────────────────
    # Special logic: Jump indices
    # ─────────────────────────────────────
    if market.startswith("JD"):
        return _jump_signal(df, market, candles)

    # ─────────────────────────────────────
    # Standard signal: CALL
    # ─────────────────────────────────────
    if ema_cross_up and (45 <= rsi_val <= 65) and bullish:
        raw_signal = {
            "market":    market,
            "direction": "CALL",
            "expiry":    config.get_expiry(market)
        }
        confirmed = sniper_confirm(candles, raw_signal)
        _log_signal(market, "CALL", confirmed, squeeze)
        return confirmed

    # ─────────────────────────────────────
    # Standard signal: PUT
    # ─────────────────────────────────────
    if ema_cross_down and (35 <= rsi_val <= 55) and bearish:
        raw_signal = {
            "market":    market,
            "direction": "PUT",
            "expiry":    config.get_expiry(market)
        }
        confirmed = sniper_confirm(candles, raw_signal)
        _log_signal(market, "PUT", confirmed, squeeze)
        return confirmed

    return _no_signal(market)


# ─────────────────────────────────────────
# Boom index signal logic
# ─────────────────────────────────────────
def _boom_signal(df: pd.DataFrame, market: str, rsi_val: float, candles: list) -> dict:
    """
    Boom indices trend down between spikes then spike UP.
    Strategy: buy CALL during downward drift when RSI is oversold.
    """
    last    = df.iloc[-1]
    ema9    = float(last["ema9"])
    ema21   = float(last["ema21"])
    drifting_down = ema9 < ema21

    if drifting_down and rsi_val < 40:
        raw = {"market": market, "direction": "CALL", "expiry": config.get_expiry(market)}
        confirmed = sniper_confirm(candles, raw)
        log.info(f"[STRATEGY] {market} — BOOM CALL signal (RSI={rsi_val:.1f})")
        return confirmed

    return _no_signal(market)


# ─────────────────────────────────────────
# Crash index signal logic
# ─────────────────────────────────────────
def _crash_signal(df: pd.DataFrame, market: str, rsi_val: float, candles: list) -> dict:
    """
    Crash indices trend up between spikes then spike DOWN.
    Strategy: buy PUT during upward drift when RSI is overbought.
    """
    last    = df.iloc[-1]
    ema9    = float(last["ema9"])
    ema21   = float(last["ema21"])
    drifting_up = ema9 > ema21

    if drifting_up and rsi_val > 60:
        raw = {"market": market, "direction": "PUT", "expiry": config.get_expiry(market)}
        confirmed = sniper_confirm(candles, raw)
        log.info(f"[STRATEGY] {market} — CRASH PUT signal (RSI={rsi_val:.1f})")
        return confirmed

    return _no_signal(market)


# ─────────────────────────────────────────
# Jump index signal logic
# ─────────────────────────────────────────
def _jump_signal(df: pd.DataFrame, market: str, candles: list) -> dict:
    """
    Jump indices have random spikes 30× normal volatility.
    Strategy: detect spike from last candle, trade MEAN REVERSION after it.
    """
    if len(df) < 5:
        return _no_signal(market)

    last       = df.iloc[-1]
    avg_body   = (df["close"] - df["open"]).abs().tail(20).mean()
    last_body  = abs(float(last["close"]) - float(last["open"]))

    # Spike detected: last candle body is 5× the average
    if avg_body > 0 and last_body > avg_body * 5:
        spike_up   = float(last["close"]) > float(last["open"])
        direction  = "PUT" if spike_up else "CALL"   # mean reversion opposite the spike
        raw = {"market": market, "direction": direction, "expiry": config.get_expiry(market)}
        confirmed = sniper_confirm(candles, raw)
        log.info(f"[STRATEGY] {market} — JUMP spike detected → {direction} reversion")
        return confirmed

    return _no_signal(market)


# ─────────────────────────────────────────
# Helpers
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


def _log_signal(market, direction, confirmed: dict, squeeze: bool):
    conf  = confirmed.get("confidence", "normal")
    score = confirmed.get("score", 0)
    tag   = "🔥 HIGH" if conf == "high" else "⚡ NORMAL"
    bb    = " + BB SQUEEZE" if squeeze else ""
    log.info(
        f"[STRATEGY] {market} — {direction} {tag} | "
        f"Score: {score}/5{bb}"
    )
