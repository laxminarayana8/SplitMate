import httpx
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import (
    RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET,
    SUBSCRIPTION_PRICE_INR,
    SUBSCRIPTION_DAYS,
)
from database import is_subscribed, get_subscription_expiry

logger = logging.getLogger(__name__)

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


async def _create_payment_link(telegram_user_id: int, chat_id: int, bot_username: str) -> str:
    """
    Creates a Razorpay Payment Link for one subscription purchase and returns
    its short_url. The telegram_user_id/chat_id are stashed in `notes` --
    Razorpay echoes `notes` back in the webhook payload, which is how the
    webhook handler later knows which Telegram user to unlock.
    """
    payload = {
        "amount": int(round(SUBSCRIPTION_PRICE_INR * 100)),  # Razorpay wants paise, not rupees
        "currency": "INR",
        "description": f"SplitMate Pro - {SUBSCRIPTION_DAYS} days",
        "notes": {
            "telegram_user_id": str(telegram_user_id),
            "chat_id": str(chat_id),
        },
        "callback_url": f"https://t.me/{bot_username}",
        "callback_method": "get",
    }

    async with httpx.AsyncClient(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=15) as client:
        resp = await client.post(f"{RAZORPAY_API_BASE}/payment_links", json=payload)
        resp.raise_for_status()
        return resp.json()["short_url"]


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /subscribe (and the 'Go Pro' button). Generates a fresh Razorpay payment link."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if is_subscribed(user_id):
        expiry = get_subscription_expiry(user_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⭐ You're already a Pro subscriber until *{expiry}*.\nUse /mysubscription to check your status.",
            parse_mode="Markdown",
        )
        return

    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Payments aren't configured yet on this bot. Ask the bot admin to set up Razorpay.",
        )
        logger.warning("subscribe_command called but RAZORPAY_KEY_ID/SECRET are not set")
        return

    try:
        bot_username = context.bot.username
        pay_url = await _create_payment_link(user_id, chat_id, bot_username)
    except Exception:
        logger.exception("Failed to create Razorpay payment link")
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Couldn't create a payment link right now. Please try again in a moment.",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"⭐ *SplitMate Pro* — ₹{SUBSCRIPTION_PRICE_INR} for {SUBSCRIPTION_DAYS} days\n\n"
            "Tap below to pay by UPI, card, or netbanking. Your subscription unlocks automatically "
            "within a few seconds of payment -- no need to send a screenshot."
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("💳 Pay Now", url=pay_url)]]
        ),
    )


async def my_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mysubscription: lets a user check their current Pro status without triggering a new payment link."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if is_subscribed(user_id):
        expiry = get_subscription_expiry(user_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⭐ *SplitMate Pro active* until *{expiry}*.",
            parse_mode="Markdown",
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="You're on the free plan. Send /subscribe to unlock Pro features.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⭐ Go Pro", callback_data="subscribe:start")]]
            ),
        )


async def subscribe_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the inline 'Go Pro' button (same effect as /subscribe)."""
    query = update.callback_query
    await query.answer()
    await subscribe_command(update, context)
