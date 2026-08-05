from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import execute
from utils import format_rupees
from states import EDIT_SELECT_FIELD, EDIT_NEW_VALUE
from keyboards.menus import main_menu, category_menu


async def start_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when user taps '✏️ Edit' in history."""
    query = update.callback_query
    await query.answer()

    expense_id = int(query.data.split(":")[1])
    context.user_data["edit_expense_id"] = expense_id

    # Fetch existing expense details
    expense = execute(
        "SELECT amount, category, split_type FROM expenses WHERE expense_id=?",
        (expense_id,),
        fetch=True,
    )

    if not expense:
        await query.edit_message_text("❌ Expense not found or already deleted.")
        return ConversationHandler.END

    amount, category, split_type = expense[0]

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Edit Amount", callback_data="field:amount")],
            [InlineKeyboardButton("📂 Edit Category", callback_data="field:category")],
            [InlineKeyboardButton("❌ Cancel", callback_data="field:cancel")],
        ]
    )

    await query.edit_message_text(
        f"✏️ **Editing Expense #{expense_id}**\n\n"
        f"Current Amount: ₹{format_rupees(amount)}\n"
        f"Current Category: {category}\n"
        f"Current Split: {split_type}\n\n"
        f"Select what you want to modify:",
        reply_markup=keyboard,
    )

    return EDIT_SELECT_FIELD


async def field_selected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles field selection (Amount vs Category)."""
    query = update.callback_query
    await query.answer()

    field = query.data.split(":")[1]

    if field == "cancel":
        await query.edit_message_text("❌ Editing cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["edit_field"] = field

    if field == "amount":
        await query.message.reply_text("💰 Enter the new total amount:")
        return EDIT_NEW_VALUE

    elif field == "category":
        await query.message.reply_text(
            "📂 Select new category:",
            reply_markup=category_menu,
        )
        return EDIT_NEW_VALUE


async def save_edited_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves the updated value to the database."""
    field = context.user_data.get("edit_field")
    expense_id = context.user_data.get("edit_expense_id")
    new_value_text = update.message.text

    expense_row = execute(
        "SELECT group_id, payer_member_id, category, description, split_type, status FROM expenses WHERE expense_id=?",
        (expense_id,),
        fetch=True,
    )
    if not expense_row:
        await update.message.reply_text("❌ Expense not found or already deleted.", reply_markup=main_menu)
        context.user_data.clear()
        return ConversationHandler.END

    group_id, payer_member_id, category, description, split_type, status = expense_row[0]
    # Editing a *declined* expense is how the payer is meant to fix and
    # resubmit it (see the "✏️ Edit Amount/Category" button on the decline
    # notice in handlers/expense.py::share_decline_callback). Previously
    # this function only ever updated the expenses/expense_shares rows --
    # it never flipped status back to 'active', never restored the
    # member_debts it had reversed on decline, and never re-sent the "your
    # share" DMs. Since history/balance/summary queries all filter out
    # status='declined', the edited expense just silently stayed invisible
    # forever with no error shown. was_declined below drives all three of
    # those follow-up steps.
    was_declined = status == "declined"

    if field == "amount":
        try:
            new_amount = float(new_value_text)
            if new_amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a valid numeric amount greater than 0.")
            return EDIT_NEW_VALUE

        # Update expense table
        execute(
            "UPDATE expenses SET amount=? WHERE expense_id=?",
            (new_amount, expense_id),
        )

        # Recalculate equal shares if split_type was Equal
        if split_type == "⚖️ Equal":
            shares = execute(
                "SELECT member_id FROM expense_shares WHERE expense_id=?",
                (expense_id,),
                fetch=True,
            )
            if shares:
                # Precise share, not pre-rounded to 2dp -- see the matching fix
                # in handlers/expense.py::execute_equal_split for why applying
                # the same 2dp-rounded value to every member causes drift.
                per_head = round(new_amount / len(shares), 6)
                execute(
                    "UPDATE expense_shares SET share_amount=? WHERE expense_id=?",
                    (per_head, expense_id),
                )

        reactivated = False
        if was_declined:
            reactivated = await _reactivate_declined_expense(
                context, expense_id, group_id, payer_member_id, new_amount, category, description, split_type
            )

        note = "\n\n🔄 This expense is active again -- everyone's been re-notified." if reactivated else ""
        await update.message.reply_text(
            f"✅ **Expense #{expense_id} Updated!**\nNew Amount: ₹{format_rupees(new_amount)}{note}",
            reply_markup=main_menu,
        )

    elif field == "category":
        execute(
            "UPDATE expenses SET category=? WHERE expense_id=?",
            (new_value_text, expense_id),
        )

        reactivated = False
        if was_declined:
            amount_row = execute("SELECT amount FROM expenses WHERE expense_id=?", (expense_id,), fetch=True)
            current_amount = amount_row[0][0] if amount_row else 0
            reactivated = await _reactivate_declined_expense(
                context, expense_id, group_id, payer_member_id, current_amount, new_value_text, description, split_type
            )

        note = "\n\n🔄 This expense is active again -- everyone's been re-notified." if reactivated else ""
        await update.message.reply_text(
            f"✅ **Expense #{expense_id} Updated!**\nNew Category: {new_value_text}{note}",
            reply_markup=main_menu,
        )

    context.user_data.clear()
    return ConversationHandler.END


async def _reactivate_declined_expense(
    context: ContextTypes.DEFAULT_TYPE,
    expense_id: int,
    group_id: int,
    payer_member_id: int,
    amount: float,
    category: str,
    description: str,
    split_type: str,
) -> bool:
    """
    Brings a previously-declined expense back to life after the payer edits
    it: flips status back to 'active' (so history/balance/summary pick it
    up again -- they all filter out 'declined'), re-applies its debt impact
    (declining had reversed it), and re-sends each participant their "your
    share" DM with a fresh Decline option. Returns False (no-op) if there's
    nothing to notify.
    """
    # Imported locally to avoid a circular import (handlers.expense already
    # imports nothing from handlers.edit, so this is one-directional, but
    # keeping it local mirrors how handlers/join.py does the same thing).
    from handlers.expense import update_member_debts, notify_split_participants

    shares = execute(
        "SELECT member_id, share_amount FROM expense_shares WHERE expense_id=?",
        (expense_id,),
        fetch=True,
    )
    if not shares:
        # Nothing to restore debts/notifications for -- still reactivate so
        # it's not stuck hidden.
        execute("UPDATE expenses SET status='active' WHERE expense_id=?", (expense_id,))
        return False

    execute("UPDATE expenses SET status='active' WHERE expense_id=?", (expense_id,))

    debt_payload = [(member_id, share_amount) for member_id, share_amount in shares]
    update_member_debts(group_id, expense_id, payer_member_id, debt_payload)
    await notify_split_participants(
        context, group_id, payer_member_id, amount, category, description,
        split_type, debt_payload, expense_id=expense_id,
    )
    return True


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Edit cancelled.", reply_markup=main_menu)
    return ConversationHandler.END