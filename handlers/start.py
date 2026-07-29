from telegram import Update
from telegram.ext import ContextTypes

from keyboards.menus import main_menu


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 Welcome to SplitMate!\n\n"
        "Your private roommate expense manager.\n\n"
        "Choose an option below.",
        reply_markup=main_menu
    )