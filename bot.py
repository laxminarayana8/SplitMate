from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from config import BOT_TOKEN
from database import create_tables

from handlers.start import start
from handlers.history import history
from handlers.balance import balance
from handlers.expense import (
    add_expense,
    amount_received,
    category_received,
    split_received,
)

from states import AMOUNT, CATEGORY, SPLIT


def main():

    create_tables()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    expense_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Add Expense$"), add_expense)
        ],
        states={
            AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)
            ],
            CATEGORY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, category_received)
            ],
            SPLIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, split_received)
            ],
        },
        fallbacks=[],
    )

    app.add_handler(expense_handler)
    app.add_handler(
    MessageHandler(
        filters.Regex("^📜 History$"),
        history
    )
)

    app.add_handler(
    MessageHandler(
        filters.Regex("^💰 Balance$"),
        balance
    )
)

    print("SplitMate is running...")
    app.run_polling()


if __name__ == "__main__":
    main()