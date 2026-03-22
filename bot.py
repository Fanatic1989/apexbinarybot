import time
import logging
import json
import os
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from deriv_api import get_candles, get_balance, place_trade, get_contract_result
from strategy import analyze_market, record_trade_outcome
from risk_manager import RiskManager
from staking import StakingEngine
from telegram_bot import send_signal, send_alert
from news_filter import news_filter

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Module-level state ────────────────────
risk_manager    = None
staking_engine  = None
last_signals    = []
_market_losses  = {}
_market_paused  = {}
_MARKET_PAUSE_S = 1200
_market_locks   = {}
_global_lock    = threading.Lock()
_session_trades = 0
_session_lock   = threading.Lock()

SESSION_1_HOURS = 12
REST_HOURS      = 1
SESSION_2_HOURS = 11
MAX_TRADES_S1   = 55
MAX_TRADES_S2   = 45

# ─────────────────────────────────────────
# MINIMUM PAYOUT RATIO PER RISK LEVEL
# ─────────────────────────────────────────
# If you lose $10, the next win must return at least:
#   1% risk → $15  (1.5x)
#   2% risk → $17.50 (1.75x)
#   3% risk → $20  (2.0x)
#
# Deriv payout = stake + profit, so:
#   min_payout_ratio = (stake + min_profit) / stake
#   1% → profit >= 1.5x stake  → payout ratio >= 2.5
#   2% → profit >= 1.75x stake → payout ratio >= 2.75
#   3% → profit >= 2.0x stake  → payout ratio >= 3.0
#
# In practice Deriv binary payouts are expressed as total payout / stake:
#   e.g. stake=$10, payout=$18 → ratio=1.8 → profit=$8 (0.8x stake)
# We use profit/stake (not payout/stake) for the ratio check.

MIN_PROFIT_RATIO = {
    1: 1.5,   # 1% risk: profit must be >= 1.5x stake
    2: 1.75,  # 2% risk: profit must be >= 1.75x stake
    3: 2.0,   # 3% risk: profit must be >= 2.0x stake
}


def _get_min_profit_ratio() -> float:
    """Return the minimum profit/stake ratio for the current risk setting."""
    risk_pct = int(getattr(config, "STAKE_PERCENT", 1))
    return MIN_PROFIT_RATIO.get(risk_pct, 1.5)


def _check_payout_ratio(market: str, direction: str,
                        stake: float, expiry: int) -> tuple:
    """
    Fetch a Deriv proposal for this trade and check if the payout ratio
    meets the minimum recovery threshold for the current risk level.

    Returns:
        (passes: bool, profit: float, ratio: float, reason: str)

    Uses get_proposal from deriv_api if available, otherwise falls back
    to a direct websocket call.
    """
    min_ratio = _get_min_profit_ratio()

    try:
        # Try to use get_proposal from deriv_api if it exists
        from deriv_api import get_proposal
        proposal = get_proposal(
            symbol=market,
            direction=direction,
            stake=stake,
            duration_minutes=expiry
        )
    except (ImportError, AttributeError):
        # get_proposal not in deriv_api yet — use direct websocket call
        proposal = _fetch_proposal_direct(market, direction, stake, expiry)

    if not proposal:
        # Can't verify payout — allow trade but log warning
        log.warning(f"[{market}] Could not fetch proposal — skipping payout check")
        return True, 0.0, 0.0, "proposal_unavailable"

    payout = float(proposal.get("payout", 0))
    if payout <= 0:
        return False, 0.0, 0.0, f"invalid payout ${payout:.2f}"

    profit = payout - stake
    ratio  = profit / stake if stake > 0 else 0

    passes = ratio >= min_ratio
    reason = (
        f"profit=${profit:.2f} ratio={ratio:.2f}x "
        f"(need {min_ratio:.2f}x for {int(getattr(config,'STAKE_PERCENT',1))}% risk)"
    )

    return passes, profit, ratio, reason


