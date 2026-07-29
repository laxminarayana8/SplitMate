from telegram import Update
from telegram.ext import ContextTypes

from database import get_balances


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    data = get_balances(update.effective_chat.id)

    if not data:
        await update.message.reply_text("No expenses found.")
        return

    if len(data) < 2:
        await update.message.reply_text(
            "At least two people must add expenses before balance can be calculated."
        )
        return

    total = sum(amount for _, amount in data)
    share = total / len(data)

    message = "💰 Balance\n\n"

    differences = []

    for name, amount in data:
        message += f"👤 {name}\n"
        message += f"Paid: ₹{amount:.2f}\n\n"

        differences.append((name, amount - share))

    message += "────────────────\n"
    message += f"Total Expenses: ₹{total:.2f}\n"
    message += f"Each Person Share: ₹{share:.2f}\n\n"

    creditor = max(differences, key=lambda x: x[1])
    debtor = min(differences, key=lambda x: x[1])

    if creditor[1] > 0:
        message += (
            f"🟢 {debtor[0]} owes {creditor[0]} "
            f"₹{abs(debtor[1]):.2f}"
        )
    else:
        message += "Everyone is settled up! 🎉"

    await update.message.reply_text(message)