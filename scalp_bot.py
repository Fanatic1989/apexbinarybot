import threading
import logging
import time
import config

# ✅ SAFE IMPORT (CRITICAL FIX)
try:
    from risk_state import save_state, load_state, restore_risk_manager
except Exception as e:
    print(f"[SAFE IMPORT] risk_state failed: {e}")
    def save_state(*args, **kwargs): pass
    def load_state(*args, **kwargs): return None
    def restore_risk_manager(*args, **kwargs): pass

log = logging.getLogger(__name__)

_bot_running = False
_bot_thread = None

# ─────────────────────────────────────────
# MAIN BOT LOOP (SAFE)
# ─────────────────────────────────────────
def _bot_loop():
    global _bot_running

    log.info("[BOT] Started safely")

    while _bot_running:
        try:
            # 👉 YOUR EXISTING LOGIC GOES HERE
            log.info("[BOT] Running cycle...")
            time.sleep(10)

        except Exception as e:
            log.error(f"[BOT ERROR] {e}", exc_info=True)
            time.sleep(5)

    log.info("[BOT] Stopped")

# ─────────────────────────────────────────
# PUBLIC FUNCTIONS
# ─────────────────────────────────────────
def run_bot():
    global _bot_running, _bot_thread

    if _bot_running:
        return

    _bot_running = True
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True)
    _bot_thread.start()

    while _bot_running:
        time.sleep(5)

def stop():
    global _bot_running
    _bot_running = False

def get_status():
    return {
        "running": _bot_running
    }
