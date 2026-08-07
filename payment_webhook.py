import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import PlainTextResponse
import os

from config import RAZORPAY_WEBHOOK_SECRET
from database import grant_subscription, execute
from config import SUBSCRIPTION_DAYS, SUBSCRIPTION_PLAN_NAME

logger = logging.getLogger(__name__)


def _verify_signature(raw_body: bytes, signature: str) -> bool:
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.error("RAZORPAY_WEBHOOK_SECRET is not set -- refusing to process webhook")
        return False
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _already_processed(payment_id: str) -> bool:
    """Webhooks can be delivered more than once -- guard against granting the same payment twice."""
    row = execute(
        "SELECT 1 FROM subscriptions WHERE last_payment_charge_id=?",
        (payment_id,),
        fetch=True,
    )
    return bool(row)


def create_webhook_app(telegram_application) -> FastAPI:
    """
    Builds the FastAPI app that listens for Razorpay webhooks. Takes the
    already-built PTB `Application` so it can send the "you're unlocked"
    Telegram message the instant a payment clears.
    """
    app = FastAPI()

    VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "splitmate123")


    @app.get("/webhook")
    async def verify_webhook(request: Request):
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            return PlainTextResponse(challenge)

        raise HTTPException(status_code=403, detail="Verification failed")


    @app.post("/webhook")
    async def whatsapp_webhook(request: Request):
        body = await request.json()
        print("WhatsApp Webhook:", body)
        return {"status": "ok"}

    @app.post("/razorpay/webhook")
    async def razorpay_webhook(
        request: Request,
        x_razorpay_signature: str = Header(default=None),
    ):
        raw_body = await request.body()

        if not _verify_signature(raw_body, x_razorpay_signature):
            raise HTTPException(status_code=400, detail="invalid signature")

        event = json.loads(raw_body)

        if event.get("event") != "payment_link.paid":
            # Ignore every other event type (payment.failed, refund.*, etc.)
            return {"status": "ignored"}

        payload = event["payload"]
        notes = payload["payment_link"]["entity"].get("notes", {})
        payment_entity = payload["payment"]["entity"]
        payment_id = payment_entity["id"]

        telegram_user_id = notes.get("telegram_user_id")
        chat_id = notes.get("chat_id")

        if not telegram_user_id:
            logger.error("payment_link.paid webhook missing telegram_user_id in notes: %s", notes)
            return {"status": "ignored"}

        if _already_processed(payment_id):
            return {"status": "already_processed"}

        new_expiry = grant_subscription(
            telegram_user_id=int(telegram_user_id),
            days=SUBSCRIPTION_DAYS,
            charge_id=payment_id,
            plan=SUBSCRIPTION_PLAN_NAME,
        )

        try:
            await telegram_application.bot.send_message(
                chat_id=int(chat_id) if chat_id else int(telegram_user_id),
                text=(
                    "✅ *Payment received — you're now SplitMate Pro!*\n\n"
                    f"Your subscription is active until *{new_expiry}*.\n"
                    "Try /summary for the upgraded Monthly Summary."
                ),
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Payment recorded but failed to notify user %s on Telegram", telegram_user_id)

        return {"status": "ok"}

    return app
