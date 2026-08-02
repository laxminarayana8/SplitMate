import asyncio
import warnings
import uvicorn
from telegram.warnings import PTBUserWarning

# Suppress the PTB per_message warning for CallbackQueryHandlers in ConversationHandlers
warnings.filterwarnings("ignore", category=PTBUserWarning)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
)

from config import BOT_TOKEN, WEBHOOK_LISTEN_PORT
from database import create_tables
from keyboards.menus import main_menu
from utils import auto_delete, DELETE_AFTER_ACK
from payment_webhook import create_webhook_app

from handlers.start import start
from handlers.join import (
    bot_added_to_chat,
    new_chat_members_handler,
    confirm_join_callback,
)
from handlers.expense import (
    add_expense,
    amount_received,
    category_callback,
    description_received,
    description_skip_callback,
    split_callback,
    exact_amount_received,
    ratio_received,
    cancel_expense_callback,
    expense_action_callback,
    duplicate_confirmation_callback,
    equal_among_toggle_callback,
    equal_among_continue_callback,
)
from handlers.history import history, history_callback
from handlers.balance import balance
from handlers.edit import (
    start_edit_callback,
    field_selected_callback,
    save_edited_value,
    cancel_edit,
)
from handlers.settle import (
    settle_up_start,
    settle_select_callback,
    settle_up_choice_callback,
    partial_amount_received,
    settle_approval_callback,
    settle_back_callback,
    settle_cancel_callback,
)
from handlers.settings import (
    settings_menu,
    settings_callback,
    subgroup_toggle_callback,
    subgroup_save_callback,
    subgroup_reset_callback,
)
from handlers.summary import (
    monthly_summary_command,
    monthly_summary_month_callback,
)
from handlers.request_settlement import (
    request_settlement_command,
    settlement_response_callback,
)
from handlers.receipt import (
    receipt_received,
    receipt_group_selected_callback,
    receipt_confirm_callback,
)
from handlers.subscription import (
    subscribe_command,
    my_subscription_command,
    subscribe_button_callback,
)

from states import (
    AMOUNT,
    CATEGORY,
    DESCRIPTION,
    SPLIT,
    EXACT_AMOUNT,
    RATIO,
    SETTLE_MENU,
    PARTIAL_SETTLEMENT,
    EDIT_SELECT_FIELD,
    EDIT_NEW_VALUE,
    RECEIPT_GROUP_SELECT,
    RECEIPT_CONFIRM,
    EQUAL_AMONG_SELECT,
)


async def cancel(update, context):
    """Allows users to break out of any conversation and resets to the main menu."""
    context.user_data.clear()
    chat_id = update.effective_chat.id
    
    if update.message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass

    msg = await context.bot.send_message(chat_id=chat_id, text="❌ Action cancelled.", reply_markup=main_menu)
    auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_ACK)
    return ConversationHandler.END


async def reset_to_menu(update, context):
    """Fallback handler: Clears active state and shows the main menu when random text is typed."""
    context.user_data.clear()
    chat_id = update.effective_chat.id

    if update.message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="👋 Welcome back to SplitMate! Choose an option below:",
        reply_markup=main_menu,
    )
    auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_ACK)
    return ConversationHandler.END


