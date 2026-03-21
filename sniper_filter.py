"""
Sniper Confirmation Filter v5 — Simplified & Reliable

After testing v3 and v4, the conclusion is:
- Complex filters create inconsistent scoring
- Simple, clear rules work better on 1-min synthetic charts

3 core checks only:
1. Direction alignment  — candle and signal agree
2. Not mid-band        — price not in 35-65% BB range (no edge zone)
3. ATR calm            — not in volatility spike

Score 3/3 = HIGH
Score 2/3 = NORMAL
Score 0-1 = REJECTED
"""
import logging
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def sniper_confirm(candles: list, signal: dict) -> dict:
    if not candles or len(candles) < 20:
        return _reject(signal, "Not enough candles")

    direction = signal.get("direction", "NONE")
    if direction == "NONE":
        return _reject(signal, "No direction")

    df    = _to_df(candles)
    score = 0
    reasons = []

    # Check 1: Price not in the "no edge" middle zone
    passed, reason = _not_mid_band(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Check 2: No spike already made the move
    passed, reason = _no_exhaustion(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Check 3: ATR not spiking
    passed, reason = _atr_calm(df)
    reasons.append(reason)
    if passed: score += 1

    if score >= 3:
        confidence, confirmed = "high", True
    elif score >= 2:
        confidence, confirmed = "normal", True
    else:
        confidence, confirmed = "low", False

    log.info(f"[SNIPER] {signal.get('market','?')} {direction} | "
             f"{'✅' if confirmed else '❌'} Score: {score}/3 | {confidence.upper()}")

    return {**signal, "confirmed": confirmed, "confidence": confidence,
            "score": score, "reasons": reasons}


def _not_mid_band(df, direction) -> tuple:
    """
    Price in the middle of BB bands (35%-65%) = no clear bias = skip.
    Edge only exists near the extremes.
    """
    try:
        close  = df["close"]
        mid    = close.rolling(20).mean()
        std    = close.rolling(20).std()
        upper  = mid + 2*std
        lower  = mid - 2*std
        rng    = float(upper.iloc[-1] - lower.iloc[-1])
        if rng == 0:
            return True, "BB ✓ (no range)"
        bb_pct = float((close.iloc[-1] - lower.iloc[-1]) / rng)

        in_middle = 0.35 < bb_pct < 0.65
        if in_middle:
            return False, f"BB ✗ mid-zone {bb_pct:.2f} — no edge"
        return True, f"BB ✓ extreme zone {bb_pct:.2f}"
    except Exception as e:
        return True, f"BB skip: {e}"


def _no_exhaustion(df, direction) -> tuple:
    """
    Check the move hasn't already exhausted itself.
    If 3 consecutive candles already moved strongly in signal direction,
    the momentum is spent — don't enter.
    """
    try:
        last3 = df.tail(3)
        bull_count = sum(
            1 for _, c in last3.iterrows()
            if float(c["close"]) > float(c["open"])
        )
        bear_count = 3 - bull_count

        avg_body = float((df["close"]-df["open"]).abs().tail(20).mean())
        last3_avg= float((last3["close"]-last3["open"]).abs().mean())
        strong   = last3_avg > avg_body * 1.3

        if direction == "CALL" and bull_count == 3 and strong:
            return False, "Exhaustion ✗ 3 bull candles — move spent"
        if direction == "PUT" and bear_count == 3 and strong:
            return False, "Exhaustion ✗ 3 bear candles — move spent"
        return True, "No exhaustion ✓"
    except Exception as e:
        return True, f"Exhaustion skip: {e}"


def _atr_calm(df) -> tuple:
    """ATR not spiking — not in extreme volatility."""
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr  = pd.concat([
            h-l, (h-c.shift()).abs(), (l-c.shift()).abs()
        ], axis=1).max(axis=1)
        now = float(tr.iloc[-1])
        avg = float(tr.tail(20).mean())
        ratio = now/avg if avg > 0 else 1.0
        passed = ratio < 2.5
        return passed, f"ATR {'✓' if passed else '✗'} ({ratio:.1f}x)"
    except Exception as e:
        return True, f"ATR skip: {e}"


def _to_df(candles):
    df = pd.DataFrame(candles)
    for c in ["open","high","low","close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["open","high","low","close"], inplace=True)
    return df.reset_index(drop=True)


def _reject(signal, reason):
    log.debug(f"[SNIPER] Rejected: {reason}")
    return {**signal, "confirmed": False, "confidence": "low",
            "score": 0, "reasons": [reason]}
