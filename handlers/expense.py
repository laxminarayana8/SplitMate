from database import save_expense
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from keyboards.menus import category_menu, split_menu, main_menu
from states import AMOUNT, CATEGORY, SPLIT


async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 Enter the expense amount (Example: 850)"
    )
    return AMOUNT


async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        amount = float(update.message.text)

    except ValueError:

        await update.message.reply_text(
            "❌ Please enter numbers only."
        )

        return AMOUNT

    context.user_data["amount"] = amount

    await update.message.reply_text(
        "📂 Choose Category",
        reply_markup=category_menu
    )

    return CATEGORY


async def category_received(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["category"] = update.message.text

    await update.message.reply_text(
        "⚖️ Choose how to split this expense",
        reply_markup=split_menu
    )

    return SPLIT
async def split_received(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["split"] = update.message.text
    
    save_expense(
    chat_id=update.effective_chat.id,
    payer_id=update.effective_user.id,
    payer_name=update.effective_user.first_name,
    amount=context.user_data["amount"],
    category=context.user_data["category"],
    split_type=context.user_data["split"],
)

    await update.message.reply_text(
    f"""✅ Expense captured!

💰 Amount: ₹{context.user_data['amount']}
📂 Category: {context.user_data['category']}
⚖️ Split: {context.user_data['split']}

Expense saved successfully.
""",
    reply_markup=main_menu
)

    return ConversationHandler.END