"""
Apex Scalping Bot v2.0 — Deriv Multipliers
Forex & Gold scalping with ATR-based SL/TP + Trailing Stop
"""
import threading
import logging
import time
import json
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from scalp_strategy import analyze_market
from news_filter import news_filter
from risk_state import save_state, load_state, restore_risk_manager

log = logging.getLogger(__name__)

# ── Markets for scalping ──────────────────────────────────────
SCALP_MARKETS = [
    "frxEURUSD",
    "frxGBPUSD",
    "frxUSDJPY",
    "frxAUDUSD",
    "frxUSDCHF",
    "frxXAUUSD",  # Gold
]

# ── Risk settings ─────────────────────────────────────────────
MAX_OPEN_POSITIONS = 3     # Max concurrent trades
SCAN_INTERVAL      = 30    # Seconds between scans (faster than binary)
MAX_DAILY_LOSS_PCT = 5.0   # Stop trading if down 5%
DAILY_PROFIT_PCT   = 8.0   # Stop trading when up 8%

# ── Trailing stop settings ───────────────────────────────────
TRAIL_ACTIVATE_PCT   = 1.0   # Activate after profit > stake × this value
TRAIL_DISTANCE_PCT   = 1.0   # Close if profit falls from peak by stake × this value

# ── Globals ───────────────────────────────────────────────────
_bot_running    = False
_bot_thread     = None
_open_positions = {}   # contract_id -> position info (includes trailing data)
_trade_history  = []
_risk           = {
    "balance":          0.0,
    "starting_balance": 0.0,
    "daily_pnl":        0.0,
    "total_trades":     0,
    "wins":             0,
    "losses":           0,
    "open":             0,
}
_lock = threading.Lock()
HISTORY_FILE      = "scalp_trades.json"
COMPOUND_FILE     = "compound_settings.json"

# ── Dashboard helpers ────────────────────────────────────────
last_signals = []           # store recent signals (max 10)
strategy_stats = {}         # strategy -> {"wins": int, "losses": int}
_strategy_stats_lock = threading.Lock()

# ── Compounding settings ──────────────────────────────────────
_compound_enabled = False
_compound_pct     = 50      # % of profit to reinvest
_compound_lock    = threading.Lock()


def set_compound(enabled: bool, pct: int):
    global _compound_enabled, _compound_pct
    with _compound_lock:
        _compound_enabled = enabled
        _compound_pct     = max(10, min(100, pct))
    try:
        with open(COMPOUND_FILE, "w") as f:
            import json as _j
            _j.dump({"enabled": enabled, "pct": _compound_pct}, f)
    except: pass
    log.info(f"[COMPOUND] {'ON' if enabled else 'OFF'} | {_compound_pct}% reinvest")


def load_compound_settings():
    global _compound_enabled, _compound_pct
    try:
        with open(COMPOUND_FILE) as f:
            import json as _j
            d = _j.load(f)
        _compound_enabled = d.get("enabled", False)
        _compound_pct     = d.get("pct", 50)
        log.info(f"[COMPOUND] Loaded: {'ON' if _compound_enabled else 'OFF'} {_compound_pct}%")
    except:
        pass


def get_compound_settings() -> dict:
    return {"compound_enabled": _compound_enabled, "compound_pct": _compound_pct}


