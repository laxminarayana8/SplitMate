from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from database import execute
from utils import format_rupees

async def request_settlement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notifies debtors privately in their DMs about money they owe to the requesting user based on active member_debts."""
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id

    if update.callback_query:
        await update.callback_query.answer()
    elif update.message:
        try:
            await update.message.delete()
        except Exception:
            pass

    # Find the group id from the chat
    group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat.id,), fetch=True)
    if not group:
        if chat.type == "private":
            await context.bot.send_message(
                chat_id=user.id,
                text="❌ Please use the 'Request Settlement' button inside your specific group chat menu."
            )
            return
        await context.bot.send_message(chat.id, "❌ Group not found.")
        return

    group_id = group[0][0]

    # Map the user clicking the button to their member_id
    member = execute(
        "SELECT member_id, display_name FROM members WHERE group_id=? AND telegram_user_id=?",
        (group_id, user_id),
        fetch=True
    )

    if not member:
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"⚠️ @{user.username or user.first_name}, I couldn't link your Telegram account to a group member profile."
        )
        return

    current_member_id, creditor_name = member[0][0], member[0][1]
    requestor_tg_id = user_id

    # Fetch active group members and their telegram IDs
    members = execute(
        "SELECT member_id, display_name, telegram_user_id FROM members WHERE group_id=? AND is_active=1",
        (group_id,),
        fetch=True
    )
    member_tg_ids = {m[0]: m[2] for m in members}
    member_names = {m[0]: m[1] for m in members}

    # ----------------------------------------
    # Read active debts directly from member_debts
    # ----------------------------------------
    user_debtors = execute(
        """
        SELECT
            debtor_member_id,
            amount
        FROM member_debts
        WHERE creditor_member_id = ?
          AND group_id = ?
          AND amount > 0
        """,
        (current_member_id, group_id),
        fetch=True,
    )

    if not user_debtors:
        await context.bot.send_message(
            chat_id=user.id if chat.type == "private" else chat.id,
            text="🎉 **All clear!** No one currently owes you any money in this group.",
            parse_mode="Markdown"
        )
        return

    notified_count = 0
    failed_names = []

    # Loop through each true debtor and send them a private DM reminder
    for debtor_id, amt in user_debtors:
        debtor_telegram_id = member_tg_ids.get(debtor_id)
        debtor_name = member_names.get(debtor_id, "User")

        if not debtor_telegram_id:
            failed_names.append(debtor_name)
            continue

        private_text = (
            f"📢 **Payment Request**\n\n"
            f"Hi **{debtor_name}**, **{creditor_name}** has requested a settlement for pending dues.\n\n"
            f"• You owe **{creditor_name}**: ₹{format_rupees(amt)}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Noted", callback_data=f"req_resp:noted:{requestor_tg_id}:{debtor_name}"),
                InlineKeyboardButton("❌ Decline", callback_data=f"req_resp:decline:{requestor_tg_id}:{debtor_name}")
            ]
        ])

        try:
            await context.bot.send_message(
                chat_id=debtor_telegram_id,
                text=private_text,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            notified_count += 1
        except Exception:
            failed_names.append(debtor_name)

    # Confirm action in the group chat
    if chat.type != "private":
        success_msg = f"📬 Settlement requests sent privately to {notified_count} debtor(s)!"
        if failed_names:
            success_msg += f"\n⚠️ Could not DM: {', '.join(failed_names)} (they need to start a chat with the bot first)."
        
        notification = await context.bot.send_message(chat_id=chat.id, text=success_msg)
        from utils import auto_delete, DELETE_AFTER_NOTICE
        auto_delete(context, chat.id, notification.message_id, delay=DELETE_AFTER_NOTICE)


async def settlement_response_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles when a debtor clicks 'Noted' or 'Decline', forwarding the response back to the requestor."""
    query = update.callback_query
    await query.answer()

    data_parts = query.data.split(":")
    if len(data_parts) < 4:
        return

    action = data_parts[1]
    requestor_tg_id = int(data_parts[2])
    debtor_name = data_parts[3]

    if action == "noted":
        response_label = "✅ You marked this as Noted."
        requestor_msg = f"📬 **Acknowledgement:** **{debtor_name}** has **Accepted ✅** your settlement request."
    else:
        response_label = "❌ You declined this request."
        requestor_msg = f"⚠️ **Notice:** **{debtor_name}** has **Declined ❌** your settlement request."

    try:
        await query.edit_message_text(
            text=f"{query.message.text}\n\n*{response_label}*",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    try:
        await context.bot.send_message(
            chat_id=requestor_tg_id,
            text=requestor_msg,
            parse_mode="Markdown"
        )
    except Exception:
        pass