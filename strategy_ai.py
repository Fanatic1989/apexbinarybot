"""
Self-Learning AI Strategy Selector — NO external API required.

Uses a weighted scoring system that learns from trade outcomes.
Tracks performance per strategy per market and automatically
routes each market to its best performing strategy.

The more trades it sees, the smarter it gets.
"""
import json
import time
import logging
import math
from datetime import datetime

log = logging.getLogger(__name__)


class StrategyTracker:
    FILE = "strategy_performance.json"
    STRATEGIES = [
        "rsi_reversal",
        "bb_bounce",
        "ema_triple",
        "false_breakout",
        "momentum_streak",
    ]

    def __init__(self):
        self.data = self._load()

    def _load(self):
        try:
            with open(self.FILE) as f:
                return json.load(f)
        except:
            return self._empty()

    def _empty(self):
        return {
            "strategies": {s: {"wins":0,"losses":0,"last_used":None}
                          for s in self.STRATEGIES},
            "markets": {},
            "last_updated": None
        }

    def _save(self):
        try:
            self.data["last_updated"] = datetime.utcnow().isoformat()
            with open(self.FILE,"w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            log.error(f"[TRACKER] Save failed: {e}")

    def record(self, strategy, market, result):
        s = self.data["strategies"].setdefault(
            strategy, {"wins":0,"losses":0,"last_used":None})
        if result == "won": s["wins"] += 1
        else:               s["losses"] += 1
        s["last_used"] = datetime.utcnow().isoformat()

        m  = self.data["markets"].setdefault(market, {})
        ms = m.setdefault(strategy, {"wins":0,"losses":0})
        if result == "won": ms["wins"] += 1
        else:               ms["losses"] += 1

        self._save()
        total = s["wins"] + s["losses"]
        wr    = round(s["wins"]/total*100,1) if total else 0
        log.info(f"[AI] {strategy} on {market}: {result.upper()} | "
                 f"Global win rate: {wr}% ({total} trades)")

    def get_win_rates(self):
        rates = {}
        for name, stats in self.data["strategies"].items():
            total = stats["wins"] + stats["losses"]
            rates[name] = {
                "win_rate": round(stats["wins"]/total*100,1) if total>0 else None,
                "total":    total,
                "wins":     stats["wins"],
                "losses":   stats["losses"],
            }
        return rates

    def get_market_best_strategy(self, market):
        m = self.data["markets"].get(market, {})
        best_wr, best_strat = -1, None
        for strat, stats in m.items():
            total = stats["wins"] + stats["losses"]
            if total < 3: continue
            wr = stats["wins"] / total
            if wr > best_wr:
                best_wr   = wr
                best_strat = strat
        return best_strat

    def get_summary(self):
        return {
            "win_rates":    self.get_win_rates(),
            "markets":      self.data["markets"],
            "last_updated": self.data.get("last_updated"),
            "ai_type":      "self-learning"
        }


class AIStrategySelector:
    """
    Self-learning strategy selector — no external API needed.

    Uses three layers of intelligence:
    1. Historical performance data (which strategy won on this market before)
    2. Market condition analysis (RSI, BB, trend, volatility)
    3. Thompson Sampling (mathematical optimisation that balances
       exploiting known winners vs exploring new strategies)
    """
    CACHE_TTL = 180  # Re-evaluate every 3 minutes

    def __init__(self, tracker: StrategyTracker):
        self.tracker = tracker
        self._cache  = {}

    def select_strategy(self, candles: list, market: str) -> str:
        # Check cache
        if market in self._cache:
            strat, ts = self._cache[market]
            if time.time() - ts < self.CACHE_TTL:
                return strat

        # Layer 1: market-proven strategy (needs 3+ trades)
        proven = self.tracker.get_market_best_strategy(market)
        if proven:
            log.info(f"[AI] {market} → proven best: {proven}")
            self._cache[market] = (proven, time.time())
            return proven

        # Layer 2: Thompson Sampling across global strategy performance
        ts_choice = self._thompson_sampling(market)

        # Layer 3: Condition-based override
        condition_choice = self._condition_based(candles, market)

        # If both agree — high confidence
        if ts_choice == condition_choice:
            choice = ts_choice
            log.info(f"[AI] {market} → consensus: {choice}")
        else:
            # Condition-based takes priority with limited data
            total_trades = sum(
                v["wins"]+v["losses"]
                for v in self.tracker.data["strategies"].values()
            )
            choice = condition_choice if total_trades < 30 else ts_choice
            log.info(f"[AI] {market} → {choice} "
                     f"(ts={ts_choice}, cond={condition_choice})")

        self._cache[market] = (choice, time.time())
        return choice

    def _thompson_sampling(self, market: str = "") -> str:
        """
        Thompson Sampling with performance floor and market-specific weights.

        For fast synthetic indices (R_100, JD100):
          - Boost bb_bounce and rsi_reversal (mean reversion works best)
          - Reduce ema_triple weight (trends are too short-lived)

        For forex/commodities:
          - Boost pivot_stochrsi and fvg_retest
          - Standard sampling for others
        """
        import random
        scores = {}
        is_fast = market in ("R_100", "JD100", "1HZ100V")
        is_forex_comm = market.startswith("frx")

        for name, stats in self.tracker.data["strategies"].items():
            w = stats["wins"] + 1
            l = stats["losses"] + 1
            total = stats["wins"] + stats["losses"]
            wr    = stats["wins"] / total if total > 0 else 0.5

            # Exclude proven losers
            if total >= 4 and wr < 0.42:
                log.debug(f"[AI] {name} excluded — {wr:.0%} WR")
                scores[name] = 0.0
                continue

            score = random.betavariate(w, l)

            # Fast index boost: favour mean reversion
            if is_fast and name in ("bb_bounce", "rsi_reversal"):
                score *= 1.4
            if is_fast and name == "ema_triple":
                score *= 0.4   # penalise on fast indices

            # Forex/commodity boost: favour regime-specific strategies
            if is_forex_comm and name in ("pivot_stochrsi", "fvg_retest"):
                score *= 1.5

            scores[name] = score

        if not scores or max(scores.values()) == 0:
            log.warning("[AI] All strategies underperforming — resetting")
            return random.choice(self.tracker.STRATEGIES)

        return max(scores, key=scores.get)

    def _condition_based(self, candles: list, market: str) -> str:
        """
        Analyse current market conditions to pick the right strategy.
        """
        if not candles or len(candles) < 20:
            return "ema_triple"

        import numpy as np
        arr    = np.array([c["close"] for c in candles[-20:]])
        rsi    = _quick_rsi(arr)
        bb_pos = _quick_bb_position(arr)
        vol    = float(np.std(arr[-10:]) / np.mean(arr[-10:]) * 100)

        # Strong RSI extreme → reversal strategy
        if rsi >= 74 or rsi <= 26:
            return "rsi_reversal"

        # Price near BB bands → band bounce
        if bb_pos > 0.88 or bb_pos < 0.12:
            return "bb_bounce"

        # Check for false breakout setup
        highs = np.array([c["high"]  for c in candles[-12:]])
        lows  = np.array([c["low"]   for c in candles[-12:]])
        recent_high = highs[:-1].max()
        recent_low  = lows[:-1].min()
        last_high   = highs[-1]
        last_low    = lows[-1]
        if last_high > recent_high or last_low < recent_low:
            return "false_breakout"

        # Trending market → EMA triple
        ema9  = float(np.mean(arr[-9:]))
        ema21 = float(np.mean(arr[-21:]) if len(arr)>=21 else np.mean(arr))
        strong_trend = abs(ema9 - ema21) / ema21 > 0.0003
        if strong_trend:
            return "ema_triple"

        # Check momentum streak (3 same-direction candles)
        last3 = candles[-3:]
        all_bull = all(c["close"] > c["open"] for c in last3)
        all_bear = all(c["close"] < c["open"] for c in last3)
        if all_bull or all_bear:
            return "momentum_streak"

        # Default
        return "ema_triple"

    def re_enable_ai(self):
        self._cache.clear()


# ─────────────────────────────────────────
# Quick indicators
# ─────────────────────────────────────────
def _quick_rsi(arr, period=14):
    import numpy as np
    if len(arr) < period+1: return 50.0
    d = np.diff(arr)
    g = np.where(d>0, d, 0)
    l = np.where(d<0, -d, 0)
    ag = np.mean(g[-period:])
    al = np.mean(l[-period:])
    if al == 0: return 100.0
    return float(100 - 100/(1 + ag/al))

def _quick_bb_position(arr, period=20):
    import numpy as np
    if len(arr) < period: return 0.5
    w    = arr[-period:]
    mean = np.mean(w)
    std  = np.std(w)
    if std == 0: return 0.5
    pos = (arr[-1] - (mean-2*std)) / (4*std)
    return float(np.clip(pos, -0.2, 1.2))


# ─────────────────────────────────────────
# Global instances
# ─────────────────────────────────────────
tracker  = StrategyTracker()
selector = AIStrategySelector(tracker)