def _fetch_proposal_direct(market: str, direction: str,
                           stake: float, expiry: int) -> dict:
    """
    Fallback: fetch a proposal directly via websocket if deriv_api
    doesn't expose get_proposal yet.
    """
    import websocket as _ws
    import json as _json

    contract_type = "CALL" if direction == "CALL" else "PUT"
    ws = None
    try:
        url = (f"wss://ws.derivws.com/websockets/v3"
               f"?app_id={config.DERIV_APP_ID}")
        ws = _ws.create_connection(url, timeout=10)

        # Authorise
        ws.send(_json.dumps({"authorize": config.ACTIVE_TOKEN}))
        auth = _json.loads(ws.recv())
        if "error" in auth:
            log.warning(f"[PROPOSAL] Auth failed: {auth['error']['message']}")
            return {}

        # Request proposal
        ws.send(_json.dumps({
            "proposal":       1,
            "amount":         stake,
            "basis":          "stake",
            "contract_type":  contract_type,
            "currency":       "USD",
            "duration":       expiry,
            "duration_unit":  "m",
            "symbol":         market,
        }))
        resp = _json.loads(ws.recv())

        if "error" in resp:
            log.debug(f"[PROPOSAL] {market} error: {resp['error']['message']}")
            return {}

        return resp.get("proposal", {})

    except Exception as e:
        log.debug(f"[PROPOSAL] {market} fetch failed: {e}")
        return {}
    finally:
        if ws:
            try: ws.close()
            except: pass


# ─────────────────────────────────────────

def run_bot():
    global risk_manager, staking_engine, _session_trades

    log.info("=" * 55)
    log.info("  APEX BOT — PARALLEL SCAN MODE")
    log.info(f"  Mode: {config.MODE.upper()} | Markets: {len(config.MARKETS)}")
    log.info("=" * 55)

    balance = 0.0
    for attempt in range(1, 6):
        log.info(f"[BOT] Connecting attempt {attempt}/5 | App: {config.DERIV_APP_ID}")
        balance = get_balance()
        if balance > 0: break
        log.warning("[BOT] Failed. Retrying in 10s...")
        time.sleep(10)

    if balance <= 0:
        log.error("[BOT] Could not connect to Deriv")
        raise ConnectionError("Deriv connection failed")

    risk_manager   = RiskManager(starting_balance=balance)
    base_stake     = max(balance * (config.STAKE_PERCENT/100), 0.35)
    staking_engine = StakingEngine(base_stake=base_stake, balance=balance)

    import bot as _s
    _s.risk_manager   = risk_manager
    _s.staking_engine = staking_engine

    log.info(f"[BOT] Connected | Balance: ${balance:.2f} | Stake: ${base_stake:.2f}")
    send_alert(f"🚀 Apex Bot started\nMode: {config.MODE.upper()}\n"
               f"Balance: ${balance:.2f}\nParallel scanning {len(config.MARKETS)} markets")

    while True:
        try:
            _session_trades = 0
            _run_session("SESSION 1", MAX_TRADES_S1, SESSION_1_HOURS)
            log.info(f"[BOT] 💤 REST {REST_HOURS}h")
            send_alert(f"💤 Rest\nBalance: ${risk_manager.current_balance:.2f}")
            time.sleep(REST_HOURS * 3600)
            fresh = get_balance()
            if fresh > 0:
                risk_manager.current_balance = fresh
                staking_engine.update_balance(fresh)
            _session_trades = 0
            _run_session("SESSION 2", MAX_TRADES_S2, SESSION_2_HOURS)
        except Exception as e:
            log.error(f"[BOT] Error: {e} — restart in 60s")
            time.sleep(60)
            continue

        fresh = get_balance()
        risk_manager.reset_daily(fresh if fresh > 0 else None)
        send_alert(f"📋 Daily complete\nW:{risk_manager.total_wins} "
                   f"L:{risk_manager.total_losses}\n"
                   f"P&L: ${risk_manager.net_pnl:.2f}\n"
                   f"Balance: ${risk_manager.current_balance:.2f}")


def _run_session(name, max_trades, max_hours):
    global _session_trades
    log.info(f"[BOT] ▶ {name} | Target: {max_trades} | {max_hours}h")
    send_alert(f"▶ {name}\nTarget: {max_trades} trades\n"
               f"Balance: ${risk_manager.current_balance:.2f}")

    session_start = time.time()
    max_seconds   = max_hours * 3600
    scan_count    = 0

    while True:
        elapsed = time.time() - session_start
        if elapsed >= max_seconds:
            log.info(f"[BOT] {name} time limit"); break

        with _session_lock:
            tc = _session_trades
        if tc >= max_trades:
            log.info(f"[BOT] {name} target reached ({max_trades})"); break

        if risk_manager.daily_loss_limit_hit():
            msg = "🛑 Daily loss limit hit"
            log.warning(msg); send_alert(msg)
            _sleep_until_midnight()
            risk_manager.reset_daily(get_balance() or None)
            break

        if risk_manager.is_paused():
            rem = risk_manager.pause_remaining()
            if int(rem) % 300 < 31:
                log.info(f"[BOT] ⏸ Paused {rem/60:.1f}min left")
            time.sleep(min(rem, 30))
            continue

        scan_count += 1
        active = config.get_active_markets()
        log.info(f"[BOT] {name} Scan #{scan_count} | "
                 f"Trades:{tc}/{max_trades} | "
                 f"Markets:{len(active)} | "
                 f"Balance:${risk_manager.current_balance:.2f}")

        _parallel_scan(active)
        time.sleep(config.SCAN_INTERVAL)


