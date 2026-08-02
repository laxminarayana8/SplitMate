import asyncio
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