# ─────────────────────────────────────────
# RISK MANAGER (simple inline for scalping)
# ─────────────────────────────────────────
class ScalpRiskManager:
    def __init__(self, balance: float):
        self.balance          = balance
        self.starting_balance = balance
        self.daily_pnl        = 0.0
        self.total_trades     = 0
        self.wins             = 0
        self.losses           = 0
        self.consecutive_loss = 0

    def stake_for_trade(self) -> float:
        """1% of current balance, min $1"""
        pct = getattr(config, "STAKE_PERCENT", 1.0)
        return max(round(self.balance * (pct / 100), 2), 1.0)

    def record_win(self, profit: float):
        self.balance          += profit
        self.daily_pnl        += profit
        self.total_trades     += 1
        self.wins             += 1
        self.consecutive_loss  = 0

    def record_loss(self, loss: float):
        self.balance          -= loss
        self.daily_pnl        -= loss
        self.total_trades     += 1
        self.losses           += 1
        self.consecutive_loss += 1

    @property
    def win_rate(self) -> float:
        settled = self.wins + self.losses
        return round(self.wins / settled * 100, 1) if settled else 0.0

    @property
    def net_pnl(self) -> float:
        return round(self.balance - self.starting_balance, 2)

    def daily_loss_hit(self) -> bool:
        limit = self.starting_balance * (MAX_DAILY_LOSS_PCT / 100)
        return self.daily_pnl < -limit

    def daily_target_hit(self) -> bool:
        target = self.starting_balance * (DAILY_PROFIT_PCT / 100)
        return self.daily_pnl >= target

    def get_summary(self) -> dict:
        return {
            "balance":       round(self.balance, 2),
            "total_trades":  self.total_trades,
            "wins":          self.wins,
            "losses":        self.losses,
            "win_rate":      self.win_rate,
            "net_pnl":       self.net_pnl,
            "daily_profit":  max(self.daily_pnl, 0),
            "daily_loss":    abs(min(self.daily_pnl, 0)),
            "consec_losses": self.consecutive_loss,
        }


_risk_manager: ScalpRiskManager = None


# ─────────────────────────────────────────
# DERIV MULTIPLIER API CALLS
# ─────────────────────────────────────────
def _get_balance() -> float:
    try:
        from deriv_api import get_balance as _gb
        return _gb()
    except:
        return 0.0


def _get_candles(market: str) -> list:
    try:
        from deriv_api import get_candles
        candles = get_candles(market, granularity=60, count=150)
        return candles if candles else []
    except Exception as e:
        log.debug(f"[SCALP] get_candles {market}: {e}")
        return []


def _open_multiplier(market: str, direction: str, stake: float,
                     multiplier: int, sl: float, tp: float) -> dict:
    """
    Open a Deriv Multiplier position.
    direction: LONG or SHORT
    sl/tp: dollar amounts for stop loss and take profit (Deriv uses absolute amounts)
    Returns: {contract_id, entry_price, ...} or {}
    """
    try:
        import websocket, json as _j
        contract_type = "MULTUP" if direction == "LONG" else "MULTDOWN"

        ws = websocket.create_connection(
            f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}",
            timeout=15
        )
        ws.send(_j.dumps({"authorize": config.ACTIVE_TOKEN}))
        auth = _j.loads(ws.recv())
        if "error" in auth:
            ws.close()
            return {}

        payload = {
            "buy": 1,
            "price": stake,
            "parameters": {
                "amount":          stake,
                "basis":           "stake",
                "contract_type":   contract_type,
                "currency":        "USD",
                "symbol":          market,
                "multiplier":      multiplier,
                "limit_order": {
                    "stop_loss":    {"order_type": "stop_loss", "order_amount": round(sl, 2)},
                    "take_profit":  {"order_type": "take_profit", "order_amount": round(tp, 2)},
                }
            }
        }
        ws.send(_j.dumps(payload))
        resp = _j.loads(ws.recv())
        ws.close()

        if "buy" in resp:
            b = resp["buy"]
            return {
                "contract_id":  b["contract_id"],
                "entry_price":  float(b.get("spot", 0)),
                "buy_price":    float(b.get("buy_price", stake)),
                "payout":       float(b.get("payout", 0)),
            }
        log.error(f"[DERIV] Open failed: {resp.get('error',{}).get('message','')}")
        return {}
    except Exception as e:
        log.error(f"[DERIV] Open multiplier error: {e}")
        return {}


