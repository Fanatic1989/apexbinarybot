"""
Bot Manager — runs isolated bot instances per user.
Each user gets their own thread, their own RiskManager,
their own StakingEngine and their own trade history file.
"""
import threading
import logging
import time
import json
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# username -> { thread, stop_flag, risk_manager, staking_engine, last_signals }
_user_bots = {}
_lock      = threading.Lock()


def _trade_file(username: str) -> str:
    return f"trades_{username}.json"


def start_user_bot(username: str, user_config: dict) -> dict:
    """Start a bot instance for a user with their own config."""
    with _lock:
        existing = _user_bots.get(username)
        if existing and existing["thread"].is_alive():
            return {"ok": False, "error": "Bot already running"}

    stop_flag = threading.Event()

    def _run():
        try:
            _bot_loop(username, user_config, stop_flag)
        except Exception as e:
            log.error(f"[BOT:{username}] Crashed: {e}", exc_info=True)
        finally:
            with _lock:
                if username in _user_bots:
                    _user_bots[username]["running"] = False
            # Update user record
            try:
                import user_manager as um
                um.update_user_settings(username, bot_running=False)
            except: pass
            log.info(f"[BOT:{username}] Thread exited")

    t = threading.Thread(target=_run, daemon=True,
                         name=f"Bot-{username}")
    t.start()

    with _lock:
        _user_bots[username] = {
            "thread":       t,
            "stop_flag":    stop_flag,
            "running":      True,
            "last_signals": [],
            "risk_manager": None,
            "staking":      None,
            "started_at":   datetime.now(timezone.utc).isoformat(),
        }

    import user_manager as um
    um.update_user_settings(username, bot_running=True)

    log.info(f"[BOT:{username}] Started")
    return {"ok": True}


def stop_user_bot(username: str) -> dict:
    with _lock:
        entry = _user_bots.get(username)
    if not entry:
        return {"ok": False, "error": "Bot not running"}
    entry["stop_flag"].set()
    entry["running"] = False
    import user_manager as um
    um.update_user_settings(username, bot_running=False)
    log.info(f"[BOT:{username}] Stop requested")
    return {"ok": True}


def is_running(username: str) -> bool:
    with _lock:
        entry = _user_bots.get(username)
    return bool(entry and entry.get("running") and entry["thread"].is_alive())


def get_user_state(username: str) -> dict:
    with _lock:
        entry = _user_bots.get(username, {})
    rm = entry.get("risk_manager")
    st = entry.get("staking")
    return {
        "running":      entry.get("running", False),
        "last_signals": entry.get("last_signals", []),
        "risk": rm.get_summary() if rm else None,
        "staking": st.get_info() if st else None,
    }


def get_last_signals(username: str) -> list:
    with _lock:
        entry = _user_bots.get(username, {})
    return entry.get("last_signals", [])


