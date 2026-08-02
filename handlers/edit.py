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

    if field == "amount":
        try:
            new_amount = float(new_value_text)
        except ValueError:
            await update.message.reply_text("❌ Enter a valid numeric amount.")
            return EDIT_NEW_VALUE

        # Update expense table
        execute(
            "UPDATE expenses SET amount=? WHERE expense_id=?",
            (new_amount, expense_id),
        )

        # Recalculate equal shares if split_type was Equal
        split = execute(
            "SELECT split_type FROM expenses WHERE expense_id=?",
            (expense_id,),
            fetch=True,
        )
        if split and split[0][0] == "⚖️ Equal":
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

        await update.message.reply_text(
            f"✅ **Expense #{expense_id} Updated!**\nNew Amount: ₹{format_rupees(new_amount)}",
            reply_markup=main_menu,
        )

    elif field == "category":
        execute(
            "UPDATE expenses SET category=? WHERE expense_id=?",
            (new_value_text, expense_id),
        )
        await update.message.reply_text(
            f"✅ **Expense #{expense_id} Updated!**\nNew Category: {new_value_text}",
            reply_markup=main_menu,
        )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Edit cancelled.", reply_markup=main_menu)
    return ConversationHandler.END