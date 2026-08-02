from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from datetime import datetime
from database import get_monthly_summary_data, execute
from utils import auto_delete, format_rupees

MONTH_LOOKBACK = 6  # how many months (including the current one) to offer in the picker


def _past_months(n: int):
    """Returns the current month plus the previous (n-1) months as (year, month) tuples, most recent first."""
    now = datetime.now()
    year, month = now.year, now.month
    months = []
    for _ in range(n):
        months.append((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return months


def _month_picker_keyboard():
    keyboard = [
        [InlineKeyboardButton(
            datetime(year, month, 1).strftime("%B %Y"),
            callback_data=f"summary_month:{year}:{month}",
        )]
        for year, month in _past_months(MONTH_LOOKBACK)
    ]
    return InlineKeyboardMarkup(keyboard)


async def monthly_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: shows a picker for the last 6 months instead of jumping straight to a report."""
    chat_id = update.effective_chat.id
    query = update.callback_query

    if query:
        await query.answer()
    elif update.message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass

    group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat_id,), fetch=True)
    if not group:
        text = "❌ Group not found."
        if query:
            await query.edit_message_text(text)
        else:
            await context.bot.send_message(chat_id, text)
        return

    text = "📊 **Monthly Summary**\n\nSelect a month to view:"
    keyboard = _month_picker_keyboard()

    if query:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="Markdown")


async def monthly_summary_month_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a month selection from the picker and renders that month's report (or an empty-month alert)."""
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        _, year_str, month_str = query.data.split(":")
        year, month = int(year_str), int(month_str)
    except (ValueError, IndexError):
        await query.answer("❌ Invalid selection.", show_alert=True)
        return

    group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat_id,), fetch=True)
    if not group:
        await query.answer("❌ Group not found.", show_alert=True)
        return
    group_id = group[0][0]

    month_name = datetime(year, month, 1).strftime("%B %Y")
    summary_data = get_monthly_summary_data(group_id, year, month)

    total_exp = summary_data["total_group_exp"]
    mom_change_pct = summary_data["mom_change_pct"]
    total_settled = summary_data["total_settlements"]
    settlement_count = summary_data["settlement_count"]
    paid_ranking = summary_data["paid_ranking"]
    top_3 = summary_data["top_paid"]
    least_paid = summary_data["least_paid"]
    category_breakdown = summary_data["category_breakdown"]
    transaction_count = summary_data["transaction_count"]
    avg_expense = summary_data["avg_expense"]
    biggest_expense = summary_data["biggest_expense"]
    debts = summary_data["debts"]  # current/live balances — NOT scoped to this month, see database.py

    # Nothing happened this month at all: pop up an alert and leave the picker as-is.
    if not total_exp and not total_settled and not top_3:
        await query.answer(f"📭 No transactions present for {month_name}.", show_alert=True)
        return

    await query.answer()

    # --- Month-over-month trend line ---
    if mom_change_pct is None:
        trend_str = " _(no prior month to compare)_"
    elif mom_change_pct > 0:
        trend_str = f" (↑ {mom_change_pct:.0f}% vs last month)"
    elif mom_change_pct < 0:
        trend_str = f" (↓ {abs(mom_change_pct):.0f}% vs last month)"
    else:
        trend_str = " (no change vs last month)"

    # --- Spender breakdown: medals for top 3, plain bullets for the rest ---
    top_str = ""
    if paid_ranking:
        medals = ["🥇", "🥈", "🥉"]
        for idx, (name, amt) in enumerate(paid_ranking, 1):
            marker = medals[idx - 1] if idx <= 3 else "•"
            top_str += f"{marker} **{name}**: ₹{format_rupees(amt)}\n"
    else:
        top_str = "• No expenses recorded this month.\n"

    least_str = f"• **{least_paid[0]}**: ₹{format_rupees(least_paid[1])}" if least_paid else "• N/A (Only one active spender)"

    # --- Category breakdown ---
    category_str = ""
    if category_breakdown:
        for cat, amt in category_breakdown:
            pct = (amt / total_exp * 100) if total_exp else 0
            category_str += f"• {cat}: ₹{format_rupees(amt)} ({pct:.0f}%)\n"
    else:
        category_str = "• No category data for this month.\n"

    # --- Biggest single expense ---
    if biggest_expense:
        big_amt, big_cat, big_desc, big_payer = biggest_expense
        desc_part = f" — {big_desc}" if big_desc else ""
        biggest_str = f"₹{format_rupees(big_amt)} · {big_cat}{desc_part} (paid by {big_payer})"
    else:
        biggest_str = "N/A"

    # --- Current live balances (NOT month-scoped — carries forward month to month) ---
    all_members = execute("SELECT display_name FROM members WHERE group_id=?", (group_id,), fetch=True)
    member_names = {m[0] for m in all_members}
    debtors_involved = {row[0] for row in debts}.union({row[1] for row in debts})
    settled_up_members = member_names - debtors_involved
    settled_up_str = ", ".join(settled_up_members) if settled_up_members else "None completely clear yet"

    debts_str = ""
    if debts:
        for debtor, creditor, amt in debts:
            debts_str += f"• **{debtor}** owes **{creditor}**: ₹{format_rupees(amt)}\n"
    else:
        debts_str = "🎉 All balances are completely cleared out!"

    suggestions = []
    if settled_up_members and settled_up_members != member_names:
        cleared_names = ", ".join(settled_up_members)
        suggestions.append(f"💡 *Kudos:* **{cleared_names}** has completely cleared all accounts!")
    elif not debts:
        suggestions.append("💡 *Kudos:* All group accounts are completely settled up!")
    else:
        suggestions.append("💡 *Action Item:* Prompt members with outstanding balances to clear pending dues.")
    suggestions_text = "\n".join(suggestions)

    report = (
        f"📊 **Monthly Financial Summary ({month_name})**\n\n"
        f"💵 **Total Group Expenditure:** ₹{format_rupees(total_exp)}{trend_str}\n"
        f"🧾 **Transactions:** {transaction_count} · Avg ₹{format_rupees(avg_expense)} each\n"
        f"🔥 **Biggest Expense:** {biggest_str}\n"
        f"🤝 **Settlements Executed:** ₹{format_rupees(total_settled)} ({settlement_count} settlement(s))\n\n"
        f"🗂️ **Category Breakdown:**\n{category_str}\n"
        f"🏆 **Spender Ranking (Paid):**\n{top_str}\n"
        f"📉 **Lowest Spender:**\n{least_str}\n\n"
        f"📋 **Outstanding Balances** _(current, as of today — not limited to {month_name})_:\n{debts_str}\n\n"
        f"✨ **Fully Settled Up** _(current)_:\n👤 {settled_up_str}\n\n"
        f"---\n"
        f"🤖 **Smart Suggestions:**\n{suggestions_text}"
    )

    back_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Month Selection", callback_data="summary_back")]]
    )

    await query.edit_message_text(text=report, reply_markup=back_keyboard, parse_mode="Markdown")
    auto_delete(context, chat_id, query.message.message_id, delay=600)