def _bot_loop(username: str, user_cfg: dict, stop_flag: threading.Event):
    """
    Full isolated bot loop for one user.
    Uses their own Deriv token, risk settings, trade history.
    """
    import config as base_cfg
    from deriv_api import get_candles, place_trade, get_contract_result
    from risk_manager import RiskManager
    from staking import StakingEngine
    from strategy import analyze_market, record_trade_outcome
    from news_filter import news_filter
    from concurrent.futures import ThreadPoolExecutor, as_completed

    mode       = user_cfg.get("mode", "demo")
    token      = user_cfg.get("demo_token") if mode == "demo" \
                 else user_cfg.get("live_token")
    risk_pct   = float(user_cfg.get("risk_pct", 1))
    tfile      = _trade_file(username)

    if not token:
        log.error(f"[BOT:{username}] No API token set")
        return

    # Get balance using user's token
    balance = _get_balance(token, base_cfg.DERIV_APP_ID)
    if balance <= 0:
        log.error(f"[BOT:{username}] Could not get balance")
        return

    base_stake = max(balance * (risk_pct / 100), 0.35)
    rm         = RiskManager(starting_balance=balance)
    se         = StakingEngine(base_stake=base_stake, balance=balance)

    with _lock:
        if username in _user_bots:
            _user_bots[username]["risk_manager"] = rm
            _user_bots[username]["staking"]      = se

    log.info(f"[BOT:{username}] Connected | Balance: ${balance:.2f} | "
             f"Stake: ${base_stake:.2f} | Mode: {mode.upper()}")

    scan_count    = 0
    session_start = time.time()

    while not stop_flag.is_set():
        # Check subscription is still valid
        import user_manager as um
        if not um.is_allowed_to_trade(username):
            log.warning(f"[BOT:{username}] Subscription expired — stopping")
            break

        if rm.daily_loss_limit_hit():
            log.warning(f"[BOT:{username}] Daily loss limit hit")
            time.sleep(3600)
            rm.reset_daily(None)
            continue

        if rm.is_paused():
            time.sleep(30)
            continue

        scan_count += 1
        active = base_cfg.get_active_markets()

        signals_found = []
        signals_lock  = threading.Lock()

        def _scan(market):
            try:
                blocked, reason = news_filter.is_news_time(market)
                if blocked:
                    return
                candles = _get_candles_for_user(token, base_cfg.DERIV_APP_ID, market)
                if not candles or len(candles) < 40:
                    return
                signal = analyze_market(candles, market)
                if not signal or not signal.get("confirmed"):
                    return
                if signal.get("direction") == "NONE":
                    return
                score = 2 if signal.get("confidence") == "high" else 1
                with signals_lock:
                    signals_found.append((score, market, signal, candles))
                # Update last signals for dashboard
                with _lock:
                    if username in _user_bots:
                        ls = _user_bots[username]["last_signals"]
                        _user_bots[username]["last_signals"] = [
                            s for s in ls if s.get("market") != market
                        ]
                        _user_bots[username]["last_signals"].append({
                            "market":     market,
                            "direction":  signal.get("direction"),
                            "confidence": signal.get("confidence"),
                            "strategy":   signal.get("strategy"),
                            "timestamp":  datetime.utcnow().strftime("%H:%M:%S"),
                        })
            except Exception as e:
                log.debug(f"[BOT:{username}] {market} scan: {e}")

        with ThreadPoolExecutor(max_workers=min(8, len(active)),
                                thread_name_prefix=f"S{username}") as ex:
            futs = {ex.submit(_scan, m): m for m in active}
            for fut in as_completed(futs, timeout=60):
                try: fut.result()
                except: pass

        if not signals_found or len(signals_found) < 2:
            time.sleep(base_cfg.SCAN_INTERVAL)
            continue

        # Confidence-weighted direction vote
        put_sigs   = [s for s in signals_found if s[2].get("direction") == "PUT"]
        call_sigs  = [s for s in signals_found if s[2].get("direction") == "CALL"]
        put_pts    = sum(2 if s[2].get("confidence") == "high" else 1 for s in put_sigs)
        call_pts   = sum(2 if s[2].get("confidence") == "high" else 1 for s in call_sigs)

        if put_pts == call_pts:
            time.sleep(base_cfg.SCAN_INTERVAL)
            continue

        if put_pts > call_pts:
            dominant, direction = put_sigs, "PUT"
            dom_pts, min_pts    = put_pts, call_pts
        else:
            dominant, direction = call_sigs, "CALL"
            dom_pts, min_pts    = call_pts, put_pts

        if dom_pts - min_pts < 2:
            time.sleep(base_cfg.SCAN_INTERVAL)
            continue

        dominant.sort(key=lambda x: x[0], reverse=True)
        _, best_market, best_signal, _ = dominant[0]

        expiry     = base_cfg.get_expiry(best_market)
        stake      = se.get_stake()
        confidence = best_signal.get("confidence", "normal")
        strategy   = best_signal.get("strategy", "unknown")

        # Payout check
        from bot import _fetch_proposal_direct
        proposal = _fetch_proposal_direct(best_market, direction, stake, expiry)
        if proposal:
            payout = float(proposal.get("payout", 0))
            profit = payout - stake
            ratio  = profit / stake if stake > 0 else 0
            min_r  = {1: 0.75, 2: 0.80, 3: 0.85}.get(int(risk_pct), 0.75)
            if ratio < min_r:
                time.sleep(base_cfg.SCAN_INTERVAL)
                continue

        # Place trade using user's token
        trade = _place_trade_for_user(
            token, base_cfg.DERIV_APP_ID,
            best_market, direction, stake, expiry
        )
        if not trade:
            time.sleep(base_cfg.SCAN_INTERVAL)
            continue

        contract_id   = trade["contract_id"]
        actual_stake  = trade.get("stake", stake)
        actual_payout = trade.get("payout", 0)

        _save_user_trade(tfile, {
            "contract_id": contract_id,
            "symbol":      best_market,
            "direction":   direction,
            "stake":       round(float(actual_stake), 2),
            "payout":      round(float(actual_payout), 2),
            "result":      "open",
            "profit":      0,
            "expiry":      expiry,
            "confidence":  confidence,
            "strategy":    strategy,
        })

        log.info(f"[BOT:{username}] ⚡ {best_market} {direction} "
                 f"{confidence.upper()} #{contract_id}")

        # Wait for settlement
        extra = 20 if best_market.startswith("frx") else 8
        time.sleep(expiry * 60 + extra)

        # Poll result
        outcome = None
        for _ in range(15):
            try:
                outcome = _get_result_for_user(
                    token, base_cfg.DERIV_APP_ID, contract_id
                )
                if outcome and outcome.get("status") in ("won", "lost"):
                    break
            except: pass
            time.sleep(8)

        if outcome and outcome.get("status") in ("won", "lost"):
            status = outcome["status"]
            profit = float(outcome.get("profit", 0))

            _update_user_trade(tfile, contract_id, {
                "result": status,
                "profit": round(profit if status == "won" else -actual_stake, 2),
            })

            if status == "won":
                rm.record_win(profit)
                se.record_win(profit)
                log.info(f"[BOT:{username}] ✅ WON +${profit:.2f}")
                record_trade_outcome(best_market, strategy, "won",
                                     regime=best_signal.get("regime","any"))
            else:
                rm.record_loss(actual_stake)
                se.record_loss(actual_stake)
                log.info(f"[BOT:{username}] ❌ LOST -${actual_stake:.2f}")
                record_trade_outcome(best_market, strategy, "lost",
                                     regime=best_signal.get("regime","any"))

            # Update user stats
            um.update_user_settings(username,
                total_trades=rm.total_trades,
                total_wins=rm.total_wins,
                total_losses=rm.total_losses,
                net_pnl=round(rm.net_pnl, 2),
            )

            if (status == "lost" and
                    rm.consecutive_losses >= base_cfg.MAX_CONSECUTIVE_LOSS):
                rm.trigger_pause()

        time.sleep(base_cfg.SCAN_INTERVAL)


