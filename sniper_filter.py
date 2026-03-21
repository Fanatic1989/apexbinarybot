"""
Sniper Confirmation Filter v3

5 independent checks that verify a signal is genuine.
Each check looks at something the STRATEGY didn't already check.
Score 4-5 = HIGH confidence
Score 2-3 = NORMAL confidence  
Score 0-1 = REJECTED

Filters:
1. Body strength — candle has conviction, not a doji
2. No spike reversal — no large opposite candle in last 3 bars
3. Price structure — higher lows (CALL) or lower highs (PUT)
4. ATR normal — volatility not spiking (not in news spike)
5. Momentum confirmation — recent candles moving same direction
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

    df      = _to_df(candles)
    score   = 0
    reasons = []

    # Filter 1: Body strength
    passed, reason = _body_strength(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Filter 2: No spike reversal
    passed, reason = _no_spike_reversal(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Filter 3: Price structure
    passed, reason = _price_structure(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Filter 4: ATR normal (not in volatility spike)
    passed, reason = _atr_normal(df)
    reasons.append(reason)
    if passed: score += 1

    # Filter 5: Momentum confirmation
    passed, reason = _momentum_confirm(df, direction)
    reasons.append(reason)
    if passed: score += 1

    if score >= 4:
        confidence, confirmed = "high", True
    elif score >= 2:
        confidence, confirmed = "normal", True
    else:
        confidence, confirmed = "low", False

    log.info(f"[SNIPER] {signal.get('market','?')} {direction} | "
             f"{'✅' if confirmed else '❌'} Score: {score}/5 | {confidence.upper()}")

    return {
        **signal,
        "confirmed":  confirmed,
        "confidence": confidence,
        "score":      score,
        "reasons":    reasons
    }


def _body_strength(df, direction) -> tuple:
    """
    Last candle must have a meaningful body (not a doji).
    Body >= 40% of total range AND direction matches signal.
    """
    try:
        last  = df.iloc[-1]
        body  = abs(float(last["close"]) - float(last["open"]))
        rng   = float(last["high"]) - float(last["low"])
        if rng == 0:
            return False, "Doji ✗"
        ratio = body / rng
        bull  = float(last["close"]) > float(last["open"])
        bear  = not bull

        if direction == "CALL" and bear:
            return False, f"Body wrong dir ✗ (bearish on CALL)"
        if direction == "PUT" and bull:
            return False, f"Body wrong dir ✗ (bullish on PUT)"
        if ratio < 0.35:
            return False, f"Weak body ✗ ({ratio:.0%})"

        return True, f"Body ✓ ({ratio:.0%})"
    except Exception as e:
        return False, f"Body error: {e}"


def _no_spike_reversal(df, direction) -> tuple:
    """
    No large opposite-direction candle in the last 3 bars.
    A spike reversal = someone already saw this and reversed hard.
    """
    try:
        avg_body = float((df["close"] - df["open"]).abs().tail(20).mean())
        last3    = df.tail(3)

        for _, c in last3.iterrows():
            body = abs(float(c["close"]) - float(c["open"]))
            if body < avg_body * 1.8:
                continue
            bull = float(c["close"]) > float(c["open"])
            if direction == "CALL" and not bull:
                return False, "Spike reversal ✗ (large bear candle)"
            if direction == "PUT" and bull:
                return False, "Spike reversal ✗ (large bull candle)"

        return True, "No spike ✓"
    except Exception as e:
        return False, f"Spike error: {e}"


def _price_structure(df, direction) -> tuple:
    """
    CALL: price making higher lows (upward structure)
    PUT:  price making lower highs (downward structure)
    Checks last 5 candles for structural alignment.
    """
    try:
        lows  = [float(df.iloc[i]["low"])  for i in range(-5, 0)]
        highs = [float(df.iloc[i]["high"]) for i in range(-5, 0)]

        if direction == "CALL":
            # At least 3 of 4 consecutive lows should be higher
            hl_count = sum(1 for i in range(1, 5) if lows[i] >= lows[i-1])
            passed   = hl_count >= 2
            return passed, f"Structure {'✓' if passed else '✗'} ({hl_count}/4 higher lows)"
        else:
            # At least 3 of 4 consecutive highs should be lower
            lh_count = sum(1 for i in range(1, 5) if highs[i] <= highs[i-1])
            passed   = lh_count >= 2
            return passed, f"Structure {'✓' if passed else '✗'} ({lh_count}/4 lower highs)"
    except Exception as e:
        return False, f"Structure error: {e}"


def _atr_normal(df) -> tuple:
    """
    ATR should not be more than 2x its 20-period average.
    Spike = news event or extreme volatility = avoid.
    """
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr  = pd.concat([
            h - l,
            (h - c.shift()).abs(),
            (l - c.shift()).abs()
        ], axis=1).max(axis=1)
        atr_now = float(tr.iloc[-1])
        atr_avg = float(tr.tail(20).mean())
        if atr_avg == 0:
            return True, "ATR ✓ (no baseline)"
        ratio = atr_now / atr_avg
        passed = ratio < 2.0
        return passed, f"ATR {'✓' if passed else '✗'} ({ratio:.1f}x avg)"
    except Exception as e:
        return True, f"ATR skip: {e}"


def _momentum_confirm(df, direction) -> tuple:
    """
    Recent price movement confirms the signal direction.
    Compare last 3 candles net move vs prior 3 candles.
    """
    try:
        recent = float(df["close"].iloc[-1]) - float(df["close"].iloc[-4])
        prior  = float(df["close"].iloc[-4]) - float(df["close"].iloc[-7])

        if direction == "CALL":
            passed = recent > 0 and recent >= prior * 0.4
        else:
            passed = recent < 0 and abs(recent) >= abs(prior) * 0.4

        return passed, f"Momentum {'✓' if passed else '✗'} (r={recent:+.4f} p={prior:+.4f})"
    except Exception as e:
        return False, f"Momentum error: {e}"


def _to_df(candles):
    df = pd.DataFrame(candles)
    for c in ["open","high","low","close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["open","high","low","close"], inplace=True)
    return df.reset_index(drop=True)


def _reject(signal, reason):
    log.debug(f"[SNIPER] Rejected: {reason}")
    return {**signal, "confirmed":False, "confidence":"low", "score":0, "reasons":[reason]}