def _get_position_status(contract_id: int) -> dict:
    """Check if a multiplier position is still open and get current P&L."""
    try:
        import websocket, json as _j
        ws = websocket.create_connection(
            f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}",
            timeout=15
        )
        ws.send(_j.dumps({"authorize": config.ACTIVE_TOKEN}))
        _j.loads(ws.recv())
        ws.send(_j.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id,
        }))
        resp = _j.loads(ws.recv())
        ws.close()

        poc = resp.get("proposal_open_contract", {})
        status = poc.get("status", "open")
        profit = float(poc.get("profit", 0))
        current_spot = float(poc.get("current_spot", 0))

        if status in ("sold", "won", "lost"):
            return {"status": "closed", "profit": profit, "current_spot": current_spot}
        return {"status": "open", "profit": profit, "current_spot": current_spot}
    except Exception as e:
        log.debug(f"[DERIV] Status check {contract_id}: {e}")
        return {"status": "unknown", "profit": 0}


def _close_position(contract_id: int) -> dict:
    """Manually close a multiplier position."""
    try:
        import websocket, json as _j
        ws = websocket.create_connection(
            f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}",
            timeout=15
        )
        ws.send(_j.dumps({"authorize": config.ACTIVE_TOKEN}))
        _j.loads(ws.recv())
        ws.send(_j.dumps({"sell": contract_id, "price": 0}))
        resp = _j.loads(ws.recv())
        ws.close()
        sold = resp.get("sell", {})
        return {
            "ok":     "error" not in resp,
            "profit": float(sold.get("sold_for", 0)) - float(sold.get("buy_price", 0)),
        }
    except Exception as e:
        log.error(f"[DERIV] Close error: {e}")
        return {"ok": False, "profit": 0}


# ─────────────────────────────────────────
# TRADE HISTORY
# ─────────────────────────────────────────
def _save_trade(trade: dict):
    try:
        try:
            with open(HISTORY_FILE) as f:
                data = json.load(f)
        except:
            data = {"trades": []}
        data["trades"].append(trade)
        if len(data["trades"]) > 500:
            data["trades"] = data["trades"][-500:]
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"[HISTORY] Save failed: {e}")


def _update_trade(contract_id, updates: dict):
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        for t in data["trades"]:
            if str(t.get("contract_id")) == str(contract_id):
                t.update(updates)
                break
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"[HISTORY] Update failed: {e}")


def get_trade_history() -> list:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f).get("trades", [])
    except:
        return []


