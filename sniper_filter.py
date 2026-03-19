import logging
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def sniper_confirm(candles: list, signal: dict) -> dict:
    """
    Multi-layer confirmation filter.

    Key fix: filters now check DIFFERENT aspects than the strategy already checked.
    Strategy checks: EMA cross, RSI range, candle direction
    Sniper checks:   Trend strength, momentum consistency, price structure

    Score 3-5 = HIGH confidence
    Score 1-2 = NORMAL confidence
    Score 0   = LOW / rejected
    """
    if not candles or len(candles) < 20:
        return _reject(signal, "Not enough candle data")

    direction = signal.get("direction", "NONE")
    if direction == "NONE":
        return _reject(signal, "No direction")

    df      = _to_df(candles)
    score   = 0
    reasons = []

    # ── Filter 1: EMA50 trend alignment ──
    # Checks longer EMA not used in strategy
    passed, reason = _check_ema50_trend(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # ── Filter 2: RSI slope direction ─────
    # Not just RSI value but is it MOVING the right way
    passed, reason = _check_rsi_slope(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # ── Filter 3: Consecutive candle streak
    # 3 of last 5 candles in signal direction
    passed, reason = _check_candle_streak(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # ── Filter 4: No recent opposite spike ─
    # Rejects signals where price just reversed hard
    passed, reason = _check_no_spike_reversal(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # ── Filter 5: Price momentum strength ──
    # Recent candles moving faster than average = momentum
    passed, reason = _check_momentum_strength(df, direction)
    reasons.append(reason)
    if passed: score += 1

    # ── Confidence grading ────────────────
    # Raised thresholds — HIGH needs 4/5, not 3/5
    # This stops rubber-stamping bad signals as HIGH
    if score >= 4:
        confidence = "high"
        confirmed  = True
    elif score >= 2:
        confidence = "normal"
        confirmed  = True
    else:
        confidence = "low"
        confirmed  = False

    result = {
        **signal,
        "confirmed":  confirmed,
        "confidence": confidence,
        "score":      score,
        "reasons":    reasons
    }

    log.info(f"[SNIPER] {signal.get('market','?')} {direction} | "
             f"{'✅' if confirmed else '❌'} Score: {score}/5 | {confidence.upper()}")
    for r in reasons:
        log.debug(f"[SNIPER]   {r}")

    return result


# ─────────────────────────────────────────
# Filter 1 — EMA50 trend (longer term)
# ─────────────────────────────────────────
def _check_ema50_trend(df, direction):
    """
    Price must be on correct side of EMA50.
    EMA50 is not used in strategy so this is a genuine extra check.
    """
    try:
        ema50      = df["close"].ewm(span=50, adjust=False).mean()
        last_close = df["close"].iloc[-1]
        last_ema50 = ema50.iloc[-1]

        if direction == "CALL":
            passed = last_close > last_ema50
        else:
            passed = last_close < last_ema50

        return passed, f"EMA50 {'✓' if passed else '✗'} (price={'above' if last_close > last_ema50 else 'below'})"
    except Exception as e:
        return False, f"EMA50 error: {e}"


# ─────────────────────────────────────────
# Filter 2 — RSI slope (is RSI moving right way?)
# ─────────────────────────────────────────
def _check_rsi_slope(df, direction):
    """
    RSI must be moving in the signal direction over last 3 bars.
    CALL: RSI trending up (slope positive)
    PUT:  RSI trending down (slope negative)
    This catches situations where RSI is in range but reversing.
    """
    try:
        close = df["close"]
        delta = close.diff()
        gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = 100 - (100 / (1 + rs))

        rsi_now  = rsi.iloc[-1]
        rsi_prev = rsi.iloc[-3]   # 3 bars ago
        slope    = rsi_now - rsi_prev

        if direction == "CALL":
            passed = slope > 0.5    # RSI rising
        else:
            passed = slope < -0.5   # RSI falling

        return passed, f"RSI slope {'✓' if passed else '✗'} ({slope:+.1f} over 3 bars)"
    except Exception as e:
        return False, f"RSI slope error: {e}"


# ─────────────────────────────────────────
# Filter 3 — Candle streak
# ─────────────────────────────────────────
def _check_candle_streak(df, direction):
    """
    3 of last 5 candles must be in signal direction.
    More forgiving than requiring consecutive candles.
    """
    try:
        last5   = df.tail(5)
        bulls   = (last5["close"] > last5["open"]).sum()
        bears   = (last5["close"] < last5["open"]).sum()

        if direction == "CALL":
            passed = bulls >= 3
            return passed, f"Candle streak {'✓' if passed else '✗'} ({bulls}/5 bullish)"
        else:
            passed = bears >= 3
            return passed, f"Candle streak {'✓' if passed else '✗'} ({bears}/5 bearish)"
    except Exception as e:
        return False, f"Candle streak error: {e}"


# ─────────────────────────────────────────
# Filter 4 — No spike reversal
# ─────────────────────────────────────────
def _check_no_spike_reversal(df, direction):
    """
    Reject if the last 3 candles had a large opposite-direction candle.
    This catches fakeouts where price spiked then pulled back.
    A large opposite candle = body > 2x average body, pointing wrong way.
    """
    try:
        avg_body = (df["close"] - df["open"]).abs().tail(20).mean()
        last3    = df.tail(3)

        for _, candle in last3.iterrows():
            body = abs(candle["close"] - candle["open"])
            if body < avg_body * 2:
                continue
            # Large candle found — check direction
            candle_bull = candle["close"] > candle["open"]
            if direction == "CALL" and not candle_bull:
                return False, "Spike reversal ✗ (large bearish candle in last 3)"
            if direction == "PUT" and candle_bull:
                return False, "Spike reversal ✗ (large bullish candle in last 3)"

        return True, "No spike reversal ✓"
    except Exception as e:
        return False, f"Spike check error: {e}"


# ─────────────────────────────────────────
# Filter 5 — Momentum strength
# ─────────────────────────────────────────
def _check_momentum_strength(df, direction):
    """
    Price must be accelerating in signal direction.
    Compare last 3 candle net move vs previous 3 candle net move.
    If recent move > previous move = momentum building.
    """
    try:
        recent_move = df["close"].iloc[-1] - df["close"].iloc[-4]
        prior_move  = df["close"].iloc[-4] - df["close"].iloc[-7]

        if direction == "CALL":
            # Both moves should be up, recent stronger
            passed = recent_move > 0 and recent_move >= prior_move * 0.5
        else:
            # Both moves should be down, recent stronger
            passed = recent_move < 0 and abs(recent_move) >= abs(prior_move) * 0.5

        return passed, (f"Momentum {'✓' if passed else '✗'} "
                       f"(recent={recent_move:+.5f} prior={prior_move:+.5f})")
    except Exception as e:
        return False, f"Momentum error: {e}"


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def _to_df(candles):
    df = pd.DataFrame(candles)
    for col in ["open","high","low","close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["open","high","low","close"], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _reject(signal, reason):
    log.debug(f"[SNIPER] Rejected: {reason}")
    return {
        **signal,
        "confirmed":  False,
        "confidence": "low",
        "score":      0,
        "reasons":    [reason]
    }
