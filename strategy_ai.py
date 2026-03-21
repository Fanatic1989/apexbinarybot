"""
Thompson Sampling Strategy Optimizer — v4.0

Clean implementation based on the Multi-Armed Bandit model:
  alphas = wins + 1  (Laplace smoothing)
  betas  = losses + 1

Improvements over standard TS:
  - Market-specific arm weights (R_100 favours mean reversion)
  - Regime-aware selection (trending vs ranging vs choppy)
  - Automatic underperformer reset (fresh start after 5+ trades below 42%)
  - Persistent state survives restarts (saves to disk)
  - Per-market proven history overrides sampling when enough data exists
"""
import json
import time
import logging
import numpy as np
from datetime import datetime

log = logging.getLogger(__name__)

STRATEGIES = [
    "bb_bounce",       # Bollinger Band mean reversion
    "rsi_reversal",    # RSI extreme reversal
    "false_breakout",  # False breakout reversal
    "momentum_streak", # Trend continuation
    "fvg_retest",      # Fair Value Gap retest (commodities)
    "pivot_stochrsi",  # Pivot Point + Stoch RSI (forex)
    "ema_triple",      # Triple EMA alignment
]


# ─────────────────────────────────────────
# Core Thompson Sampling Engine
# ─────────────────────────────────────────
class ThompsonSamplingOptimizer:
    """
    Pure MAB implementation.
    Sample Beta(alpha, beta) for each arm → pick highest.
    """
    def __init__(self, strategies: list):
        self.strategies = strategies
        self.n          = len(strategies)
        self.alphas     = np.ones(self.n, dtype=float)
        self.betas      = np.ones(self.n, dtype=float)

    def select(self, excluded: list = None, weights: dict = None) -> int:
        """
        Sample from each Beta distribution.
        excluded : strategies to skip (zero sample)
        weights  : multipliers per strategy name {name: float}
        """
        samples = np.array([
            float(np.random.beta(self.alphas[i], self.betas[i]))
            for i in range(self.n)
        ])

        # Apply market-specific weights
        if weights:
            for i, name in enumerate(self.strategies):
                if name in weights:
                    samples[i] *= weights[name]

        # Zero out excluded strategies
        if excluded:
            for i, name in enumerate(self.strategies):
                if name in excluded:
                    samples[i] = 0.0

        # If everything excluded, return random
        if samples.max() == 0:
            return int(np.random.randint(self.n))

        return int(np.argmax(samples))

    def update(self, idx: int, won: bool):
        if won: self.alphas[idx] += 1.0
        else:   self.betas[idx]  += 1.0

    def get_win_rate(self, idx: int) -> float:
        """Estimated win rate (excluding Laplace smoothing)."""
        total = self.alphas[idx] + self.betas[idx] - 2
        return round((self.alphas[idx]-1) / total * 100, 1) if total > 0 else None

    def get_stats(self) -> dict:
        out = {}
        for i, name in enumerate(self.strategies):
            total = int(self.alphas[i] + self.betas[i] - 2)
            wins  = int(self.alphas[i] - 1)
            losses= int(self.betas[i] - 1)
            out[name] = {
                "wins":     wins,
                "losses":   losses,
                "total":    total,
                "win_rate": round(wins/total*100,1) if total>0 else None,
                "alpha":    round(float(self.alphas[i]),2),
                "beta":     round(float(self.betas[i]),2),
            }
        return out

    def reset_underperformers(self, min_trades=5, min_wr=0.42):
        """Give consistently losing strategies a fresh start."""
        for i, name in enumerate(self.strategies):
            total = self.alphas[i] + self.betas[i] - 2
            if total >= min_trades:
                wr = (self.alphas[i]-1) / total
                if wr < min_wr:
                    log.warning(f"[TS] {name} WR={wr:.0%} after {total:.0f} trades"
                                f" — resetting for re-exploration")
                    self.alphas[i] = 1.0
                    self.betas[i]  = 1.0


