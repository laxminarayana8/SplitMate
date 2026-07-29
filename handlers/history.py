from telegram import Update
from telegram.ext import ContextTypes

from database import get_history


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):

    expenses = get_history(update.effective_chat.id)

    if not expenses:
        await update.message.reply_text("📭 No expenses found.")
        return

    message = "📜 Expense History\n\n"

    total = 0

    for i, expense in enumerate(expenses, start=1):

        payer, amount, category, split_type, created_at = expense

        total += amount

        message += (
            f"{i}. {category}\n"
            f"💰 ₹{amount}\n"
            f"👤 {payer}\n"
            f"⚖️ {split_type}\n\n"
        )

    message += f"──────────────\n"
    message += f"Total Expenses: ₹{total}"

    await update.message.reply_text(message)