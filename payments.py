"""
NOWPayments integration — USDT subscription payments.
Handles payment creation, webhook verification and subscription activation.
"""
import os
import json
import hmac
import hashlib
import logging
import requests
from datetime import datetime, timezone, timedelta

import user_manager

log = logging.getLogger(__name__)

NOWPAYMENTS_API_KEY    = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
NOWPAYMENTS_API        = "https://api.nowpayments.io/v1"

SUB_PRICE_USD  = 79.00
SUB_DAYS       = 30
PAY_CURRENCY   = "usdttrc20"   # USDT on TRC20 (lowest fees)


# ── Create payment ────────────────────────────────────────────

def create_payment(username: str, success_url: str,
                   cancel_url: str) -> dict:
    """
    Create a NOWPayments invoice for $79 USDT.
    Returns payment URL the user is redirected to.
    """
    if not NOWPAYMENTS_API_KEY:
        return {"ok": False, "error": "Payment system not configured"}

    payload = {
        "price_amount":   SUB_PRICE_USD,
        "price_currency": "usd",
        "pay_currency":   PAY_CURRENCY,
        "order_id":       f"apex_{username}_{int(datetime.now().timestamp())}",
        "order_description": f"Apex Bot — 30 day subscription for {username}",
        "ipn_callback_url": os.getenv("APP_URL", "") + "/webhook/nowpayments",
        "success_url":    success_url,
        "cancel_url":     cancel_url,
    }

    try:
        r = requests.post(
            f"{NOWPAYMENTS_API}/invoice",
            json=payload,
            headers={
                "x-api-key":   NOWPAYMENTS_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        data = r.json()

        if "invoice_url" in data:
            log.info(f"[PAY] Invoice created for {username}: {data.get('id')}")
            return {
                "ok":          True,
                "payment_url": data["invoice_url"],
                "payment_id":  data.get("id"),
                "amount":      data.get("pay_amount"),
                "currency":    data.get("pay_currency"),
            }
        else:
            log.error(f"[PAY] Invoice error: {data}")
            return {"ok": False, "error": data.get("message", "Payment creation failed")}

    except Exception as e:
        log.error(f"[PAY] Request failed: {e}")
        return {"ok": False, "error": str(e)}


# ── Webhook verification ──────────────────────────────────────

def verify_webhook(payload_bytes: bytes, sig_header: str) -> bool:
    """Verify NOWPayments IPN signature."""
    if not NOWPAYMENTS_IPN_SECRET:
        log.warning("[PAY] IPN secret not set — skipping verification")
        return True
    try:
        expected = hmac.new(
            NOWPAYMENTS_IPN_SECRET.encode(),
            payload_bytes,
            hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(expected, sig_header)
    except Exception as e:
        log.error(f"[PAY] Webhook verify error: {e}")
        return False


def handle_webhook(payload: dict) -> dict:
    """
    Process NOWPayments IPN callback.
    Activates subscription when payment is confirmed.
    """
    payment_status = payload.get("payment_status", "")
    order_id       = payload.get("order_id", "")

    log.info(f"[PAY] Webhook: {order_id} → {payment_status}")

    # Only activate on confirmed/finished
    if payment_status not in ("confirmed", "finished"):
        return {"ok": True, "action": "ignored", "status": payment_status}

    # Extract username from order_id (format: apex_{username}_{timestamp})
    parts = order_id.split("_")
    if len(parts) < 3 or parts[0] != "apex":
        log.warning(f"[PAY] Unknown order format: {order_id}")
        return {"ok": False, "error": "Unknown order format"}

    username = parts[1]

    # Verify amount paid is correct
    price_amount = float(payload.get("price_amount", 0))
    if price_amount < SUB_PRICE_USD * 0.99:  # 1% tolerance
        log.warning(f"[PAY] Underpayment for {username}: ${price_amount}")
        return {"ok": False, "error": f"Underpayment: ${price_amount}"}

    # Activate subscription
    result = user_manager.admin_extend_subscription(username, days=SUB_DAYS)
    if result["ok"]:
        log.info(f"[PAY] ✅ Subscription activated for {username} "
                 f"until {result['new_end'][:10]}")
        return {
            "ok":       True,
            "action":   "subscription_activated",
            "username": username,
            "new_end":  result["new_end"],
        }
    else:
        log.error(f"[PAY] Failed to activate for {username}: {result}")
        return {"ok": False, "error": result.get("error")}


# ── Check payment status ──────────────────────────────────────

def get_payment_status(payment_id: str) -> dict:
    """Poll a specific payment status."""
    if not NOWPAYMENTS_API_KEY:
        return {}
    try:
        r = requests.get(
            f"{NOWPAYMENTS_API}/payment/{payment_id}",
            headers={"x-api-key": NOWPAYMENTS_API_KEY},
            timeout=10,
        )
        return r.json()
    except Exception as e:
        log.error(f"[PAY] Status check failed: {e}")
        return {}
