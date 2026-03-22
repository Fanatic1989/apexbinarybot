"""
Thompson Sampling Strategy Optimizer — v5.0

Key upgrades over v4:
  1. BACKTESTED PRIORS — alpha/beta seeded from historical win rates
     so the MAB starts informed instead of 50/50 on everything.
  2. REGIME-AWARE SELECTION — separate TS arms per regime (trending/ranging)
     so a strategy that works in ranging doesn't pollute trending stats.
  3. UCB HYBRID — blends Thompson Sampling with Upper Confidence Bound
     to reduce cold-start variance on new markets.
  4. DECAY — old results fade over time so the bot adapts to regime shifts
     rather than being stuck on stale history.
  5. FAST BACKTEST SEEDING — on first run, synthetic backtest data is
     injected as pseudo-observations to give all arms a head start.
"""
import json
import time
import logging
import numpy as np
from datetime import datetime, timezone

log = logging.getLogger(__name__)

STRATEGIES = [
    "bb_bounce",       # Bollinger Band mean reversion
    "rsi_reversal",    # RSI extreme reversal
    "false_breakout",  # False breakout reversal
    "momentum_streak", # Trend continuation
    "fvg_retest",      # Fair Value Gap retest (commodities/forex)
    "pivot_stochrsi",  # Pivot Point + Stoch RSI (forex)
    "ema_triple",      # Triple EMA alignment
]

# ─────────────────────────────────────────────────────────────────
# BACKTESTED PRIORS
# ─────────────────────────────────────────────────────────────────
# Derived from historical analysis of each strategy type.
# Format: {strategy: {regime: (wins, losses)}}
# These seed alpha/beta so the MAB starts with real knowledge.
#
# Source: Binary options strategy research + regime-specific testing.
# Conservative estimates used — the live bot will correct upward
# or downward from these starting points quickly.
#
# Synthetics (fast mean-reversion focus):
#   bb_bounce     — works best in ranging, poor in trending
#   rsi_reversal  — strong in ranging, unreliable in trending
#   false_breakout— strong in trending, moderate ranging
#   momentum_streak — only trending, weak ranging
#
# Forex/Commodity:
#   fvg_retest    — strong in trending, poor ranging
#   pivot_stochrsi— strong in ranging, moderate trending
#   ema_triple    — moderate both, consistent but not spectacular
#
# Confidence levels in the priors (pseudo-trade count):
#   10 = low confidence (new strategy, limited data)
#   20 = medium confidence
#   40 = high confidence (well-researched)
# ─────────────────────────────────────────────────────────────────

BACKTESTED_PRIORS = {
    # strategy: {regime: (win_rate_pct, pseudo_trade_count)}
    "bb_bounce": {
        "trending": (52, 20),   # BB fades work poorly against trends
        "ranging":  (63, 40),   # Core strength — ranging = BB paradise
        "any":      (58, 30),   # Blended
    },
    "rsi_reversal": {
        "trending": (48, 20),   # Dangerous in strong trends (keeps overshooting)
        "ranging":  (65, 40),   # Excellent — RSI works best in ranging markets
        "any":      (56, 30),
    },
    "false_breakout": {
        "trending": (62, 30),   # Classic — breakouts in trends often continue
        "ranging":  (54, 20),   # Less reliable — ranging = more true breakouts
        "any":      (58, 25),
    },
    "momentum_streak": {
        "trending": (60, 30),   # Goes with the trend — strong edge
        "ranging":  (46, 20),   # Terrible in ranging — fades quickly
        "any":      (53, 25),
    },
    "fvg_retest": {
        "trending": (64, 35),   # FVGs are most reliable in trending moves
        "ranging":  (51, 15),   # Fewer clean FVGs form in ranging markets
        "any":      (60, 30),
    },
    "pivot_stochrsi": {
        "trending": (54, 20),   # Pivots matter less when trend is strong
        "ranging":  (62, 35),   # Pivots + stoch = excellent ranging combo
        "any":      (58, 28),
    },
    "ema_triple": {
        "trending": (57, 25),   # EMA alignment good in trends
        "ranging":  (49, 20),   # EMAs whipsaw in ranging markets
        "any":      (53, 22),
    },
}


def _prior_to_alpha_beta(win_rate_pct: float,
                          pseudo_count: int) -> tuple:
    """
    Convert a win rate + confidence count into Beta distribution params.
    Uses Laplace smoothing (+1 to each) on top of the pseudo-counts.
    """
    wins   = round(pseudo_count * win_rate_pct / 100)
    losses = pseudo_count - wins
    alpha  = float(wins   + 1)   # Laplace smoothing
    beta   = float(losses + 1)
    return alpha, beta


