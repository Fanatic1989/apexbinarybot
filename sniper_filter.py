"""
Sniper Confirmation Filter v4 — Entry Timing Focus

The previous version rewarded LATE entries (strong momentum already in place).
This version rewards EARLY entries (setup present, move not yet happened).

5 filters focused on TIMING not confirmation:
1. RSI not exhausted  — RSI hasn't already reached extreme in signal direction
2. BB setup ready     — price near band extreme, not already blown through
3. No recent spike    — no large candle already made the move
4. Reversal candle    — last candle shows hesitation/reversal sign
5. ATR calm           — volatility not spiking (not in news spike)

Score 4-5 = HIGH (perfect setup, early entry)
Score 2-3 = NORMAL (decent setup)
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

    # Filter 1: RSI not exhausted in signal direction
    passed, reason = _rsi_not_exhausted(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Filter 2: BB position supports entry
    passed, reason = _bb_position_valid(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Filter 3: No recent large spike in signal direction (move not done)
    passed, reason = _no_prior_spike(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Filter 4: Reversal candle sign (hesitation or pin bar)
    passed, reason = _reversal_sign(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # Filter 5: ATR calm — not in volatility spike
    passed, reason = _atr_calm(df)
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

    return {**signal, "confirmed": confirmed, "confidence": confidence,
            "score": score, "reasons": reasons}


def _rsi_not_exhausted(df, direction) -> tuple:
    """
    RSI should NOT already be at extreme in signal direction.
    CALL: RSI should be low (oversold setup) but not below 15 (already bouncing)
    PUT:  RSI should be high (overbought setup) but not above 85 (already falling)
    
    We want to enter BEFORE the move, not after RSI already reversed.
    """
    try:
        close  = df["close"]
        d      = close.diff()
        g      = d.clip(lower=0).ewm(span=14, adjust=False).mean()
        l      = (-d.clip(upper=0)).ewm(span=14, adjust=False).mean()
        rsi    = float((100 - 100/(1 + g/l.replace(0, np.nan))).iloc[-1])
        rsi_prev = float((100 - 100/(1 + g/l.replace(0, np.nan))).iloc[-2])

        if direction == "CALL":
            # Good: RSI oversold 20-45 (setup ready, not yet bounced hard)
            if 15 <= rsi <= 45:
                return True, f"RSI ✓ oversold setup {rsi:.0f}"
            if rsi < 15:
                return False, f"RSI ✗ too low {rsi:.0f} — already bouncing"
            return False, f"RSI ✗ {rsi:.0f} not oversold for CALL"
        else:
            # Good: RSI overbought 55-80 (setup ready, not yet fallen hard)
            if 55 <= rsi <= 85:
                return True, f"RSI ✓ overbought setup {rsi:.0f}"
            if rsi > 85:
                return False, f"RSI ✗ too high {rsi:.0f} — already falling"
            return False, f"RSI ✗ {rsi:.0f} not overbought for PUT"
    except Exception as e:
        return False, f"RSI error: {e}"


def _bb_position_valid(df, direction) -> tuple:
    """
    Price position within Bollinger Bands.
    CALL: price in lower 30% of bands (near support)
    PUT:  price in upper 30% of bands (near resistance)
    """
    try:
        close  = df["close"]
        mid    = close.rolling(20).mean()
        std    = close.rolling(20).std()
        upper  = mid + 2*std
        lower  = mid - 2*std
        bb_pct = float(
            (close.iloc[-1] - lower.iloc[-1]) /
            (upper.iloc[-1] - lower.iloc[-1])
        )

        if direction == "CALL":
            if bb_pct <= 0.35:
                return True, f"BB ✓ lower zone {bb_pct:.2f}"
            return False, f"BB ✗ {bb_pct:.2f} not near lower band for CALL"
        else:
            if bb_pct >= 0.65:
                return True, f"BB ✓ upper zone {bb_pct:.2f}"
            return False, f"BB ✗ {bb_pct:.2f} not near upper band for PUT"
    except Exception as e:
        return False, f"BB error: {e}"


def _no_prior_spike(df, direction) -> tuple:
    """
    Check that a large move in the signal direction hasn't ALREADY happened.
    If it has, we're entering late — the move is done.
    
    CALL: no large bull candle in last 3 bars (move already happened up)
    PUT:  no large bear candle in last 3 bars (move already happened down)
    """
    try:
        avg_body = float((df["close"] - df["open"]).abs().tail(20).mean())
        last3    = df.tail(4).head(3)  # 3 candles before current

        for _, c in last3.iterrows():
            body = abs(float(c["close"]) - float(c["open"]))
            if body < avg_body * 1.5:
                continue
            bull = float(c["close"]) > float(c["open"])
            # If signal is CALL but big bull candle already happened = late
            if direction == "CALL" and bull:
                return False, f"Prior spike ✗ large bull candle before CALL"
            # If signal is PUT but big bear candle already happened = late
            if direction == "PUT" and not bull:
                return False, f"Prior spike ✗ large bear candle before PUT"

        return True, "No prior spike ✓"
    except Exception as e:
        return False, f"Spike error: {e}"


def _reversal_sign(df, direction) -> tuple:
    """
    Last candle should show hesitation or early reversal sign.
    This means the move is pausing and about to reverse.
    
    Signs: small body (doji), wick in signal direction,
    or candle moving opposite to recent trend.
    """
    try:
        last   = df.iloc[-1]
        prev   = df.iloc[-2]
        body   = abs(float(last["close"]) - float(last["open"]))
        rng    = float(last["high"]) - float(last["low"])
        bull   = float(last["close"]) > float(last["open"])
        bear   = not bull

        avg_body = float((df["close"] - df["open"]).abs().tail(10).mean())

        # Small body = doji = hesitation = reversal possible
        if rng > 0 and body / rng < 0.4:
            return True, "Doji ✓ (hesitation candle)"

        # Small body relative to recent = momentum slowing
        if body < avg_body * 0.7:
            return True, "Small body ✓ (momentum slowing)"

        # Candle counter to recent direction = early reversal
        prev_bull = float(prev["close"]) > float(prev["open"])
        if direction == "CALL" and bear:
            return True, "Counter candle ✓ (bear pause before CALL)"
        if direction == "PUT" and bull:
            return True, "Counter candle ✓ (bull pause before PUT)"

        return False, "No reversal sign ✗"
    except Exception as e:
        return False, f"Reversal error: {e}"


def _atr_calm(df) -> tuple:
    """ATR not spiking — not in news or extreme volatility event."""
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr      = pd.concat([
            h-l, (h-c.shift()).abs(), (l-c.shift()).abs()
        ], axis=1).max(axis=1)
        atr_now = float(tr.iloc[-1])
        atr_avg = float(tr.tail(20).mean())
        ratio   = atr_now / atr_avg if atr_avg > 0 else 1.0
        passed  = ratio < 2.0
        return passed, f"ATR {'✓' if passed else '✗'} ({ratio:.1f}x avg)"
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
