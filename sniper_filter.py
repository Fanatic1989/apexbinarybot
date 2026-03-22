"""
Sniper Confirmation Filter v6

Key fixes over v5:
  - bb% passed from strategy into sniper so checks aren't duplicated
  - Exhaustion check is context-aware: at band extremes, 3 same-direction
    candles is CONFIRMATION not exhaustion (price is being pushed there)
  - Mid-band check threshold widened slightly (0.30-0.70) to stop
    rejecting legitimate near-extreme signals
  - ATR threshold raised from 2.5x to 3.0x — 2.5x was too aggressive
    on volatile synthetics like JD100

3 checks, same scoring:
  Score 3/3 = HIGH confidence, confirmed
  Score 2/3 = NORMAL confidence, confirmed
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

    # Pre-calculate bb_pct once — used by multiple checks
    bb_pct = _calc_bb_pct(df)

    # Check 1: Price not in the "no edge" middle zone
    passed, reason = _not_mid_band(bb_pct, direction)
    reasons.append(reason)
    if passed: score += 1

    # Check 2: Exhaustion check — context-aware
    # At band extremes, same-direction candles = confirmation not exhaustion
    passed, reason = _no_exhaustion(df, direction, bb_pct)
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
             f"{'✅' if confirmed else '❌'} Score: {score}/3 | "
             f"{confidence.upper()} | bb%={bb_pct:.2f} | "
             f"{' | '.join(reasons)}")

    return {**signal, "confirmed": confirmed, "confidence": confidence,
            "score": score, "reasons": reasons}


def _calc_bb_pct(df) -> float:
    """Calculate where price sits within the BB bands (0=lower, 1=upper)."""
    try:
        close = df["close"]
        mid   = close.rolling(20).mean()
        std   = close.rolling(20).std()
        upper = mid + 2*std
        lower = mid - 2*std
        rng   = float(upper.iloc[-1] - lower.iloc[-1])
        if rng == 0:
            return 0.5
        return float((close.iloc[-1] - lower.iloc[-1]) / rng)
    except:
        return 0.5


def _not_mid_band(bb_pct: float, direction: str) -> tuple:
    """
    Price in the middle of BB bands = no clear bias = skip.

    v6 changes:
    - Widened from (0.35-0.65) to (0.30-0.70) — was too tight,
      rejecting near-extreme signals that strategy already validated
    - Price OUTSIDE bands (bb_pct > 1.0 or < 0.0) always passes —
      these are the strongest signals, never reject them here
    """
    # Outside bands = strongest possible signal, always pass
    if bb_pct > 1.0 or bb_pct < 0.0:
        return True, f"BB ✓ outside band {bb_pct:.2f}"

    # Clear extremes — high confidence zone
    if bb_pct >= 0.80 or bb_pct <= 0.20:
        return True, f"BB ✓ extreme {bb_pct:.2f}"

    # Near extremes — valid but not ideal
    if bb_pct >= 0.70 or bb_pct <= 0.30:
        return True, f"BB ✓ near extreme {bb_pct:.2f}"

    # Middle zone — no edge
    return False, f"BB ✗ mid-zone {bb_pct:.2f} — no edge"


def _no_exhaustion(df, direction: str, bb_pct: float) -> tuple:
    """
    Check the move hasn't already exhausted itself.

    v6 FIX — context-aware:
    At band EXTREMES (bb% < 0.15 or > 0.85), consecutive same-direction
    candles mean price is being PUSHED to the extreme = confirmation.
    Only flag exhaustion when in the middle zone where a spike has
    already made the move and is likely to reverse.

    Exhaustion is only relevant when:
    - bb% is between 0.30-0.70 (middle) AND
    - 3 strong consecutive candles in signal direction
    """
    try:
        last3      = df.tail(3)
        bull_count = sum(
            1 for _, c in last3.iterrows()
            if float(c["close"]) > float(c["open"])
        )
        bear_count = 3 - bull_count

        avg_body  = float((df["close"]-df["open"]).abs().tail(20).mean())
        last3_avg = float((last3["close"]-last3["open"]).abs().mean())
        strong    = last3_avg > avg_body * 1.3

        # At extremes: same-direction = confirmation, not exhaustion
        at_extreme = bb_pct > 0.85 or bb_pct < 0.15
        if at_extreme:
            return True, f"Extreme zone ✓ — candle direction is confirmation"

        # Outside bands: definitely confirmation
        if bb_pct > 1.0 or bb_pct < 0.0:
            return True, f"Outside band ✓ — candle direction is confirmation"

        # Middle zone exhaustion check
        if direction == "CALL" and bull_count == 3 and strong:
            return False, "Exhaustion ✗ 3 bull candles in mid-zone — move spent"
        if direction == "PUT"  and bear_count == 3 and strong:
            return False, "Exhaustion ✗ 3 bear candles in mid-zone — move spent"

        return True, "No exhaustion ✓"

    except Exception as e:
        return True, f"Exhaustion skip: {e}"


def _atr_calm(df) -> tuple:
    """
    ATR not spiking — not in extreme volatility.

    v6: Raised threshold from 2.5x to 3.0x.
    JD100/R_100 naturally have higher ATR variance — 2.5x was
    rejecting normal volatility on these instruments.
    """
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr  = pd.concat([
            h-l, (h-c.shift()).abs(), (l-c.shift()).abs()
        ], axis=1).max(axis=1)
        now   = float(tr.iloc[-1])
        avg   = float(tr.tail(20).mean())
        ratio = now/avg if avg > 0 else 1.0
        passed = ratio < 3.0   # raised from 2.5
        return passed, f"ATR {'✓' if passed else '✗'} ({ratio:.1f}x avg)"
    except Exception as e:
        return True, f"ATR skip: {e}"


def _to_df(candles):
    df = pd.DataFrame(candles)
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    return df.reset_index(drop=True)


def _reject(signal, reason):
    log.debug(f"[SNIPER] Rejected: {reason}")
    return {**signal, "confirmed": False, "confidence": "low",
            "score": 0, "reasons": [reason]}