def _parallel_scan(markets):
    """
    Scan all markets simultaneously for signals.
    Collect ALL signals then trade only the SINGLE best one.
    """
    signals_found = []
    signals_lock  = threading.Lock()

    def _scan_for_signal(market):
        try:
            if time.time() < _market_paused.get(market, 0):
                return
            from news_filter import news_filter
            blocked, reason = news_filter.is_news_time(market)
            if blocked:
                log.info(f"[{market}] 📰 {reason}")
                return
            candles = get_candles(market)
            if not candles or len(candles) < 40:
                return
            signal = analyze_market(candles, market)
            import bot as _b
            _b.last_signals = [s for s in _b.last_signals if s.get("market") != market]
            if signal and signal.get("direction") != "NONE":
                _b.last_signals.append({
                    "market":     market,
                    "direction":  signal.get("direction", "NONE"),
                    "confidence": signal.get("confidence", "low"),
                    "strategy":   signal.get("strategy", "—"),
                    "timestamp":  datetime.utcnow().strftime("%H:%M:%S")
                })
                if len(_b.last_signals) > 40:
                    _b.last_signals = _b.last_signals[-40:]
            if not signal or not signal.get("confirmed", False):
                return
            if signal.get("direction") == "NONE":
                return
            score = 2 if signal.get("confidence") == "high" else 1
            with signals_lock:
                signals_found.append((score, market, signal, candles))
        except Exception as e:
            log.error(f"[{market}] Scan error: {e}")

    max_workers = min(8, len(markets))
    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="Scan") as ex:
        futures = {ex.submit(_scan_for_signal, m): m for m in markets}
        try:
            for fut in as_completed(futures, timeout=60):
                try: fut.result()
                except Exception as e:
                    log.error(f"[{futures[fut]}] Thread error: {e}")
        except Exception as e:
            log.warning(f"[BOT] Scan timeout: {e}")

    if not signals_found:
        log.debug("[BOT] No confirmed signals this scan")
        return

    put_signals  = [s for s in signals_found if s[2].get("direction") == "PUT"]
    call_signals = [s for s in signals_found if s[2].get("direction") == "CALL"]
    total        = len(signals_found)
    put_count    = len(put_signals)
    call_count   = len(call_signals)

    log.info(f"[BOT] {total} signal(s): {put_count} PUT / {call_count} CALL")

    if total >= 2 and put_count == call_count:
        log.info(f"[BOT] ⚠️ Signals split {put_count}P/{call_count}C — "
                 f"no dominant direction, skipping scan")
        return

    if total < 2:
        log.info(f"[BOT] Only {total} confirmed signal — need 2+ to trade")
        return

    if put_count > call_count:
        dominant  = put_signals
        direction = "PUT"
    else:
        dominant  = call_signals
        direction = "CALL"

    minority = call_count if direction == "PUT" else put_count
    if minority >= len(dominant):
        log.info(f"[BOT] No clear majority: {put_count}P/{call_count}C — skip")
        return

    log.info(f"[BOT] Dominant direction: {direction} "
             f"({len(dominant)}/{total} signals agree)")

    dominant.sort(key=lambda x: x[0], reverse=True)
    best_score, best_market, best_signal, best_candles = dominant[0]

    log.info(f"[BOT] Trading best {direction}: {best_market} "
             f"{best_signal.get('confidence','').upper()}")

    _scan_market(best_market, best_signal)