def main():
    create_tables()

    app = Application.builder().token(BOT_TOKEN).build()

    # -------------------------
    # Command Handlers
    # -------------------------
    app.add_handler(CommandHandler("start", start))

    # -------------------------
    # Group-join invite flow
    # -------------------------
    app.add_handler(ChatMemberHandler(bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_members_handler))
    app.add_handler(CallbackQueryHandler(confirm_join_callback, pattern="^confirm_join:"))

    app.add_handler(CommandHandler("summary", monthly_summary_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("mysubscription", my_subscription_command))
    app.add_handler(CallbackQueryHandler(subscribe_button_callback, pattern="^subscribe:start$"))

    main_menu_filter = MessageHandler(filters.Regex("^🤝 Request Settlement$"), cancel)

    # -------------------------
    # Conversation Handler: Add Expense
    # -------------------------
    expense_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^➕ Add Expense$"),
                add_expense,
            )
        ],
        states={
            AMOUNT: [
                main_menu_filter,
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    amount_received,
                ),
            ],
            CATEGORY: [
                CallbackQueryHandler(category_callback, pattern="^cat:"),
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
            ],
            DESCRIPTION: [
                CallbackQueryHandler(description_skip_callback, pattern="^desc:skip$"),
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    description_received,
                ),
            ],
            SPLIT: [
                CallbackQueryHandler(split_callback, pattern="^split:"),
                CallbackQueryHandler(duplicate_confirmation_callback, pattern="^force_add:"),
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
            ],
            EQUAL_AMONG_SELECT: [
                CallbackQueryHandler(equal_among_toggle_callback, pattern="^eq_among_toggle:"),
                CallbackQueryHandler(equal_among_continue_callback, pattern="^eq_among_continue$"),
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
            ],
            EXACT_AMOUNT: [
                main_menu_filter,
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    exact_amount_received,
                ),
            ],
            RATIO: [
                main_menu_filter,
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    ratio_received,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
            CallbackQueryHandler(duplicate_confirmation_callback, pattern="^force_add:"),
            main_menu_filter,
        ],
        allow_reentry=True,
    )

    # -------------------------
    # Conversation Handler: Receipt Sharing (OCR -> add expense)
    # -------------------------
    # Entry point is any photo, whether forwarded into a registered group or
    # shared straight to the bot's private chat. From RECEIPT_CONFIRM onward
    # it feeds into the exact same category/description/split states (and
    # handler functions) as the manual "Add Expense" flow above, so there's
    # one code path for actually saving an expense.
    receipt_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, receipt_received)],
        states={
            RECEIPT_GROUP_SELECT: [
                CallbackQueryHandler(receipt_group_selected_callback, pattern="^receipt_group:"),
                CallbackQueryHandler(receipt_group_selected_callback, pattern="^receipt:cancel$"),
            ],
            RECEIPT_CONFIRM: [
                CallbackQueryHandler(receipt_confirm_callback, pattern="^receipt:"),
            ],
            AMOUNT: [
                main_menu_filter,
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received),
            ],
            CATEGORY: [
                CallbackQueryHandler(category_callback, pattern="^cat:"),
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
            ],
            DESCRIPTION: [
                CallbackQueryHandler(description_skip_callback, pattern="^desc:skip$"),
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, description_received),
            ],
            SPLIT: [
                CallbackQueryHandler(split_callback, pattern="^split:"),
                CallbackQueryHandler(duplicate_confirmation_callback, pattern="^force_add:"),
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
            ],
            EQUAL_AMONG_SELECT: [
                CallbackQueryHandler(equal_among_toggle_callback, pattern="^eq_among_toggle:"),
                CallbackQueryHandler(equal_among_continue_callback, pattern="^eq_among_continue$"),
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
            ],
            EXACT_AMOUNT: [
                main_menu_filter,
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, exact_amount_received),
            ],
            RATIO: [
                main_menu_filter,
                CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ratio_received),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(cancel_expense_callback, pattern="^cancel_expense$"),
            CallbackQueryHandler(duplicate_confirmation_callback, pattern="^force_add:"),
            main_menu_filter,
        ],
        allow_reentry=True,
    )

    # -------------------------
    # Conversation Handler: Edit Expense
    # -------------------------
    edit_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_edit_callback, pattern="^edit_exp:")
        ],
        states={
            EDIT_SELECT_FIELD: [
                CallbackQueryHandler(field_selected_callback, pattern="^field:")
            ],
            EDIT_NEW_VALUE: [
                MessageHandler(filters.Regex("^🤝 Request Settlement$"), cancel_edit),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    save_edited_value,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_edit),
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🤝 Request Settlement$"), cancel_edit),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=True,
    )

    # -------------------------
    # Conversation Handler: Settle Up
    # -------------------------
    settle_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^🤝 Settle Up$"),
                settle_up_start,
            )
        ],
        states={
            SETTLE_MENU: [
                CallbackQueryHandler(settle_select_callback, pattern="^settle_select:"),
                CallbackQueryHandler(settle_up_choice_callback, pattern="^settle_(full|partial):"),
                CallbackQueryHandler(settle_back_callback, pattern="^settle_back$"),
                CallbackQueryHandler(settle_cancel_callback, pattern="^settle_cancel$"),
            ],
            PARTIAL_SETTLEMENT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    partial_amount_received,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(settle_cancel_callback, pattern="^settle_cancel$"),
        ],
        allow_reentry=True,
    )

    app.add_handler(expense_handler)
    app.add_handler(receipt_handler)
    app.add_handler(edit_conv_handler)
    app.add_handler(settle_handler)

    # -------------------------
    # Callback Query Handlers (Inline Buttons)
    # -------------------------
    app.add_handler(
        CallbackQueryHandler(expense_action_callback, pattern="^exp_(conf|edit|del):")
    )

    app.add_handler(CallbackQueryHandler(settle_approval_callback, pattern="^(settle_|approve_|decline_)"))
    
    app.add_handler(
        CallbackQueryHandler(settlement_response_callback, pattern="^req_resp:")
    )

    app.add_handler(
        CallbackQueryHandler(history_callback, pattern="^(hist:|cancel_history$)")
    )

    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^settings:"))
    app.add_handler(CallbackQueryHandler(subgroup_toggle_callback, pattern="^subgroup_toggle:"))
    app.add_handler(CallbackQueryHandler(subgroup_save_callback, pattern="^subgroup_save$"))
    app.add_handler(CallbackQueryHandler(subgroup_reset_callback, pattern="^subgroup_reset$"))
    app.add_handler(CallbackQueryHandler(monthly_summary_month_callback, pattern="^summary_month:"))
    app.add_handler(CallbackQueryHandler(monthly_summary_command, pattern="^summary_back$"))
    

    # -------------------------
    # Standalone Message Handlers
    # -------------------------
    app.add_handler(
        MessageHandler(
            filters.Regex("^📜 History$"),
            history,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Regex("^💰 Balance$"),
            balance,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Regex("^📊 Monthly Summary$"),
            monthly_summary_command,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Regex("^🤝 Request Settlement$"),
            request_settlement_command,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Regex("^⚙️ Settings$"),
            settings_menu,
        )
    )

    # -------------------------
    # Fallback & Catch-All Handlers
    # -------------------------
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.Regex("^(➕ Add Expense|📜 History|💰 Balance|📊 Monthly Summary|🤝 Settle Up|🤝 Request Settlement|⚙️ Settings)$"),
            reset_to_menu,
        )
    )

    print("🚀 SplitMate is running...")
    return app


async def run():
    application = main()

    webhook_app = create_webhook_app(application)
    uvicorn_config = uvicorn.Config(webhook_app, host="0.0.0.0", port=WEBHOOK_LISTEN_PORT, log_level="info")
    server = uvicorn.Server(uvicorn_config)

    async with application:
        await application.start()
        await application.updater.start_polling()
        print(f"🚀 SplitMate is polling Telegram, and listening for Razorpay webhooks on :{WEBHOOK_LISTEN_PORT}")

        try:
            # Runs until the process is killed (Ctrl+C / systemd stop).
            await server.serve()
        finally:
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    asyncio.run(run())