# ─────────────────────────────────────────
# POSITION MONITOR (with trailing stop and strategy stats)
# ─────────────────────────────────────────
def _monitor_positions():
    """Background thread — polls open positions every 10 seconds and manages trailing stop."""
    global _open_positions, _risk_manager, strategy_stats

    while _bot_running:
        time.sleep(10)
        if not _open_positions:
            continue

        closed_ids = []
        with _lock:
            positions_copy = dict(_open_positions)

        for cid, pos in positions_copy.items():
            try:
                status = _get_position_status(cid)
                if status["status"] == "closed":
                    profit = float(status.get("profit", 0))
                    result = "won" if profit > 0 else "lost"
                    strategy = pos.get("strategy", "unknown")

                    with _lock:
                        if _risk_manager:
                            if profit > 0:
                                _risk_manager.record_win(profit)
                                if _compound_enabled:
                                    reinvest = profit * (_compound_pct / 100)
                                    _risk_manager.balance += reinvest
                                    log.info(f"[COMPOUND] Reinvesting ${reinvest:.2f} ({_compound_pct}% of ${profit:.2f} win)")
                                # Update strategy stats
                                with _strategy_stats_lock:
                                    if strategy not in strategy_stats:
                                        strategy_stats[strategy] = {"wins": 0, "losses": 0}
                                    strategy_stats[strategy]["wins"] += 1
                            else:
                                _risk_manager.record_loss(abs(profit))
                                with _strategy_stats_lock:
                                    if strategy not in strategy_stats:
                                        strategy_stats[strategy] = {"wins": 0, "losses": 0}
                                    strategy_stats[strategy]["losses"] += 1
                        closed_ids.append(cid)

                    _update_trade(cid, {
                        "result": result,
                        "profit": round(profit, 2),
                        "close_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "current_spot": status.get("current_spot", 0),
                    })
                    log.info(f"[SCALP] {'✅ WON' if profit>0 else '❌ LOST'} #{cid} | P&L: ${profit:+.2f} | {pos['market']} {pos['direction']} | {strategy}")

                else:
                    # Position still open – trailing stop logic
                    profit = status["profit"]
                    stake = pos.get("stake", 0)
                    if stake <= 0:
                        continue

                    trailing_activated = pos.get("trailing_activated", False)
                    peak_profit = pos.get("peak_profit", profit)

                    if profit > peak_profit:
                        peak_profit = profit
                        with _lock:
                            if cid in _open_positions:
                                _open_positions[cid]["peak_profit"] = peak_profit
                                _open_positions[cid]["peak_price"] = status.get("current_spot", pos.get("entry_price", 0))

                    if not trailing_activated and profit > stake * TRAIL_ACTIVATE_PCT:
                        with _lock:
                            if cid in _open_positions:
                                _open_positions[cid]["trailing_activated"] = True
                                _open_positions[cid]["peak_profit"] = peak_profit
                        trailing_activated = True
                        log.info(f"[TRAIL] Activated trailing for #{cid} | profit ${profit:.2f} > ${stake * TRAIL_ACTIVATE_PCT:.2f}")

                    if trailing_activated:
                        drawdown = peak_profit - profit
                        if drawdown > stake * TRAIL_DISTANCE_PCT:
                            log.info(f"[TRAIL] Closing #{cid} | drawdown ${drawdown:.2f} from peak ${peak_profit:.2f}")
                            close_result = _close_position(cid)
                            if close_result.get("ok", False):
                                profit = close_result["profit"]
                                # Record the closed trade manually
                                with _lock:
                                    if _risk_manager:
                                        if profit > 0:
                                            _risk_manager.record_win(profit)
                                            if _compound_enabled:
                                                reinvest = profit * (_compound_pct / 100)
                                                _risk_manager.balance += reinvest
                                                log.info(f"[COMPOUND] Reinvesting ${reinvest:.2f} ({_compound_pct}% of ${profit:.2f} win)")
                                            # Update strategy stats
                                            strat = pos.get("strategy", "unknown")
                                            with _strategy_stats_lock:
                                                if strat not in strategy_stats:
                                                    strategy_stats[strat] = {"wins": 0, "losses": 0}
                                                strategy_stats[strat]["wins"] += 1
                                        else:
                                            _risk_manager.record_loss(abs(profit))
                                            strat = pos.get("strategy", "unknown")
                                            with _strategy_stats_lock:
                                                if strat not in strategy_stats:
                                                    strategy_stats[strat] = {"wins": 0, "losses": 0}
                                                strategy_stats[strat]["losses"] += 1
                                    closed_ids.append(cid)
                                _update_trade(cid, {
                                    "result": "won" if profit > 0 else "lost",
                                    "profit": round(profit, 2),
                                    "close_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                                    "close_reason": "trailing",
                                    "current_spot": status.get("current_spot", 0),
                                })
                                log.info(f"[SCALP] {'✅ WON' if profit>0 else '❌ LOST'} (trailing) #{cid} | P&L: ${profit:+.2f}")
                            else:
                                log.warning(f"[TRAIL] Failed to close #{cid}")
                            continue

                    # Update floating P&L in history (only if still open)
                    if cid not in closed_ids:
                        _update_trade(cid, {
                            "floating_pnl": round(profit, 2),
                            "current_spot": status.get("current_spot", 0),
                            "peak_profit": peak_profit if trailing_activated else None,
                        })

            except Exception as e:
                log.debug(f"[MONITOR] {cid}: {e}")

        # Remove closed positions from memory
        if closed_ids:
            with _lock:
                for cid in closed_ids:
                    _open_positions.pop(cid, None)


