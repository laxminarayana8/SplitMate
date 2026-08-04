from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes
from database import execute
from keyboards.menus import main_menu, history_filter_inline_menu
from utils import auto_delete, format_rupees, DELETE_AFTER_NOTICE


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: Triggered when user clicks '📜 History' in the group. Prompts via private chat or instructs user."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if update.message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass

    # Resolve the group this History request came FROM, so the private-chat
    # callback below queries the right group instead of guessing.
    group = execute(
        "SELECT group_id FROM groups WHERE telegram_chat_id=?",
        (chat_id,),
        fetch=True,
    )
    if not group:
        await context.bot.send_message(chat_id=chat_id, text="❌ Group not found. Please send /start in this group first.")
        return

    context.user_data["history_group_id"] = group[0][0]

    # Prompt the user privately or send an ephemeral message pointing them to PM
    try:
        msg = await context.bot.send_message(
            chat_id=user_id,
            text="📜 **Select a history view:**",
            reply_markup=history_filter_inline_menu,
            parse_mode="Markdown"
        )
        context.user_data["history_msg_id"] = msg.message_id
        
        # Notify in group briefly or let them know
        notif = await context.bot.send_message(
            chat_id=chat_id,
            text=f"✉️ **{update.effective_user.first_name}**, I've sent your history options to your private chat!",
            parse_mode="Markdown"
        )
        auto_delete(context, chat_id, notif.message_id, delay=DELETE_AFTER_NOTICE)
    except Exception:
        # Fallback if bot cannot DM the user yet (e.g. user hasn't started the bot privately)
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ I couldn't send you a private message. Please start a private chat with me first by clicking on my profile.",
            parse_mode="Markdown"
        )


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user's choice on history filtering and displays results in private chat."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = query.message.chat.id  # This is now the user's private chat id
    user_id = query.from_user.id

    if data == "cancel_history":
        await query.edit_message_text("❌ History view cancelled.")
        return

    # Use the group the user actually opened History from, remembered in history().
    # Falls back to "first active group" only if that context is missing
    # (e.g. bot restarted between opening the menu and tapping a filter).
    group_id = context.user_data.get("history_group_id")

    if group_id:
        member = execute(
            "SELECT member_id, display_name FROM members WHERE group_id=? AND telegram_user_id=?",
            (group_id, user_id),
            fetch=True
        )
        if not member:
            await query.edit_message_text("❌ You are not registered in this group.")
            return
    else:
        member_groups = execute(
            "SELECT g.group_id, g.telegram_chat_id FROM members m JOIN groups g ON m.group_id = g.group_id WHERE m.telegram_user_id = ? AND m.is_active = 1 AND g.telegram_chat_id < 0",
            (user_id,),
            fetch=True
        )

        if not member_groups:
            await query.edit_message_text("❌ You are not registered in any active groups.")
            return

        # If user is in multiple groups, we pick the first one or handle it. Assuming single-group context or taking the first:
        group_id, group_chat_id = member_groups[0]

        # Get user member info
        member = execute("SELECT member_id, display_name FROM members WHERE group_id=? AND telegram_user_id=?", (group_id, user_id), fetch=True)
        if not member:
            await query.edit_message_text("❌ You are not registered in this group.")
            return
    current_member_id, user_display_name = member[0]

    # Base query for expenses excluding settlements (e.g., category != 'Settlement')
    base_query = """
        SELECT
            e.expense_id,
            m.display_name,
            e.amount,
            e.category,
            e.description,
            e.split_type,
            e.created_at
        FROM expenses e
        JOIN members m
            ON e.payer_member_id = m.member_id
        WHERE e.group_id = ? AND e.category != 'Settlement' AND e.status != 'declined'
    """
    params = [group_id]
    filter_title = ""

    if data == "hist:last10":
        filter_title = "Last 10 Transactions"
        base_query += " ORDER BY e.expense_id DESC LIMIT 10"

    elif data == "hist:user":
        filter_title = f"Personal History ({user_display_name})"
        base_query += " AND e.payer_member_id = ? ORDER BY e.expense_id DESC"
        params.append(current_member_id)

    elif data == "hist:month":
        filter_title = "Last 1 Month Transactions"
        one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        base_query += " AND e.created_at >= ? ORDER BY e.expense_id DESC"
        params.append(one_month_ago)

    elif data == "hist:all":
        filter_title = "Whole Group History"
        base_query += " ORDER BY e.expense_id DESC"

    expenses = execute(base_query, tuple(params), fetch=True)

    if not expenses:
        await query.edit_message_text(
            f"📂 **History View — {filter_title}**\n\n📭 No expense transactions found matching this criteria.",
            parse_mode="Markdown"
        )
        return

    # Update initial selection message to reflect chosen filter
    await query.edit_message_text(f"📂 **History View — {filter_title}:**", parse_mode="Markdown")

    total = 0

    # Send each expense as an individual structured message block to private chat
    for expense in expenses:
        expense_id, payer, amount, category, description, split_type, created_at = expense
        total += amount

        desc_text = f"📝 Description: {description}\n" if description else ""

        message_text = (
            f"🆔 **ID:** #{expense_id}\n"
            f"📂 **Category:** {category}\n"
            f"{desc_text}"
            f"💰 **Amount:** ₹{format_rupees(amount)}\n"
            f"👤 **Paid by:** {payer}\n"
            f"⚖️ **Split:** {split_type}\n"
            f"📅 **Date:** {created_at}"
        )

        await context.bot.send_message(chat_id=chat_id, text=message_text, parse_mode="Markdown")

    # Send total sum at the end of private chat output
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"💵 **Total ({filter_title}):** ₹{format_rupees(total)}",
        parse_mode="Markdown"
    )