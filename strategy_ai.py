"""
AI Strategy Selector — uses Claude API to analyse market conditions
and select the optimal strategy mode for each market in real time.

Tracks per-strategy, per-market performance and feeds that data
to Claude which recommends which strategy to prioritise.
"""
import json
import time
import logging
import os
import requests
from datetime import datetime

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Strategy performance tracker
# ─────────────────────────────────────────
class StrategyTracker:
    """
    Tracks win/loss per strategy mode per market.
    Persists to disk so data survives restarts.
    """
    FILE = "strategy_performance.json"

    STRATEGIES = [
        "rsi_reversal",      # Mode 1 — highest potential
        "bb_bounce",         # Mode 2
        "ema_triple",        # Mode 3
        "false_breakout",    # Mode 4
        "momentum_streak",   # Mode 5
    ]

    def __init__(self):
        self.data = self._load()
        self.session_start = datetime.utcnow().isoformat()

    def _load(self) -> dict:
        try:
            with open(self.FILE) as f:
                return json.load(f)
        except:
            return self._empty()

    def _empty(self) -> dict:
        return {
            "strategies": {s: {"wins": 0, "losses": 0, "last_used": None}
                          for s in self.STRATEGIES},
            "markets": {},
            "ai_recommendations": [],
            "last_updated": None
        }

    def _save(self):
        try:
            self.data["last_updated"] = datetime.utcnow().isoformat()
            with open(self.FILE, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            log.error(f"[TRACKER] Save failed: {e}")

    def record(self, strategy: str, market: str, result: str):
        """Record a trade outcome for a strategy+market combo."""
        # Global strategy stats
        s = self.data["strategies"].setdefault(strategy, {"wins":0,"losses":0,"last_used":None})
        if result == "won":
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["last_used"] = datetime.utcnow().isoformat()

        # Per-market stats
        m = self.data["markets"].setdefault(market, {})
        ms = m.setdefault(strategy, {"wins":0,"losses":0})
        if result == "won":
            ms["wins"] += 1
        else:
            ms["losses"] += 1

        self._save()
        log.info(f"[TRACKER] {strategy} on {market}: {result.upper()} "
                 f"| Global: {s['wins']}W {s['losses']}L")

    def get_win_rates(self) -> dict:
        """Return win rate for each strategy."""
        rates = {}
        for name, stats in self.data["strategies"].items():
            total = stats["wins"] + stats["losses"]
            rates[name] = {
                "win_rate": round(stats["wins"]/total*100, 1) if total > 0 else None,
                "total":    total,
                "wins":     stats["wins"],
                "losses":   stats["losses"],
            }
        return rates

    def get_market_best_strategy(self, market: str) -> str:
        """Return the best performing strategy for a specific market."""
        m = self.data["markets"].get(market, {})
        best_wr   = -1
        best_strat = None
        for strat, stats in m.items():
            total = stats["wins"] + stats["losses"]
            if total < 3:
                continue   # need minimum 3 trades to judge
            wr = stats["wins"] / total
            if wr > best_wr:
                best_wr   = wr
                best_strat = strat
        return best_strat

    def get_summary(self) -> dict:
        return {
            "win_rates":  self.get_win_rates(),
            "markets":    self.data["markets"],
            "last_updated": self.data.get("last_updated"),
        }


# ─────────────────────────────────────────
# AI Strategy Selector
# ─────────────────────────────────────────
class AIStrategySelector:
    """
    Uses Claude API to analyse market candle data
    and recommend which strategy to use.

    Falls back to rule-based selection if API unavailable.
    """

    CACHE_TTL = 300  # Re-ask Claude every 5 minutes per market

    def __init__(self, tracker: StrategyTracker):
        self.tracker    = tracker
        self._cache     = {}   # {market: (strategy, timestamp)}
        self._ai_active = True

    def select_strategy(self, candles: list, market: str) -> str:
        """
        Select the best strategy for current market conditions.
        Returns one of: rsi_reversal, bb_bounce, ema_triple,
                        false_breakout, momentum_streak
        """
        # Check cache first
        if market in self._cache:
            strat, ts = self._cache[market]
            if time.time() - ts < self.CACHE_TTL:
                return strat

        # Check if this market has a proven best strategy (data-driven)
        market_best = self.tracker.get_market_best_strategy(market)
        if market_best:
            log.info(f"[AI] {market} → using proven best: {market_best}")
            self._cache[market] = (market_best, time.time())
            return market_best

        # Use Claude AI to analyse current conditions
        if self._ai_active:
            ai_choice = self._ask_claude(candles, market)
            if ai_choice:
                self._cache[market] = (ai_choice, time.time())
                log.info(f"[AI] Claude selected: {ai_choice} for {market}")
                return ai_choice

        # Fallback: rule-based selection
        return self._rule_based(candles, market)

    def _ask_claude(self, candles: list, market: str) -> str:
        """Ask Claude which strategy to use based on market conditions."""
        try:
            # Prepare compact candle summary for Claude
            last20 = candles[-20:]
            closes = [round(c["close"], 4) for c in last20]
            highs  = [round(c["high"],  4) for c in last20]
            lows   = [round(c["low"],   4) for c in last20]

            # Calculate quick indicators to give Claude context
            import numpy as np
            arr = np.array(closes)
            rsi = _quick_rsi(arr)
            trend = "UP" if arr[-1] > arr[-5] else "DOWN" if arr[-1] < arr[-5] else "FLAT"
            vol   = round(float(np.std(arr[-10:]) / np.mean(arr[-10:]) * 100), 3)
            bb_pos = _quick_bb_position(arr)

            # Performance data
            rates = self.tracker.get_win_rates()
            perf_summary = ", ".join([
                f"{s}: {v['win_rate']}% ({v['total']} trades)"
                for s, v in rates.items()
                if v['total'] > 0
            ]) or "No data yet"

            prompt = f"""You are a binary options trading strategy selector for Deriv synthetic indices.

Market: {market}
Current RSI: {rsi:.1f}
Trend (5-candle): {trend}
Volatility: {vol}%
BB Position (0=lower, 1=upper): {bb_pos:.2f}
Last 5 closes: {closes[-5:]}

Strategy performance so far:
{perf_summary}

Available strategies:
1. rsi_reversal — trades when RSI was extreme (>76 or <24) and is now turning back. Best in ranging/choppy markets.
2. bb_bounce — trades when price pierces Bollinger Band then closes back inside. Best when volatility is moderate.
3. ema_triple — trades when EMA 9/21/50 all aligned. Best in strong trending markets.
4. false_breakout — trades when price breaks a recent high/low then reverses. Best in ranging markets.
5. momentum_streak — trades 3 consecutive same-direction candles. Best in trending markets with momentum.

Based on current conditions, which single strategy is MOST LIKELY to win the next trade?

Reply with ONLY the strategy name, nothing else. Choose from:
rsi_reversal, bb_bounce, ema_triple, false_breakout, momentum_streak"""

            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 50,
                    "messages":   [{"role": "user", "content": prompt}]
                },
                timeout=8
            )

            if response.status_code == 200:
                data   = response.json()
                choice = data["content"][0]["text"].strip().lower().replace(" ","_")
                valid  = ["rsi_reversal","bb_bounce","ema_triple","false_breakout","momentum_streak"]
                if choice in valid:
                    # Store recommendation
                    self.tracker.data["ai_recommendations"].append({
                        "market": market,
                        "strategy": choice,
                        "rsi": rsi,
                        "trend": trend,
                        "time": datetime.utcnow().isoformat()
                    })
                    if len(self.tracker.data["ai_recommendations"]) > 100:
                        self.tracker.data["ai_recommendations"] = \
                            self.tracker.data["ai_recommendations"][-100:]
                    self.tracker._save()
                    return choice
            else:
                log.warning(f"[AI] Claude API returned {response.status_code}")
                self._ai_active = False  # disable if API fails

        except Exception as e:
            log.warning(f"[AI] Claude API error: {e}")
            self._ai_active = False

        return None

    def _rule_based(self, candles: list, market: str) -> str:
        """
        Fallback rule-based strategy selection.
        Uses market conditions to pick the most appropriate strategy.
        """
        import numpy as np
        arr     = np.array([c["close"] for c in candles[-20:]])
        rsi     = _quick_rsi(arr)
        bb_pos  = _quick_bb_position(arr)
        vol     = float(np.std(arr[-10:]) / np.mean(arr[-10:]) * 100)
        trending = abs(arr[-1] - arr[-10]) > np.std(arr[-10:]) * 1.5

        # High RSI or low RSI → RSI reversal
        if rsi >= 72 or rsi <= 28:
            return "rsi_reversal"

        # Price near BB extremes → BB bounce
        if bb_pos > 0.85 or bb_pos < 0.15:
            return "bb_bounce"

        # Strong trend → EMA triple
        if trending and 0.3 <= bb_pos <= 0.7:
            return "ema_triple"

        # Low volatility → false breakout more common
        if vol < 0.05:
            return "false_breakout"

        # Default: momentum
        return "momentum_streak"

    def re_enable_ai(self):
        """Re-enable AI after a failure."""
        self._ai_active = True
        log.info("[AI] AI strategy selector re-enabled")


# ─────────────────────────────────────────
# Quick indicator helpers
# ─────────────────────────────────────────
def _quick_rsi(arr, period=14) -> float:
    import numpy as np
    if len(arr) < period + 1:
        return 50.0
    deltas = np.diff(arr)
    gains  = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    ag = np.mean(gains[-period:])
    al = np.mean(losses[-period:])
    if al == 0:
        return 100.0
    rs = ag / al
    return float(100 - 100 / (1 + rs))

def _quick_bb_position(arr, period=20) -> float:
    import numpy as np
    if len(arr) < period:
        return 0.5
    window = arr[-period:]
    mean   = np.mean(window)
    std    = np.std(window)
    if std == 0:
        return 0.5
    upper = mean + 2*std
    lower = mean - 2*std
    pos   = (arr[-1] - lower) / (upper - lower)
    return float(np.clip(pos, -0.2, 1.2))


# ─────────────────────────────────────────
# Global instances (imported by strategy.py)
# ─────────────────────────────────────────
tracker  = StrategyTracker()
selector = AIStrategySelector(tracker)