# ─────────────────────────────────────────
# MAIN SCAN LOOP
# ─────────────────────────────────────────
def _scan_loop():
    global _bot_running, _risk_manager, _open_positions, last_signals

    log.info("[SCALP] Bot started — scanning forex markets")
    scan_count = 0

    while _bot_running:
        scan_count += 1

        if _risk_manager:
            if _risk_manager.daily_loss_hit():
                log.warning("[SCALP] Daily loss limit hit — pausing until tomorrow")
                time.sleep(3600)
                continue
            if _risk_manager.daily_target_hit():
                log.info("[SCALP] 🎯 Daily profit target hit — well done!")
                time.sleep(3600)
                continue

        with _lock:
            open_count = len(_open_positions)

        if open_count >= MAX_OPEN_POSITIONS:
            log.debug(f"[SCALP] Max positions ({MAX_OPEN_POSITIONS}) open — waiting")
            time.sleep(SCAN_INTERVAL)
            continue

        log.info(f"[SCALP] Scan #{scan_count} | Open: {open_count}/{MAX_OPEN_POSITIONS} | Balance: ${_risk_manager.balance:.2f} | Markets: {len(SCALP_MARKETS)}")

        signals = []
        signals_lock = threading.Lock()

        def _scan_market(market):
            try:
                blocked, reason = news_filter.is_news_time(market)
                if blocked:
                    log.info(f"[SCALP] {market} 📰 {reason}")
                    return
                candles = _get_candles(market)
                min_candles = 40 if market in ("frxXAUUSD", "frxXAGUSD") else 60
                if not candles or len(candles) < min_candles:
                    log.warning(f"[SCALP] {market} insufficient candles ({len(candles) if candles else 0}/{min_candles})")
                    return
                signal = analyze_market(candles, market)
                if signal and signal.get("confirmed"):
                    log.info(f"[SCALP] ✅ {market} {signal['direction']} | {signal['strategy']} | {signal['confidence'].upper()} | x{signal['multiplier']}")
                    with _lock:
                        already_open = any(p["market"] == market for p in _open_positions.values())
                    if not already_open:
                        # Add timestamp for dashboard
                        signal["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        with signals_lock:
                            signals.append(signal)
                    else:
                        log.info(f"[SCALP] {market} already has open position — skipping")
                else:
                    log.debug(f"[SCALP] {market} no signal")
            except Exception as e:
                log.warning(f"[SCALP] {market} scan error: {e}")

        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="ScalpScan") as ex:
            futs = {ex.submit(_scan_market, m): m for m in SCALP_MARKETS}
            for fut in as_completed(futs, timeout=30):
                try:
                    fut.result()
                except:
                    pass

        # Store signals for dashboard (keep last 10)
        if signals:
            with _lock:
                # Prepend to keep newest first, limit to 10
                last_signals[:] = (signals + last_signals)[:10]

        if not signals:
            log.info(f"[SCALP] No signals this scan — waiting {SCAN_INTERVAL}s")
            time.sleep(SCAN_INTERVAL)
            continue

        # Sort by confidence — HIGH first
        signals.sort(key=lambda s: 0 if s.get("confidence") == "high" else 1)

        slots = MAX_OPEN_POSITIONS - open_count
        for signal in signals[:slots]:
            _execute_trade(signal)
            time.sleep(2)

        time.sleep(SCAN_INTERVAL)

    log.info("[SCALP] Bot stopped")