# ─────────────────────────────────────────
# Strategy Tracker (persistence layer)
# ─────────────────────────────────────────
class StrategyTracker:
    FILE = "strategy_performance.json"

    def __init__(self):
        self.data      = self._load()
        self.optimizer = ThompsonSamplingOptimizer(STRATEGIES)
        self._sync()

    def _load(self) -> dict:
        try:
            with open(self.FILE) as f:
                return json.load(f)
        except:
            return {"strategies":{s:{"wins":0,"losses":0} for s in STRATEGIES},
                    "markets":{}, "last_updated":None}

    def _save(self):
        try:
            self.data["last_updated"] = datetime.utcnow().isoformat()
            with open(self.FILE,"w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            log.error(f"[TRACKER] Save error: {e}")

    def _sync(self):
        """Restore optimizer state from persisted data."""
        for i, name in enumerate(STRATEGIES):
            s = self.data["strategies"].get(name,{"wins":0,"losses":0})
            self.optimizer.alphas[i] = float(s["wins"]  + 1)
            self.optimizer.betas[i]  = float(s["losses"] + 1)

    def record(self, strategy: str, market: str, result: str):
        """Record trade result — updates tracker + optimizer."""
        won = (result == "won")

        # Global strategy stats
        s = self.data["strategies"].setdefault(strategy, {"wins":0,"losses":0})
        if won: s["wins"]   += 1
        else:   s["losses"] += 1

        # Per-market stats
        m  = self.data["markets"].setdefault(market, {})
        ms = m.setdefault(strategy, {"wins":0,"losses":0})
        if won: ms["wins"]   += 1
        else:   ms["losses"] += 1

        # Update optimizer
        if strategy in STRATEGIES:
            idx = STRATEGIES.index(strategy)
            self.optimizer.update(idx, won)
            wr = self.optimizer.get_win_rate(idx)
            log.info(f"[TS] {strategy} on {market}: {result.upper()} | "
                     f"WR={wr}% | "
                     f"α={self.optimizer.alphas[idx]:.0f} "
                     f"β={self.optimizer.betas[idx]:.0f}")

            # Auto-reset underperformers every 10 trades
            total_global = sum(v["wins"]+v["losses"]
                               for v in self.data["strategies"].values())
            if total_global % 10 == 0:
                self.optimizer.reset_underperformers()

        self._save()

    def record_volatility_spike(self, market: str):
        """ATR spike detected — logged for future use."""
        log.debug(f"[TS] {market} volatility spike noted")

    def get_market_best(self, market: str) -> str:
        """Best proven strategy for this market (needs 3+ trades, 55%+ WR)."""
        m = self.data["markets"].get(market, {})
        best_wr, best = 0.55, None   # minimum 55% to qualify as "proven"
        for strat, stats in m.items():
            total = stats["wins"] + stats["losses"]
            if total < 3:
                continue
            wr = stats["wins"] / total
            if wr > best_wr:
                best_wr, best = wr, strat
        return best

    def get_excluded(self) -> list:
        """Strategies proven to underperform (below 42% after 5+ trades)."""
        out = []
        for name, stats in self.data["strategies"].items():
            total = stats["wins"] + stats["losses"]
            if total >= 5 and stats["wins"]/total < 0.42:
                out.append(name)
        return out

    def get_win_rates(self) -> dict:
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

    def get_summary(self) -> dict:
        return {
            "win_rates":    self.get_win_rates(),
            "markets":      self.data["markets"],
            "last_updated": self.data.get("last_updated"),
            "ai_type":      "thompson-sampling-mab-v4",
            "optimizer":    self.optimizer.get_stats(),
        }


# ─────────────────────────────────────────
# AI Strategy Selector
# ─────────────────────────────────────────
class AIStrategySelector:
    """
    3-layer strategy selection:
    1. Proven market history    — use what's worked here before
    2. Thompson Sampling        — MAB optimisation
    3. Condition override       — regime + price conditions
    """
    CACHE_TTL = 120

    # Market-specific weights for Thompson Sampling
    # These bias the sampling WITHOUT overriding it
    MARKET_WEIGHTS = {
        # Fast indices: favour mean reversion
        "R_100":   {"bb_bounce":1.5, "rsi_reversal":1.4, "ema_triple":0.4},
        "1HZ100V": {"bb_bounce":1.5, "rsi_reversal":1.4, "ema_triple":0.4},
        "JD100":   {"bb_bounce":1.4, "rsi_reversal":1.3},
        # Forex: favour regime strategies
        "frxEURUSD": {"pivot_stochrsi":1.6, "fvg_retest":1.4},
        "frxGBPUSD": {"pivot_stochrsi":1.6, "fvg_retest":1.4},
        "frxXAUUSD": {"fvg_retest":1.8, "pivot_stochrsi":1.2},
    }

    def __init__(self, tracker: StrategyTracker):
        self.tracker    = tracker
        self._cache     = {}
        self._ai_active = True

    def select_strategy(self, candles: list, market: str) -> str:
        # Layer 1: proven history for this specific market
        proven = self.tracker.get_market_best(market)
        if proven:
            log.debug(f"[AI] {market} → proven best: {proven}")
            return proven

        excluded = self.tracker.get_excluded()
        weights  = self.MARKET_WEIGHTS.get(market)

        # Layer 2: Thompson Sampling with market-specific weights
        ts_idx    = self.tracker.optimizer.select(
                        excluded=excluded, weights=weights)
        ts_choice = STRATEGIES[ts_idx]

        # Layer 3: condition-based override
        cond_choice = self._condition_based(candles, market)

        if ts_choice == cond_choice:
            log.info(f"[AI] {market} → consensus: {ts_choice}")
            return ts_choice

        # Weight condition-based higher early on (< 20 global trades)
        total = sum(v["wins"]+v["losses"]
                    for v in self.tracker.data["strategies"].values())
        choice = cond_choice if total < 20 else ts_choice
        log.info(f"[AI] {market} → {choice} "
                 f"(ts={ts_choice}, cond={cond_choice}, trades={total})")
        return choice

    def _condition_based(self, candles: list, market: str) -> str:
        """Route based on asset class + current market conditions."""
        if not candles or len(candles) < 20:
            return "bb_bounce"
        try:
            import config as _c
            if _c.is_commodity(market): return "fvg_retest"
            if _c.is_forex(market):     return "pivot_stochrsi"

            # Synthetics — analyse conditions
            arr    = np.array([c["close"] for c in candles[-20:]])
            rsi    = _quick_rsi(arr)
            bb_pos = _quick_bb_pos(arr)

            # RSI extreme → mean reversion
            if rsi >= 72 or rsi <= 28: return "rsi_reversal"

            # Price near BB band → bounce
            if bb_pos > 0.88 or bb_pos < 0.12: return "bb_bounce"

            # Recent breakout of swing high/low
            highs = np.array([c["high"] for c in candles[-12:]])
            lows  = np.array([c["low"]  for c in candles[-12:]])
            if highs[-1] > highs[:-1].max() or lows[-1] < lows[:-1].min():
                return "false_breakout"

            # 3 same-direction candles
            last3 = candles[-3:]
            if all(c["close"] > c["open"] for c in last3): return "momentum_streak"
            if all(c["close"] < c["open"] for c in last3): return "momentum_streak"

            return "bb_bounce"
        except Exception as e:
            log.debug(f"[AI] Condition error: {e}")
            return "bb_bounce"

    def re_enable_ai(self):
        self._cache.clear()
        self._ai_active = True


# ─────────────────────────────────────────
# Quick indicators (no pandas dependency)
# ─────────────────────────────────────────
def _quick_rsi(arr: np.ndarray, period: int = 14) -> float:
    if len(arr) < period + 1:
        return 50.0
    d = np.diff(arr)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = np.mean(g[-period:])
    al = np.mean(l[-period:])
    return 100.0 if al == 0 else float(100 - 100 / (1 + ag/al))

def _quick_bb_pos(arr: np.ndarray, period: int = 20) -> float:
    if len(arr) < period:
        return 0.5
    w    = arr[-period:]
    mean = np.mean(w)
    std  = np.std(w)
    if std == 0:
        return 0.5
    pos = (arr[-1] - (mean - 2*std)) / (4*std)
    return float(np.clip(pos, -0.2, 1.2))


# ─────────────────────────────────────────
# Global instances
# ─────────────────────────────────────────
tracker  = StrategyTracker()
selector = AIStrategySelector(tracker)