def _scan_market(market, signal=None):
    global _session_trades

    if time.time() < _market_paused.get(market, 0):
        return

    lock = _market_locks.setdefault(market, threading.Lock())
    if not lock.acquire(blocking=False):
        return

    try:
        if signal is None:
            candles = get_candles(market)
            if not candles or len(candles) < 40:
                return
            signal = analyze_market(candles, market)

        if not signal or signal.get("direction") == "NONE":
            return
        if not signal.get("confirmed", False):
            log.info(f"[{market}] {signal.get('direction')} score "
                     f"{signal.get('score',0)}/5 — not confirmed")
            return

        direction  = signal["direction"]
        confidence = signal.get("confidence", "normal")
        strategy   = signal.get("strategy", "unknown")
        expiry     = config.get_expiry(market)

        with _global_lock:
            stake = staking_engine.get_stake() if staking_engine else 0.35

        # ── PAYOUT RATIO CHECK ────────────────────────────────
        # Before placing any trade, verify the payout meets the
        # minimum recovery ratio for the current risk level.
        passes, exp_profit, ratio, reason = _check_payout_ratio(
            market, direction, stake, expiry
        )

        min_ratio = _get_min_profit_ratio()
        risk_pct  = int(getattr(config, "STAKE_PERCENT", 1))

        if not passes:
            log.info(
                f"[{market}] ⛔ Payout too low — {reason} "
                f"| Risk {risk_pct}% needs {min_ratio:.2f}x min — SKIPPING"
            )
            return

        log.info(
            f"[{market}] ✅ Payout OK — {reason} "
            f"| Expected profit: ${exp_profit:.2f}"
        )
        # ─────────────────────────────────────────────────────

        log.info(f"[{market}] ⚡ {direction} | {confidence.upper()} | "
                 f"Strategy: {strategy} | Expiry: {expiry}m | Stake: ${stake:.2f}")

        send_signal(market=market, direction=direction,
                    expiry=expiry, confidence=confidence, stake=stake)

        trade = place_trade(symbol=market, direction=direction,
                            stake=stake, duration_minutes=expiry)
        if not trade:
            log.error(f"[{market}] Trade placement failed")
            return

        contract_id   = trade["contract_id"]
        actual_stake  = trade.get("stake",  stake)
        actual_payout = trade.get("payout", 0)

        with _session_lock:
            _session_trades += 1
            tc = _session_trades

        log.info(f"[{market}] Trade #{tc} placed — contract #{contract_id}")

        _save_trade({
            "contract_id": contract_id,
            "symbol":      market,
            "direction":   direction,
            "stake":       round(float(actual_stake),  2),
            "payout":      round(float(actual_payout), 2),
            "result":      "open",
            "profit":      0,
            "expiry":      expiry,
            "confidence":  confidence,
            "strategy":    strategy,
        })

        _wait_for_settlement(expiry, market)

        outcome    = None
        max_polls  = 20 if market.startswith("frx") else 12
        poll_sleep = 15 if market.startswith("frx") else 8

        for attempt in range(max_polls):
            try:
                outcome = get_contract_result(contract_id)
            except Exception as pe:
                log.warning(f"[{market}] Poll error {attempt+1}: {pe}")
                outcome = None
            if outcome and outcome.get("status") in ("won", "lost"):
                log.info(f"[{market}] #{contract_id} settled: "
                         f"{outcome['status'].upper()} on poll {attempt+1}")
                break
            log.info(f"[{market}] #{contract_id} still open — "
                     f"poll {attempt+1}/{max_polls} in {poll_sleep}s")
            time.sleep(poll_sleep)

        if outcome and outcome.get("status") in ("won", "lost"):
            _handle_outcome(market, direction, actual_stake,
                            outcome, trade, signal)
        else:
            log.error(f"[{market}] No result after {max_polls} polls")
            _update_trade(contract_id, {"result": "unresolved"})

    finally:
        lock.release()


def _handle_outcome(market, direction, stake, outcome, trade, signal):
    status      = outcome.get("status")
    profit      = float(outcome.get("profit", 0))
    contract_id = trade.get("contract_id", "—")
    strategy    = signal.get("strategy", "unknown")
    confidence  = signal.get("confidence", "normal")
    expiry      = signal.get("expiry", config.get_expiry(market))
    payout      = trade.get("payout", 0)

    _update_trade(contract_id, {
        "result": status,
        "profit": round(profit if status == "won" else -stake, 2),
    })

    import bot as _b

    if status == "won":
        log.info(f"[{market}] ✅ WON +${profit:.2f}")
        with _global_lock:
            risk_manager.record_win(profit)
            staking_engine.record_win(profit)
        _b._market_losses[market] = 0
        send_alert(f"✅ {market} {direction} WON +${profit:.2f}\n"
                   f"Strategy: {strategy}\n"
                   f"Balance: ${risk_manager.current_balance:.2f}")
        try: record_trade_outcome(market, strategy, "won")
        except: pass

    elif status == "lost":
        log.info(f"[{market}] ❌ LOST -${stake:.2f}")
        with _global_lock:
            risk_manager.record_loss(stake)
            staking_engine.record_loss(stake)
        send_alert(f"❌ {market} {direction} LOST -${stake:.2f}\n"
                   f"Strategy: {strategy}\n"
                   f"Balance: ${risk_manager.current_balance:.2f}")
        try: record_trade_outcome(market, strategy, "lost")
        except: pass

        _b._market_losses[market] = _b._market_losses.get(market, 0) + 1
        if _b._market_losses[market] >= 2:
            resume = time.time() + _b._MARKET_PAUSE_S
            _b._market_paused[market] = resume
            _b._market_losses[market] = 0
            log.warning(f"[{market}] 2 losses — cooling down 20min")

        with _global_lock:
            if risk_manager.consecutive_losses >= config.MAX_CONSECUTIVE_LOSS:
                risk_manager.trigger_pause()
                msg = (f"⏸ {config.MAX_CONSECUTIVE_LOSS} losses in a row\n"
                       f"Pausing {config.PAUSE_DURATION//60}min")
                log.warning(msg); send_alert(msg)


