from telegram import Update
from telegram.ext import ContextTypes
from database import execute
from keyboards.menus import main_menu
from utils import auto_delete, format_rupees
from services.currency import convert_amount
import json

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass

    group = execute(
        "SELECT group_id FROM groups WHERE telegram_chat_id=?",
        (chat_id,),
        fetch=True
    )
    if not group:
        await context.bot.send_message(chat_id=chat_id, text="❌ Group not found.")
        return
    group_id = group[0][0]

    user_member = execute(
        "SELECT member_id FROM members WHERE group_id=? AND telegram_user_id=?",
        (group_id, user_id),
        fetch=True
    )
    if not user_member:
        await context.bot.send_message(chat_id=chat_id, text="❌ You are not registered in this group yet. Send /start first.")
        return
    current_member_id = user_member[0][0]

    # Fetch user's preferred currency (defaults to INR)
    user_curr_res = execute(
        "SELECT default_currency FROM user_settings WHERE user_id=?",
        (user_id,),
        fetch=True
    )
    user_currency = user_curr_res[0][0] if user_curr_res else "INR"

    members = execute(
        "SELECT member_id, display_name, telegram_user_id FROM members WHERE group_id=? AND is_active=1",
        (group_id,),
        fetch=True
    )
    if not members:
        await context.bot.send_message(chat_id=chat_id, text="No members found.")
        return

    member_names = {m[0]: m[1] for m in members}
    member_telegram_map = {m[0]: m[2] for m in members}

    # --- Calculate Total Expenditure for the Current User (converted to user currency) ---
    expenses = execute(
        """
        SELECT amount, currency, exchange_rates_snapshot 
        FROM expenses 
        WHERE group_id = ? AND payer_member_id = ? AND status != 'declined'
        """,
        (group_id, current_member_id),
        fetch=True
    )
    
    total_expenditure = 0.0
    if expenses:
        for amt, curr, snapshot in expenses:
            total_expenditure += convert_amount(amt, curr or "INR", user_currency, snapshot)

    # -----------------------------
    # Read current debts directly from member_debts with multi-currency conversion
    # -----------------------------
    debts = execute(
        """
        SELECT
            debtor_member_id,
            creditor_member_id,
            amount
        FROM member_debts md
        JOIN members d ON md.debtor_member_id = d.member_id
        JOIN members c ON md.creditor_member_id = c.member_id
        WHERE d.group_id = ? AND c.group_id = ? AND md.amount > 0
        """,
        (group_id, group_id),
        fetch=True,
    )

    total_owed = 0.0
    total_get_back = 0.0

    # Helper function to get preferred currency for any member by telegram_user_id
    def get_member_currency(m_id):
        t_id = member_telegram_map.get(m_id)
        if not t_id:
            return user_currency
        res = execute("SELECT default_currency FROM user_settings WHERE user_id=?", (t_id,), fetch=True)
        return res[0][0] if res else "INR"

    processed_debts = []
    for debtor_id, creditor_id, raw_amount in debts:
        # Assuming base debts are tracked in INR or default group currency, convert them dynamically if needed
        # Or if raw_amount matches standard base, convert from base to user_currency
        converted_amt = convert_amount(raw_amount, "INR", user_currency)
        processed_debts.append((debtor_id, creditor_id, converted_amt))

        if debtor_id == current_member_id:
            total_owed += converted_amt
        elif creditor_id == current_member_id:
            total_get_back += converted_amt

    user_name = member_names.get(current_member_id, "You")
    message = f"📊 **Your Balance Summary ({user_name}) [{user_currency}]:**\n\n"
    message += f"• 💳 **Total Expenditure:** `{user_currency} {format_rupees(total_expenditure)}`\n"

    if total_get_back > 0:
        message += (
            f"• **Overall Standing:** "
            f"You get back a total of `{user_currency} {format_rupees(total_get_back)}`\n\n"
        )
    elif total_owed > 0:
        message += (
            f"• **Overall Standing:** "
            f"You owe a total of `{user_currency} {format_rupees(total_owed)}`\n\n"
        )
    else:
        message += "• **Overall Standing:** You are fully settled up!\n\n"

    message += "🔄 **Direct Actions for You:**\n"
    has_actions = False

    for debtor_id, creditor_id, amount in processed_debts:
        if debtor_id == current_member_id:
            has_actions = True
            creditor_name = member_names.get(creditor_id, "Someone")
            message += (
                f"• 💸 You have to pay **{creditor_name}**: "
                f"`{user_currency} {format_rupees(amount)}`\n"
            )
        elif creditor_id == current_member_id:
            has_actions = True
            debtor_name = member_names.get(debtor_id, "Someone")
            message += (
                f"• 💰 **{debtor_name}** owes you: "
                f"`{user_currency} {format_rupees(amount)}`\n"
            )

    if not has_actions:
        message += "• No pending actions with anyone right now.\n"

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=message,
        parse_mode="Markdown",
        reply_markup=main_menu,
    )
    auto_delete(context, chat_id, msg.message_id, delay=120)