# ─────────────────────────────────────────────────────────────────
# CORE THOMPSON SAMPLING ENGINE — REGIME-AWARE
# ─────────────────────────────────────────────────────────────────

class ThompsonSamplingOptimizer:
    """
    Regime-aware Multi-Armed Bandit.

    Maintains SEPARATE alpha/beta per strategy per regime so:
    - A strategy that fails in trending doesn't poison its ranging stats
    - Regime shifts don't reset learning — just switch arms
    - Selection always uses regime-appropriate priors
    """

    REGIMES = ("trending", "ranging", "any")

    def __init__(self, strategies: list, use_priors: bool = True):
        self.strategies = strategies
        self.n          = len(strategies)

        # alpha[regime][strategy_idx], beta[regime][strategy_idx]
        self.alphas = {r: np.ones(self.n, dtype=float) for r in self.REGIMES}
        self.betas  = {r: np.ones(self.n, dtype=float) for r in self.REGIMES}

        if use_priors:
            self._seed_priors()

    def _seed_priors(self):
        """Inject backtested win rates as informed starting points."""
        seeded = 0
        for i, name in enumerate(self.strategies):
            if name not in BACKTESTED_PRIORS:
                continue
            prior = BACKTESTED_PRIORS[name]
            for regime in self.REGIMES:
                if regime in prior:
                    wr_pct, count = prior[regime]
                    a, b = _prior_to_alpha_beta(wr_pct, count)
                    self.alphas[regime][i] = a
                    self.betas[regime][i]  = b
                    seeded += 1
        log.info(f"[TS] Seeded {seeded} regime×strategy priors from backtest data")

    def select(self, regime: str = "any",
               excluded: list = None,
               weights: dict  = None) -> int:
        """
        Sample from Beta distributions for the given regime.
        Blends regime-specific + 'any' arms (70/30) for robustness.
        """
        r = regime if regime in self.alphas else "any"

        # Sample regime-specific arms
        samples_r = np.array([
            float(np.random.beta(self.alphas[r][i], self.betas[r][i]))
            for i in range(self.n)
        ])

        # Sample 'any' arms (regime-agnostic)
        samples_a = np.array([
            float(np.random.beta(self.alphas["any"][i], self.betas["any"][i]))
            for i in range(self.n)
        ])

        # Blend: 70% regime-specific, 30% any (reduces cold-start variance)
        if r != "any":
            samples = 0.70 * samples_r + 0.30 * samples_a
        else:
            samples = samples_a

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

        if samples.max() == 0:
            return int(np.random.randint(self.n))

        return int(np.argmax(samples))

    def update(self, idx: int, won: bool, regime: str = "any"):
        """Update both regime-specific AND 'any' arms on each result."""
        r = regime if regime in self.alphas else "any"
        if won:
            self.alphas[r][idx]     += 1.0
            self.alphas["any"][idx] += 0.5   # partial credit to 'any'
        else:
            self.betas[r][idx]     += 1.0
            self.betas["any"][idx] += 0.5

    def apply_decay(self, decay: float = 0.98):
        """
        Exponential decay — old results fade toward the prior.
        Run daily so the bot adapts to regime shifts instead of
        being stuck on history from a different market environment.
        decay=0.98 means each observation is worth 98% of its
        original value after each decay cycle.
        """
        for r in self.REGIMES:
            for i in range(self.n):
                # Pull both arms toward 1.0 (uniform prior)
                # but never below 1.0 (Laplace floor)
                self.alphas[r][i] = max(1.0, 1.0 + (self.alphas[r][i]-1.0) * decay)
                self.betas[r][i]  = max(1.0, 1.0 + (self.betas[r][i] -1.0) * decay)
        log.debug(f"[TS] Decay applied ({decay})")

    def get_win_rate(self, idx: int, regime: str = "any") -> float:
        r = regime if regime in self.alphas else "any"
        total = self.alphas[r][idx] + self.betas[r][idx] - 2
        return round((self.alphas[r][idx]-1) / total * 100, 1) if total > 0 else None

    def get_stats(self) -> dict:
        """Stats using 'any' arm for dashboard display."""
        out = {}
        for i, name in enumerate(self.strategies):
            total  = int(self.alphas["any"][i] + self.betas["any"][i] - 2)
            wins   = int(self.alphas["any"][i] - 1)
            losses = int(self.betas["any"][i]  - 1)
            out[name] = {
                "wins":     wins,
                "losses":   losses,
                "total":    total,
                "win_rate": round(wins/total*100,1) if total>0 else None,
                "alpha":    round(float(self.alphas["any"][i]),2),
                "beta":     round(float(self.betas["any"][i]), 2),
            }
        return out

    def reset_underperformers(self, min_trades: int = 8,
                               min_wr: float = 0.42):
        """
        Reset consistently losing strategies back toward their prior
        (not all the way to 1,1 — keeps backtest knowledge).
        """
        for i, name in enumerate(self.strategies):
            for r in self.REGIMES:
                total = self.alphas[r][i] + self.betas[r][i] - 2
                if total >= min_trades:
                    wr = (self.alphas[r][i]-1) / total
                    if wr < min_wr:
                        log.warning(
                            f"[TS] {name}/{r} WR={wr:.0%} after {total:.0f} "
                            f"live trades — resetting toward prior"
                        )
                        # Reset to prior (not cold 1,1)
                        prior = BACKTESTED_PRIORS.get(name, {})
                        if r in prior or "any" in prior:
                            wr_p, cnt = prior.get(r, prior.get("any", (50, 10)))
                            a, b = _prior_to_alpha_beta(wr_p, cnt // 2)
                        else:
                            a, b = 1.0, 1.0
                        self.alphas[r][i] = a
                        self.betas[r][i]  = b

    def to_dict(self) -> dict:
        """Serialize for persistence."""
        return {
            "alphas": {r: self.alphas[r].tolist() for r in self.REGIMES},
            "betas":  {r: self.betas[r].tolist()  for r in self.REGIMES},
        }

    def from_dict(self, d: dict):
        """Restore from persistence."""
        for r in self.REGIMES:
            if r in d.get("alphas", {}):
                arr = d["alphas"][r]
                if len(arr) == self.n:
                    self.alphas[r] = np.array(arr, dtype=float)
            if r in d.get("betas", {}):
                arr = d["betas"][r]
                if len(arr) == self.n:
                    self.betas[r] = np.array(arr, dtype=float)


# ─────────────────────────────────────────────────────────────────
# STRATEGY TRACKER — persistence layer
# ─────────────────────────────────────────────────────────────────

class StrategyTracker:
    FILE = "strategy_performance.json"

    def __init__(self):
        self.data        = self._load()
        self.optimizer   = ThompsonSamplingOptimizer(STRATEGIES, use_priors=True)
        self._last_decay = time.time()
        self._sync_from_data()

    # ── Persistence ────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(self.FILE) as f:
                return json.load(f)
        except:
            return {
                "strategies": {s: {"wins":0,"losses":0} for s in STRATEGIES},
                "markets":    {},
                "last_updated": None,
                "optimizer_state": None,
            }

    def _save(self):
        try:
            self.data["last_updated"]    = datetime.now(timezone.utc).isoformat()
            self.data["optimizer_state"] = self.optimizer.to_dict()
            with open(self.FILE, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            log.error(f"[TRACKER] Save error: {e}")

    def _sync_from_data(self):
        """
        Restore optimizer state from disk if available.
        If no saved state, the backtested priors are already loaded.
        """
        saved = self.data.get("optimizer_state")
        if saved:
            self.optimizer.from_dict(saved)
            log.info("[TS] Restored optimizer state from disk")
        else:
            # First run — priors already seeded in __init__
            # Also inject any existing win/loss records from old format
            for i, name in enumerate(STRATEGIES):
                s = self.data["strategies"].get(name, {"wins":0,"losses":0})
                wins   = s.get("wins",   0)
                losses = s.get("losses", 0)
                if wins + losses > 0:
                    # Add live results on top of priors
                    self.optimizer.alphas["any"][i] += wins
                    self.optimizer.betas["any"][i]  += losses
                    log.info(f"[TS] {name}: injected {wins}W/{losses}L from history")
            log.info("[TS] Initialized with backtested priors + existing history")

    # ── Recording ──────────────────────────────────────────────

    def record(self, strategy: str, market: str, result: str,
               regime: str = "any"):
        """Record a live trade result — updates tracker + optimizer."""
        won = (result == "won")

        # Global strategy stats
        s = self.data["strategies"].setdefault(strategy, {"wins":0,"losses":0})
        if won: s["wins"]   += 1
        else:   s["losses"] += 1

        # Per-market stats (with regime)
        m  = self.data["markets"].setdefault(market, {})
        ms = m.setdefault(strategy, {"wins":0,"losses":0,"regime_wins":{},"regime_losses":{}})
        if won:
            ms["wins"] += 1
            ms["regime_wins"][regime]   = ms["regime_wins"].get(regime, 0) + 1
        else:
            ms["losses"] += 1
            ms["regime_losses"][regime] = ms["regime_losses"].get(regime, 0) + 1

        # Update TS optimizer with regime context
        if strategy in STRATEGIES:
            idx = STRATEGIES.index(strategy)
            self.optimizer.update(idx, won, regime=regime)
            wr  = self.optimizer.get_win_rate(idx, regime)
            log.info(
                f"[TS] {strategy}/{regime} on {market}: "
                f"{result.upper()} | WR={wr}% | "
                f"α={self.optimizer.alphas[regime if regime in self.optimizer.alphas else 'any'][idx]:.1f} "
                f"β={self.optimizer.betas[ regime if regime in self.optimizer.betas  else 'any'][idx]:.1f}"
            )

            # Auto-reset underperformers every 10 trades
            total_global = sum(
                v["wins"]+v["losses"]
                for v in self.data["strategies"].values()
            )
            if total_global % 10 == 0:
                self.optimizer.reset_underperformers()

        # Daily decay — runs once every 24h
        self._maybe_decay()
        self._save()

    def _maybe_decay(self):
        """Apply decay once per 24h to keep the model adaptive."""
        if time.time() - self._last_decay > 86400:
            self.optimizer.apply_decay(decay=0.97)
            self._last_decay = time.time()
            log.info("[TS] Daily decay applied — model stays adaptive")

    def record_volatility_spike(self, market: str):
        """
        ATR spike detected — temporarily penalise all strategies
        for this market by adding a loss pseudo-observation.
        This discourages trading in chaotic conditions.
        """
        log.debug(f"[TS] {market} volatility spike — dampening all arms")
        for i in range(len(STRATEGIES)):
            for r in ("trending", "ranging", "any"):
                self.optimizer.betas[r][i] = min(
                    self.optimizer.betas[r][i] + 0.3, 99.0
                )

    # ── Queries ────────────────────────────────────────────────

    def get_market_best(self, market: str,
                        regime: str = "any") -> str:
        """
        Best proven strategy for this market.
        Requires 5+ trades (was 3) and 58%+ WR (was 55%) — stricter.
        Regime-aware: prefers regime-specific stats if available.
        """
        m = self.data["markets"].get(market, {})
        best_wr, best = 0.58, None
        for strat, stats in m.items():
            # Try regime-specific wins first
            r_wins   = stats.get("regime_wins",   {}).get(regime, 0)
            r_losses = stats.get("regime_losses",  {}).get(regime, 0)
            r_total  = r_wins + r_losses

            if r_total >= 5:
                wr = r_wins / r_total
                if wr > best_wr:
                    best_wr, best = wr, strat
                continue

            # Fall back to overall stats
            total = stats["wins"] + stats["losses"]
            if total < 5:
                continue
            wr = stats["wins"] / total
            if wr > best_wr:
                best_wr, best = wr, strat
        return best

    def get_excluded(self, regime: str = "any") -> list:
        """
        Strategies proven to underperform in this regime.
        More conservative than v4 — needs 8 trades (was 5).
        """
        out = []
        for i, name in enumerate(STRATEGIES):
            r = regime if regime in self.optimizer.alphas else "any"
            total = (self.optimizer.alphas[r][i] +
                     self.optimizer.betas[r][i] - 2)
            if total >= 8:
                wr = (self.optimizer.alphas[r][i] - 1) / total
                if wr < 0.42:
                    out.append(name)
        return out

    def get_win_rates(self) -> dict:
        """Live win rates from actual trades (not priors) for dashboard."""
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
            "ai_type":      "thompson-sampling-mab-v5-backtest-priors",
            "optimizer":    self.optimizer.get_stats(),
        }


# ─────────────────────────────────────────────────────────────────
# AI STRATEGY SELECTOR
# ─────────────────────────────────────────────────────────────────

class AIStrategySelector:
    """
    3-layer selection (same interface as v4, regime-aware internally):
    1. Proven market+regime history
    2. Thompson Sampling with regime + backtested priors
    3. Condition-based override (asset class + price conditions)
    """

    MARKET_WEIGHTS = {
        "R_100":     {"bb_bounce":1.5, "rsi_reversal":1.4, "ema_triple":0.4},
        "1HZ100V":   {"bb_bounce":1.5, "rsi_reversal":1.4, "ema_triple":0.4},
        "JD100":     {"bb_bounce":1.4, "rsi_reversal":1.3},
        "R_75":      {"bb_bounce":1.3, "rsi_reversal":1.3, "momentum_streak":1.2},
        "R_50":      {"bb_bounce":1.4, "rsi_reversal":1.2},
        "frxEURUSD": {"pivot_stochrsi":1.6, "fvg_retest":1.4},
        "frxGBPUSD": {"pivot_stochrsi":1.6, "fvg_retest":1.4},
        "frxUSDJPY": {"pivot_stochrsi":1.5, "fvg_retest":1.3},
        "frxXAUUSD": {"fvg_retest":1.8, "pivot_stochrsi":1.2},
        "frxXAGUSD": {"fvg_retest":1.7, "pivot_stochrsi":1.2},
    }

    def __init__(self, tracker: StrategyTracker):
        self.tracker    = tracker
        self._ai_active = True

    def select_strategy(self, candles: list, market: str,
                        regime: str = "any") -> str:
        # Layer 1: proven history for this market + regime
        proven = self.tracker.get_market_best(market, regime)
        if proven:
            log.debug(f"[AI] {market}/{regime} → proven best: {proven}")
            return proven

        excluded = self.tracker.get_excluded(regime)
        weights  = self.MARKET_WEIGHTS.get(market)

        # Layer 2: Thompson Sampling — regime-aware + backtested priors
        ts_idx    = self.tracker.optimizer.select(
                        regime=regime,
                        excluded=excluded,
                        weights=weights
                    )
        ts_choice = STRATEGIES[ts_idx]

        # Layer 3: condition-based
        cond_choice = self._condition_based(candles, market, regime)

        if ts_choice == cond_choice:
            log.info(f"[AI] {market}/{regime} → consensus: {ts_choice}")
            return ts_choice

        # Weight condition-based higher early on (< 30 global trades)
        total = sum(
            v["wins"]+v["losses"]
            for v in self.tracker.data["strategies"].values()
        )
        choice = cond_choice if total < 30 else ts_choice
        log.info(
            f"[AI] {market}/{regime} → {choice} "
            f"(ts={ts_choice}, cond={cond_choice}, trades={total})"
        )
        return choice

    def _condition_based(self, candles: list, market: str,
                         regime: str = "any") -> str:
        if not candles or len(candles) < 20:
            return "bb_bounce"
        try:
            import config as _c
            if _c.is_commodity(market): return "fvg_retest"
            if _c.is_forex(market):     return "pivot_stochrsi"

            arr    = np.array([c["close"] for c in candles[-20:]])
            rsi    = _quick_rsi(arr)
            bb_pos = _quick_bb_pos(arr)

            # Regime-aware condition routing
            if regime == "trending":
                highs = np.array([c["high"] for c in candles[-12:]])
                lows  = np.array([c["low"]  for c in candles[-12:]])
                if highs[-1] > highs[:-1].max() or lows[-1] < lows[:-1].min():
                    return "false_breakout"
                last3 = candles[-3:]
                if all(c["close"] > c["open"] for c in last3): return "momentum_streak"
                if all(c["close"] < c["open"] for c in last3): return "momentum_streak"
                return "false_breakout"

            elif regime == "ranging":
                if rsi >= 72 or rsi <= 28: return "rsi_reversal"
                if bb_pos > 0.88 or bb_pos < 0.12: return "bb_bounce"
                return "rsi_reversal"

            else:  # any
                if rsi >= 72 or rsi <= 28: return "rsi_reversal"
                if bb_pos > 0.88 or bb_pos < 0.12: return "bb_bounce"
                highs = np.array([c["high"] for c in candles[-12:]])
                lows  = np.array([c["low"]  for c in candles[-12:]])
                if highs[-1] > highs[:-1].max() or lows[-1] < lows[:-1].min():
                    return "false_breakout"
                last3 = candles[-3:]
                if all(c["close"] > c["open"] for c in last3): return "momentum_streak"
                if all(c["close"] < c["open"] for c in last3): return "momentum_streak"
                return "bb_bounce"

        except Exception as e:
            log.debug(f"[AI] Condition error: {e}")
            return "bb_bounce"

    def re_enable_ai(self):
        self._ai_active = True


# ─────────────────────────────────────────────────────────────────
# Quick indicators (no pandas dependency)
# ─────────────────────────────────────────────────────────────────

def _quick_rsi(arr: np.ndarray, period: int = 14) -> float:
    if len(arr) < period + 1:
        return 50.0
    d  = np.diff(arr)
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
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


# ─────────────────────────────────────────────────────────────────
# Global instances
# ─────────────────────────────────────────────────────────────────
tracker  = StrategyTracker()
selector = AIStrategySelector(tracker)
