import asyncio
import time
import httpx
from telegram.ext import ContextTypes

# -----------------------------
# Auto-delete dwell times (seconds)
# -----------------------------
# How long a transient message stays on screen before it's deleted. Tuned
# by what the message actually asks of the reader -- a one-word "cancelled"
# needs far less time on screen than a validation error they have to read
# and then go fix. Telegram gives us no fade/transition to soften the
# disappearance itself (see auto_delete below), so this is the one lever
# we have: give people enough time to register something before it's gone.
DELETE_AFTER_ACK = 5            # brief acknowledgements: "cancelled", "closed"
DELETE_AFTER_ERROR = 7          # validation errors -- needs reading + time to retype
DELETE_AFTER_NOTICE = 8         # background notices: "settlement request sent", filter picked
DELETE_AFTER_CONFIRMATION = 18  # meaningful confirmations: transaction saved, welcome card


def format_rupees(amount) -> str:
    """
    Rounds an amount to the nearest whole rupee for DISPLAY only.

    Internal accounting (expense_shares, member_debts) intentionally keeps
    full decimal precision so repeated equal-splits never drift -- this
    helper is only for what gets shown to the user, everywhere a ₹ amount
    is rendered in a message.
    """
    try:
        return f"{round(float(amount)):,}"
    except (TypeError, ValueError):
        return "0"


async def _delete_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
    """Waits for the specified delay and deletes the message."""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # Message might already be deleted or missing permissions


def auto_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int = 4):
    """Schedules a message deletion as a background task."""
    context.application.create_task(_delete_later(context, chat_id, message_id, delay))


def require_subscription(handler):
    """
    Decorator: wraps a handler so it only runs for users with an active Pro
    subscription. Non-subscribers get an upsell message with a 'Go Pro'
    button instead of the feature running. Add/remove this decorator on any
    handler in bot.py's imports to change what's free vs. paid.

    Usage:
        @require_subscription
        async def some_premium_handler(update, context):
            ...
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from database import is_subscribed

    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if is_subscribed(user_id):
            return await handler(update, context, *args, **kwargs)

        target = update.callback_query or update.message
        text = "⭐ This is a Pro feature. Subscribe to unlock it."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⭐ Go Pro", callback_data="subscribe:start")]]
        )
        if update.callback_query:
            await update.callback_query.answer()
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=keyboard)
        else:
            msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=keyboard)
            auto_delete(context, update.effective_chat.id, msg.message_id, delay=6)
        return None

    wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    return wrapped


# -----------------------------
# CURRENCY
# -----------------------------
# Add an entry here to make a currency selectable in Settings -> Currency.
SUPPORTED_CURRENCIES = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "HKD": "HK$",
    "SGD": "S$",
    "AED": "AED",
    "AUD": "A$",
}

# Exchange-rate lookups are cached for a short window purely to avoid
# hammering the API when several participants share the same currency pair
# on one expense -- this is NOT the "frozen at expense time" snapshot
# (that's stored per-share in the DB by save_share_conversion). Even a
# stale-by-a-few-minutes rate here only affects what gets written into that
# permanent snapshot at creation time; it never changes afterwards.
_FX_CACHE_TTL_SECONDS = 300
_fx_cache = {}


def format_amount(amount, currency: str) -> str:
    """Formats an amount with its currency symbol -- 0dp when it's a whole
    number, 2dp otherwise (mirrors format_rupees, generalized to any
    supported currency)."""
    symbol = SUPPORTED_CURRENCIES.get(currency, currency + " ")
    try:
        rounded = round(float(amount), 2)
    except (TypeError, ValueError):
        return f"{symbol}0"
    if rounded == int(rounded):
        return f"{symbol}{int(rounded):,}"
    return f"{symbol}{rounded:,.2f}"


async def get_exchange_rate(base_currency: str, target_currency: str):
    """
    Returns how many units of target_currency equal 1 unit of
    base_currency, or None if the rate couldn't be fetched (caller should
    skip showing a conversion rather than guess).

    Uses a free, no-key exchange-rate API. Callers are responsible for
    persisting whatever rate they get back (see save_share_conversion) --
    this function itself has no memory of "the rate an expense used", only
    a short cache to cut down on repeat calls within the same burst of
    notifications.
    """
    if base_currency == target_currency:
        return 1.0

    cache_key = (base_currency, target_currency)
    cached = _fx_cache.get(cache_key)
    if cached and (time.monotonic() - cached[1]) < _FX_CACHE_TTL_SECONDS:
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"https://api.exchangerate-api.com/v4/latest/{base_currency}")
            resp.raise_for_status()
            data = resp.json()
            rate = data.get("rates", {}).get(target_currency)
    except Exception:
        rate = None

    if rate is not None:
        _fx_cache[cache_key] = (rate, time.monotonic())
    return rate