def _execute_trade(signal: dict):
    global _open_positions, _risk_manager

    if not _risk_manager:
        return

    market     = signal["market"]
    direction  = signal["direction"]
    strategy   = signal["strategy"]
    multiplier = signal["multiplier"]
    confidence = signal["confidence"]
    reason     = signal["reason"]
    sl_dist    = signal.get("sl_distance", 0.0002)
    tp_dist    = signal.get("tp_distance", 0.0004)

    stake = _risk_manager.stake_for_trade()
    sl_dollars = round(stake, 2)
    rr_ratio   = tp_dist / sl_dist if sl_dist > 0 else 2.0
    tp_dollars = round(stake * rr_ratio, 2)

    log.info(f"[SCALP] ⚡ {market} {direction} | Strategy: {strategy} | x{multiplier} | Stake: ${stake:.2f} | SL: ${sl_dollars:.2f} | TP: ${tp_dollars:.2f} | RR: 1:{rr_ratio:.1f}")

    result = _open_multiplier(market, direction, stake, multiplier, sl_dollars, tp_dollars)
    if not result:
        log.warning(f"[SCALP] Failed to open {market} {direction}")
        return

    cid = result["contract_id"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    trade = {
        "contract_id":  cid,
        "market":       market,
        "symbol":       market,
        "direction":    direction,
        "strategy":     strategy,
        "multiplier":   multiplier,
        "stake":        stake,
        "sl":           sl_dollars,
        "tp":           tp_dollars,
        "sl_distance":  sl_dist,
        "tp_distance":  tp_dist,
        "rr_ratio":     round(rr_ratio, 1),
        "confidence":   confidence,
        "reason":       reason,
        "entry_price":  result.get("entry_price", 0),
        "result":       "open",
        "profit":       0,
        "floating_pnl": 0,
        "time":         now,
        "type":         "multiplier",
    }

    with _lock:
        _open_positions[cid] = {
            "market":    market,
            "direction": direction,
            "strategy":  strategy,
            "stake":     stake,
            "entry_price": result.get("entry_price", 0),
            "opened_at": time.time(),
            "trailing_activated": False,
            "peak_profit": 0.0,
            "peak_price": 0.0,
        }

    _save_trade(trade)
    log.info(f"[SCALP] Opened #{cid} | {market} {direction} x{multiplier} | {strategy}")


# ─────────────────────────────────────────
# PUBLIC INTERFACE
# ─────────────────────────────────────────
def start():
    global _bot_running, _bot_thread, _risk_manager

    if _bot_running:
        return {"ok": False, "error": "Already running"}

    balance = _get_balance()
    if balance <= 0:
        return {"ok": False, "error": "Could not fetch balance"}

    _risk_manager = ScalpRiskManager(balance)
    _bot_running  = True

    mon = threading.Thread(target=_monitor_positions, daemon=True, name="PositionMonitor")
    mon.start()

    _bot_thread = threading.Thread(target=_scan_loop, daemon=True, name="ScalpBot")
    _bot_thread.start()

    load_compound_settings()
    log.info(f"[SCALP] Started | Balance: ${balance:.2f}")
    return {"ok": True, "balance": balance}


def stop():
    global _bot_running
    _bot_running = False
    log.info("[SCALP] Stop requested")
    return {"ok": True}


def is_running() -> bool:
    return _bot_running and (_bot_thread is not None and _bot_thread.is_alive())


def get_status() -> dict:
    global _risk_manager, _open_positions, last_signals, strategy_stats
    with _lock:
        open_pos = list(_open_positions.values())
        signals_copy = list(last_signals)  # thread-safe copy
    with _strategy_stats_lock:
        stats_copy = dict(strategy_stats)

    # Build AI strategy performance data
    ai_strategy = {}
    for strat, s in stats_copy.items():
        total = s["wins"] + s["losses"]
        win_rate = (s["wins"] / total * 100) if total > 0 else 0.0
        ai_strategy[strat] = {
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": win_rate,
            "total": total,
            "source": "live",
        }

    return {
        "running":        is_running(),
        "open_positions": len(open_pos),
        "positions":      open_pos,
        "risk":           _risk_manager.get_summary() if _risk_manager else None,
        "compound":       get_compound_settings(),
        "last_signals":   signals_copy,
        "ai_strategy":    {"win_rates": ai_strategy},
    }


def close_all():
    """Emergency close all open positions."""
    with _lock:
        positions = dict(_open_positions)
    for cid in positions:
        result = _close_position(cid)
        log.info(f"[SCALP] Force closed #{cid}: {result}")
    return {"ok": True, "closed": len(positions)}


# ── Compatibility alias — server.py calls run_bot() ──────────
def run_bot():
    """
    Called by server.py _run_bot_safe thread.
    Blocks until bot is stopped.
    """
    global _bot_running, _bot_thread, _open_positions, _risk_manager

    _bot_running    = False
    _open_positions = {}

    result = start()
    if not result["ok"]:
        raise RuntimeError(result.get("error", "Failed to start scalp bot"))

    while is_running():
        time.sleep(5)
    log.info("[SCALP] run_bot() exiting")