# ── Deriv API helpers using per-user token ────────────────────

def _ws_call(token: str, app_id: str, payload: dict) -> dict:
    import websocket, json as _j
    ws = None
    try:
        ws = websocket.create_connection(
            f"wss://ws.derivws.com/websockets/v3?app_id={app_id}",
            timeout=15
        )
        ws.send(_j.dumps({"authorize": token}))
        auth = _j.loads(ws.recv())
        if "error" in auth:
            return {}
        ws.send(_j.dumps(payload))
        return _j.loads(ws.recv())
    except: return {}
    finally:
        if ws:
            try: ws.close()
            except: pass


def _get_balance(token: str, app_id: str) -> float:
    r = _ws_call(token, app_id, {"balance": 1, "account": "current"})
    try: return float(r["balance"]["balance"])
    except: return 0.0


def _get_candles_for_user(token: str, app_id: str, market: str) -> list:
    r = _ws_call(token, app_id, {
        "ticks_history": market,
        "adjust_start_time": 1,
        "count": 120,
        "end": "latest",
        "style": "candles",
        "granularity": 60,
    })
    return r.get("candles", [])


def _place_trade_for_user(token: str, app_id: str,
                           market: str, direction: str,
                           stake: float, expiry: int) -> dict:
    contract_type = "CALL" if direction == "CALL" else "PUT"
    r = _ws_call(token, app_id, {
        "buy": 1,
        "price": stake,
        "parameters": {
            "amount":          stake,
            "basis":           "stake",
            "contract_type":   contract_type,
            "currency":        "USD",
            "duration":        expiry,
            "duration_unit":   "m",
            "symbol":          market,
        }
    })
    if "buy" in r:
        return {
            "contract_id": r["buy"]["contract_id"],
            "stake":       r["buy"].get("buy_price", stake),
            "payout":      r["buy"].get("payout", 0),
        }
    return {}


def _get_result_for_user(token: str, app_id: str,
                          contract_id: int) -> dict:
    r = _ws_call(token, app_id, {
        "proposal_open_contract": 1,
        "contract_id": contract_id,
    })
    poc = r.get("proposal_open_contract", {})
    status = poc.get("status", "")
    profit = float(poc.get("profit", 0))
    if status == "won":
        return {"status": "won", "profit": profit}
    if status == "lost":
        return {"status": "lost", "profit": profit}
    return {}


# ── Trade history helpers ─────────────────────────────────────

def _save_user_trade(tfile: str, trade: dict):
    trade.setdefault("time",
        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
    trade["stake"]  = round(float(trade.get("stake",  0)), 2)
    trade["payout"] = round(float(trade.get("payout", 0)), 2)
    trade["profit"] = round(float(trade.get("profit", 0)), 2)
    try:
        try:
            with open(tfile) as f: data = json.load(f)
        except: data = {"trades": []}
        data["trades"].append(trade)
        if len(data["trades"]) > 500:
            data["trades"] = data["trades"][-500:]
        with open(tfile, "w") as f: json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"[TRADES] Save failed: {e}")


def _update_user_trade(tfile: str, contract_id, updates: dict):
    try:
        with open(tfile) as f: data = json.load(f)
        for t in data["trades"]:
            if str(t.get("contract_id")) == str(contract_id):
                t.update(updates)
                break
        with open(tfile, "w") as f: json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"[TRADES] Update failed: {e}")


def get_user_trades(username: str) -> list:
    tfile = _trade_file(username)
    try:
        with open(tfile) as f:
            return json.load(f).get("trades", [])
    except: return []