def _save_trade(trade):
    history_file = "trade_history.json"
    empty = {"trades":[],"total_trades":0,"total_wins":0,"total_losses":0,"net_pnl":0.0}
    trade.setdefault("time", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
    trade.setdefault("contract_id", "—")
    trade["stake"]  = round(float(trade.get("stake",  0)), 2)
    trade["payout"] = round(float(trade.get("payout", 0)), 2)
    trade["profit"] = round(float(trade.get("profit", 0)), 2)

    try:
        try:
            with open(history_file) as f: data = json.load(f)
        except: data = empty.copy()
        for k, v in empty.items():
            if k not in data: data[k] = v

        cid = str(trade.get("contract_id", ""))
        if cid and cid != "—":
            for ex in data["trades"]:
                if str(ex.get("contract_id", "")) == cid:
                    ex.update(trade)
                    with open(history_file, "w") as f: json.dump(data, f, indent=2)
                    return

        data["trades"].append(trade)
        data["total_trades"] = data.get("total_trades", 0) + 1
        if trade.get("result") == "won":
            data["total_wins"] = data.get("total_wins", 0) + 1
            data["net_pnl"]    = round(data.get("net_pnl", 0) + abs(trade["profit"]), 2)
        elif trade.get("result") == "lost":
            data["total_losses"] = data.get("total_losses", 0) + 1
            data["net_pnl"]      = round(data.get("net_pnl", 0) - trade["stake"], 2)

        if len(data["trades"]) > 500:
            data["trades"] = data["trades"][-500:]
        with open(history_file, "w") as f: json.dump(data, f, indent=2)
        log.info(f"[HISTORY] Saved {trade['symbol']} {trade['direction']} "
                 f"{trade.get('result','open').upper()}")
    except Exception as e:
        log.error(f"[HISTORY] Save failed: {e}")
        try:
            with open("trade_emergency.log", "a") as ef:
                ef.write(json.dumps(trade) + "\n")
        except: pass


def _update_trade(contract_id, updates):
    history_file = "trade_history.json"
    try:
        with open(history_file) as f: data = json.load(f)
        updated = False
        for t in data["trades"]:
            if str(t.get("contract_id", "")) == str(contract_id):
                t.update(updates)
                updated = True
                break
        if updated:
            result = updates.get("result", "")
            profit = float(updates.get("profit", 0))
            if result == "won":
                data["total_wins"] = data.get("total_wins", 0) + 1
                data["net_pnl"]    = round(data.get("net_pnl", 0) + abs(profit), 2)
            elif result == "lost":
                data["total_losses"] = data.get("total_losses", 0) + 1
                data["net_pnl"]      = round(data.get("net_pnl", 0) + profit, 2)
            with open(history_file, "w") as f: json.dump(data, f, indent=2)
            log.info(f"[HISTORY] Updated #{contract_id} → "
                     f"{updates.get('result','?').upper()}")
        else:
            log.warning(f"[HISTORY] #{contract_id} not found for update")
    except Exception as e:
        log.error(f"[HISTORY] Update failed: {e}")


def _wait_for_settlement(expiry_minutes, market=""):
    extra = 20 if market.startswith("frx") else 15 if "HZ" in market else 8
    wait  = (expiry_minutes * 60) + extra
    log.info(f"[BOT] Waiting {wait}s for {market} settlement...")
    time.sleep(wait)


def _sleep_until_midnight():
    from datetime import timedelta
    now      = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=5, microsecond=0)
    if midnight <= now: midnight += timedelta(days=1)
    secs = (midnight - now).total_seconds()
    log.info(f"[BOT] Sleeping {secs/3600:.1f}h until midnight")
    time.sleep(secs)


if __name__ == "__main__":
    run_bot()
