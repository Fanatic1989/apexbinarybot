"""
Sniper Confirmation Filter v6.1

Changes from v6.0:
  - BB check (Check 1) is now MANDATORY — if it fails the signal is
    rejected regardless of other scores. No edge = no trade.
  - This stops mid-zone NORMAL signals from passing on score 2/3
    when the BB check itself failed.

3 checks, mandatory first:
  Check 1 (BB)        — MANDATORY. Fail = rejected outright.
  Check 2 (Exhaustion)— context-aware (extreme zones = confirmation)
  Check 3 (ATR)       — not in extreme volatility spike

  Score 3/3 = HIGH confidence, confirmed
  Score 2/3 = NORMAL confidence, confirmed (only if Check 1 passed)
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

    # Pre-calculate bb_pct once
    bb_pct = _calc_bb_pct(df)

    # Check 1: BB position — MANDATORY
    bb_passed, reason = _not_mid_band(bb_pct, direction)
    reasons.append(reason)
    if bb_passed:
        score += 1
    else:
        # BB failed — no edge, reject immediately regardless of other checks
        log.info(f"[SNIPER] {signal.get('market','?')} {direction} | "
                 f"❌ Rejected — BB mid-zone {bb_pct:.2f} — no edge")
        return {**signal, "confirmed": False, "confidence": "low",
                "score": 0, "reasons": reasons}

    # Check 2: Exhaustion — context-aware
    passed, reason = _no_exhaustion(df, direction, bb_pct)
    reasons.append(reason)
    if passed: score += 1

    # Check 3: ATR calm
    passed, reason = _atr_calm(df)
    reasons.append(reason)
    if passed: score += 1

    # BB passed — now score determines confidence
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
    try:
        close = df["close"]
        mid   = close.rolling(20).mean()
        std   = close.rolling(20).std()
        upper = mid + 2*std
        lower = mid - 2*std
        rng   = float(upper.iloc[-1] - lower.iloc[-1])
        if rng == 0: return 0.5
        return float((close.iloc[-1] - lower.iloc[-1]) / rng)
    except:
        return 0.5


def _not_mid_band(bb_pct: float, direction: str) -> tuple:
    """
    Price must be outside the mid-zone to have edge.
    Outside bands (>1.0 or <0.0) = strongest, always pass.
    0.70-1.0 / 0.0-0.30 = near extremes, pass.
    0.30-0.70 = mid-zone, no edge, MANDATORY REJECT.
    """
    if bb_pct > 1.0 or bb_pct < 0.0:
        return True, f"BB ✓ outside band {bb_pct:.2f}"
    if bb_pct >= 0.80 or bb_pct <= 0.20:
        return True, f"BB ✓ extreme {bb_pct:.2f}"
    if bb_pct >= 0.70 or bb_pct <= 0.30:
        return True, f"BB ✓ near extreme {bb_pct:.2f}"
    return False, f"BB ✗ mid-zone {bb_pct:.2f} — no edge"


def _no_exhaustion(df, direction: str, bb_pct: float) -> tuple:
    """
    At extremes: same-direction candles = confirmation not exhaustion.
    Only flag exhaustion in the middle zone (0.15-0.85).
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

        # At extremes — confirmation, not exhaustion
        if bb_pct > 0.85 or bb_pct < 0.15:
            return True, "Extreme zone ✓ — candle direction is confirmation"

        if bb_pct > 1.0 or bb_pct < 0.0:
            return True, "Outside band ✓ — candle direction is confirmation"

        if direction == "CALL" and bull_count == 3 and strong:
            return False, "Exhaustion ✗ 3 bull candles in mid-zone — move spent"
        if direction == "PUT"  and bear_count == 3 and strong:
            return False, "Exhaustion ✗ 3 bear candles in mid-zone — move spent"

        return True, "No exhaustion ✓"

    except Exception as e:
        return True, f"Exhaustion skip: {e}"


def _atr_calm(df) -> tuple:
    """ATR not spiking. Threshold 3.0x (raised from 2.5x for volatile synthetics)."""
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr  = pd.concat([
            h-l, (h-c.shift()).abs(), (l-c.shift()).abs()
        ], axis=1).max(axis=1)
        now   = float(tr.iloc[-1])
        avg   = float(tr.tail(20).mean())
        ratio = now/avg if avg > 0 else 1.0
        passed = ratio < 3.0
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
