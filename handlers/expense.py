from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import execute, now_ist, get_user_subgroup_member_ids
from keyboards.menus import (
    category_inline_menu,
    description_inline_menu,
    main_menu,
    split_inline_menu,
    get_equal_among_menu,
)
from states import (
    AMOUNT,
    CATEGORY,
    DESCRIPTION,
    EXACT_AMOUNT,
    RATIO,
    SPLIT,
    EQUAL_AMONG_SELECT,
)
from utils import auto_delete, DELETE_AFTER_ACK, DELETE_AFTER_ERROR, DELETE_AFTER_CONFIRMATION


async def safe_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Safely deletes a message without raising exceptions if already deleted."""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def equal_split_display_shares(amount: float, member_count: int) -> list:
    """
    Whole-RUPEE breakdown of `amount` across `member_count` people, for showing
    the user a clean "who pays what note" figure on THIS transaction only.

    Uses floor + remainder at the whole-rupee level (not paisa) so the
    displayed shares are always plain integers summing to round(amount)
    (e.g. ₹1000 / 3 -> [334, 333, 333], never 333.33 or 333.34). The first
    `remainder` members (by list order) receive the extra rupee.

    IMPORTANT: this is a display-only convenience. It is never written to the
    database and never used for debt/balance accounting -- see
    execute_equal_split, which stores the precise (unrounded) per-member share
    instead. If this rounded value were used for accounting, whoever
    consistently lands in the "gets the extra rupee" slot would be silently
    overcharged more and more with every repeated equal-split expense.
    """
    total_rupees = round(amount)
    base, remainder = divmod(total_rupees, member_count)
    return [base + 1 if i < remainder else base for i in range(member_count)]


def format_rupees(amount: float) -> str:
    """Formats a rupee amount without decimals when it's a whole number, else with 2dp."""
    rounded = round(amount, 2)
    if rounded == int(rounded):
        return f"{int(rounded)}"
    return f"{rounded:.2f}"


def update_member_debts(group_id: int, expense_id: int, payer_member_id: int, shares: list):
    """
    Updates the member_debts table based on the expense shares, linking the expense_id.
    shares: list of tuples/lists containing (member_id, share_amount)
    """
    for member_id, share_amount in shares:
        if member_id == payer_member_id or share_amount <= 0:
            continue
        
        existing = execute(
            """
            SELECT debt_id, amount FROM member_debts
            WHERE group_id = ? AND debtor_member_id = ? AND creditor_member_id = ?
            """,
            (group_id, member_id, payer_member_id),
            fetch=True
        )
        
        if existing:
            debt_id, current_amount = existing[0]
            new_amount = current_amount + share_amount
            execute(
                "UPDATE member_debts SET amount = ? WHERE debt_id = ?",
                (new_amount, debt_id)
            )
        else:
            inverse = execute(
                """
                SELECT debt_id, amount FROM member_debts
                WHERE group_id = ? AND debtor_member_id = ? AND creditor_member_id = ?
                """,
                (group_id, payer_member_id, member_id),
                fetch=True
            )
            if inverse:
                inv_id, inv_amount = inverse[0]
                if inv_amount > share_amount:
                    execute(
                        "UPDATE member_debts SET amount = ? WHERE debt_id = ?",
                        (inv_amount - share_amount, inv_id)
                    )
                elif inv_amount < share_amount:
                    execute("DELETE FROM member_debts WHERE debt_id = ?", (inv_id,))
                    execute(
                        """
                        INSERT INTO member_debts (group_id, expense_id, debtor_member_id, creditor_member_id, amount) 
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (group_id, expense_id, member_id, payer_member_id, share_amount - inv_amount)
                    )
                else:
                    execute("DELETE FROM member_debts WHERE debt_id = ?", (inv_id,))
            else:
                execute(
                    """
                    INSERT INTO member_debts (group_id, expense_id, debtor_member_id, creditor_member_id, amount) 
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (group_id, expense_id, member_id, payer_member_id, share_amount)
                )


async def notify_split_participants(
    context: ContextTypes.DEFAULT_TYPE,
    group_id: int,
    payer_member_id: int,
    amount: float,
    category: str,
    description: str,
    split_label: str,
    member_shares: list,
):
    """
    Privately DMs every participant (other than the payer) a summary of the
    expense and exactly their own share, so nobody has to open the group or
    scroll through history to know what they owe.

    member_shares: list of (member_id, share_amount) tuples, e.g. the same
    payload already built for update_member_debts() by each split-execution
    function -- reused as-is rather than recomputed.
    Silently skips anyone the bot can't DM (they haven't started a private
    chat with the bot yet); this mirrors how request_settlement.py already
    handles that same failure mode.
    """
    if not member_shares:
        return

    group_row = execute("SELECT group_name FROM groups WHERE group_id=?", (group_id,), fetch=True)
    group_name = group_row[0][0] if group_row else "your group"

    payer_row = execute("SELECT display_name FROM members WHERE member_id=?", (payer_member_id,), fetch=True)
    payer_name = payer_row[0][0] if payer_row else "Someone"

    member_ids = [m for m, _ in member_shares]
    placeholders = ",".join("?" * len(member_ids))
    rows = execute(
        f"SELECT member_id, telegram_user_id, display_name FROM members WHERE member_id IN ({placeholders})",
        tuple(member_ids),
        fetch=True,
    )
    info = {r[0]: (r[1], r[2]) for r in rows} if rows else {}

    desc_part = f"\n📝 {description}" if description and description != "-" else ""

    for member_id, share in member_shares:
        if member_id == payer_member_id or share <= 0:
            continue

        telegram_user_id, _name = info.get(member_id, (None, None))
        if not telegram_user_id:
            continue

        text = (
            f"🧾 **New Expense in {group_name}**\n\n"
            f"👤 Paid by: **{payer_name}**\n"
            f"📂 **Category:** {category}{desc_part}\n"
            f"💰 **Total:** ₹{amount:.2f}\n"
            f"⚖️ **Split:** {split_label}\n\n"
            f"💸 **Your share: ₹{share:.2f}**"
        )
        try:
            await context.bot.send_message(chat_id=telegram_user_id, text=text, parse_mode="Markdown")
        except Exception:
            pass


async def verify_transaction_owner(update: Update, context: ContextTypes.DEFAULT_TYPE, expense_id: int):
    """Verifies if the user clicking is the owner and if it's within their last 3 transactions."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat_id,), fetch=True)
    if not group:
        return False
    group_id = group[0][0]

    member = execute("SELECT member_id FROM members WHERE group_id=? AND telegram_user_id=?", (group_id, user_id), fetch=True)
    if not member:
        return False
    member_id = member[0][0]

    expense = execute("SELECT payer_member_id FROM expenses WHERE expense_id=?", (expense_id,), fetch=True)
    if not expense or expense[0][0] != member_id:
        await query.answer("❌ You can only modify your own transactions.", show_alert=True)
        return False

    recent_expenses = execute(
        "SELECT expense_id FROM expenses WHERE group_id=? AND payer_member_id=? ORDER BY expense_id DESC LIMIT 3",
        (group_id, member_id),
        fetch=True
    )
    recent_ids = [row[0] for row in recent_expenses]
    
    if expense_id not in recent_ids:
        await query.answer("❌ Only your last 3 transactions can be edited or deleted.", show_alert=True)
        return False

    return True


async def check_duplicate_expense(group_id: int, amount: float, category: str):
    """Checks if an expense with the same amount and category exists in the last 10 transactions."""
    recent_expenses = execute(
        """
        SELECT amount, category 
        FROM expenses 
        WHERE group_id = ? 
        ORDER BY expense_id DESC 
        LIMIT 10
        """,
        (group_id,),
        fetch=True
    )
    
    if not recent_expenses:
        return False
        
    for row in recent_expenses:
        db_amount, db_category = row
        if float(db_amount) == float(amount) and db_category.strip().lower() == category.strip().lower():
            return True
            
    return False


async def handle_duplicate_warning(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float, category: str, split_type: str):
    """Sends a warning prompt when a potential duplicate expense is detected."""
    chat_id = update.effective_chat.id
    card_msg_id = context.user_data.get("card_msg_id")
    
    warning_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Add Anyway", callback_data=f"force_add:{split_type}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")
        ]
    ])
    
    warning_text = (
        f"⚠️ **Duplicate Warning!**\n\n"
        f"An expense of **₹{amount:.2f}** for category **'{category}'** was already found within the last 10 transactions.\n\n"
        f"Are you sure you want to add this again?"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text=warning_text, reply_markup=warning_keyboard, parse_mode="Markdown")
    elif card_msg_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=card_msg_id, text=warning_text, reply_markup=warning_keyboard, parse_mode="Markdown")


async def duplicate_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user's choice on whether to bypass the duplicate warning."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if data == "cancel_expense":
        await query.edit_message_text("❌ Expense entry cancelled to prevent accidental duplicate charge.")
        context.user_data.clear()
        return ConversationHandler.END
        
    if data.startswith("force_add:"):
        split_type = data.split(":", 1)[1]
        context.user_data["bypass_duplicate_check"] = True
        
        # Route back to the appropriate split execution/prompt
        if split_type in ["⚖️ Equal", "split_equal"]:
            return await execute_equal_split(update, context)
            
        elif split_type in ["👤 Personal", "split_personal"]:
            return await execute_personal_split(update, context)
            
        elif split_type in ["🧮 Equally Among", "split_equal_among"]:
            active_members = context.user_data.get("active_members_cache", context.user_data.get("members", []))
            context.user_data["equal_among_selected_ids"] = [m[0] for m in active_members]
            context.user_data["equal_among_members_cache"] = active_members
            amount = context.user_data["amount"]
            await query.edit_message_text(
                f"💰 **Total:** ₹{amount:.2f}\n⚖️ **Split:** Equally Among\n\n"
                f"Duplicate warning bypassed. Tap to include/exclude members for this expense only, then Continue:",
                reply_markup=get_equal_among_menu(active_members, context.user_data["equal_among_selected_ids"]),
                parse_mode="Markdown",
            )
            return EQUAL_AMONG_SELECT

        elif split_type in ["💵 Exact Amount", "split_exact"]:
            context.user_data["split_type"] = "💵 Exact Amount"
            members = context.user_data.get("members", [])
            member_list_display = "\n".join([f"• `{m[0]}`: {m[1]}" for m in members])
            amount = context.user_data["amount"]
            await query.edit_message_text(
                f"💰 **Total:** ₹{amount:.2f}\n⚖️ **Split:** Exact Amount\n\n"
                f"Duplicate warning bypassed. Enter the exact amount for each member space-separated:\n\n"
                f"{member_list_display}\n\n"
                f"*(Example: `100 150 50`)*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]]),
            )
            return EXACT_AMOUNT
            
        elif split_type in ["📐 Ratio", "split_ratio"]:
            context.user_data["split_type"] = "📐 Ratio"
            members = context.user_data.get("members", [])
            member_list_display = "\n".join([f"• `{m[0]}`: {m[1]}" for m in members])
            amount = context.user_data["amount"]
            await query.edit_message_text(
                f"💰 **Total:** ₹{amount:.2f}\n⚖️ **Split:** Ratio\n\n"
                f"Duplicate warning bypassed. Enter ratios separated by `:`:\n\n"
                f"{member_list_display}\n\n"
                f"*(Example: `1:2:1`)*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]]),
            )
            return RATIO

    context.user_data.clear()
    return ConversationHandler.END


async def send_confirmation_card(update: Update, context: ContextTypes.DEFAULT_TYPE, expense_id: int, amount: float, category: str, description: str, split_type_str: str, summary_text: str=""):
    """Sends or edits the saved message card with Confirm on top, and Edit/Delete below."""
    chat_id = update.effective_chat.id
    card_msg_id = context.user_data.get("card_msg_id")

    confirm_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"exp_conf:{expense_id}")
        ],
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"exp_edit:{expense_id}"),
            InlineKeyboardButton("🗑️ Delete", callback_data=f"exp_del:{expense_id}")
        ]
    ])

    text = (
        f"✅ **Expense Saved**\n\n"
        f"💰 **Amount:** ₹{amount:.2f}\n"
        f"📂 **Category:** {category}\n"
        f"📝 **Description:** {description}\n"
        f"⚖️ **Split:** {split_type_str}\n"
        f"{summary_text}"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=confirm_keyboard, parse_mode="Markdown")
    elif card_msg_id:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=card_msg_id, text=text, reply_markup=confirm_keyboard, parse_mode="Markdown")
    else:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=confirm_keyboard, parse_mode="Markdown")
        context.user_data["card_msg_id"] = msg.message_id


async def expense_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Confirm, Edit, and Delete interactions on the transaction card."""
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id

    action, expense_id_str = data.split(":")
    expense_id = int(expense_id_str)

    if action in ["exp_edit", "exp_del"]:
        is_valid = await verify_transaction_owner(update, context, expense_id)
        if not is_valid:
            return

    await query.answer()

    if action == "exp_conf":
        # If this expense was added via a DM receipt-share, the whole flow
        # ran in the payer's private chat -- mirror a compact announcement
        # into the actual group so other members see it too.
        if update.effective_chat.type == "private":
            group_id = context.user_data.get("group_id")
            if group_id:
                group_row = execute("SELECT telegram_chat_id FROM groups WHERE group_id=?", (group_id,), fetch=True)
                expense_row = execute(
                    """
                    SELECT e.amount, e.category, e.description, m.display_name
                    FROM expenses e JOIN members m ON e.payer_member_id = m.member_id
                    WHERE e.expense_id=?
                    """,
                    (expense_id,),
                    fetch=True,
                )
                if group_row and expense_row:
                    group_chat_id = group_row[0][0]
                    amt, cat, desc, payer_name = expense_row[0]
                    desc_part = f"\n📝 {desc}" if desc else ""
                    announce_keyboard = InlineKeyboardMarkup(
                        [[InlineKeyboardButton("📝 Edit", callback_data=f"edit_exp:{expense_id}")]]
                    )
                    try:
                        await context.bot.send_message(
                            chat_id=group_chat_id,
                            text=(
                                f"💸 **New Expense (via shared receipt)**\n\n"
                                f"Paid by: **{payer_name}**\n"
                                f"📂 {cat}{desc_part}\n"
                                f"💰 ₹{amt:.2f}"
                            ),
                            parse_mode="Markdown",
                            reply_markup=announce_keyboard,
                        )
                    except Exception:
                        pass

        await query.edit_message_text("✅ **Transaction Confirmed & Locked.**", parse_mode="Markdown")
        auto_delete(context, chat_id, query.message.message_id, delay=DELETE_AFTER_CONFIRMATION)
        msg = await context.bot.send_message(chat_id=chat_id, text="🏠 **Main Menu:**", reply_markup=main_menu, parse_mode="Markdown")
        auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_CONFIRMATION)
        context.user_data.clear()

    elif action == "exp_del":
        shares_to_revert = execute("SELECT member_id, share_amount FROM expense_shares WHERE expense_id=?", (expense_id,), fetch=True)
        expense_info = execute("SELECT group_id, payer_member_id FROM expenses WHERE expense_id=?", (expense_id,), fetch=True)
        
        if expense_info and shares_to_revert:
            g_id, p_id = expense_info[0]
            reversed_shares = [(m_id, -amt) for m_id, amt in shares_to_revert]
            update_member_debts(g_id, expense_id, p_id, reversed_shares)

        execute("DELETE FROM expense_shares WHERE expense_id=?", (expense_id,))
        execute("DELETE FROM expenses WHERE expense_id=?", (expense_id,))
        
        await query.edit_message_text("🗑️ **Transaction deleted successfully.**", parse_mode="Markdown")
        auto_delete(context, chat_id, query.message.message_id, delay=DELETE_AFTER_CONFIRMATION)
        msg = await context.bot.send_message(chat_id=chat_id, text="🏠 **Main Menu:**", reply_markup=main_menu, parse_mode="Markdown")
        auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_CONFIRMATION)
        context.user_data.clear()

    elif action == "exp_edit":
        await query.edit_message_text("✏️ **Edit mode:** Please restart the expense process via the main menu to replace or re-add transactions.", parse_mode="Markdown")
        auto_delete(context, chat_id, query.message.message_id, delay=DELETE_AFTER_CONFIRMATION)
        msg = await context.bot.send_message(chat_id=chat_id, text="🏠 **Main Menu:**", reply_markup=main_menu, parse_mode="Markdown")
        auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_CONFIRMATION)
        context.user_data.clear()


async def cancel_expense_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the expense process, updates/deletes the bot message."""
    chat_id = update.effective_chat.id
    context.user_data.clear()
    query = update.callback_query

    if query:
        await query.answer()
        await query.edit_message_text("❌ Action cancelled.")
        msg_id = query.message.message_id
        auto_delete(context, chat_id, msg_id, delay=DELETE_AFTER_ACK)
        
    elif update.message:
        await safe_delete(context, chat_id, update.message.message_id)
        msg = await update.message.reply_text("❌ Action cancelled.", reply_markup=main_menu)
        auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_ACK)

    return ConversationHandler.END


async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: Sends initial prompt without timeout timers."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.message and update.message.text == "🏠 Main Menu":
        return await cancel_expense_callback(update, context)

    if update.message:
        await safe_delete(context, chat_id, update.message.message_id)

    old_card_id = context.user_data.get("card_msg_id")
    if old_card_id:
        await safe_delete(context, chat_id, old_card_id)

    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]
    ])

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="💰 **Enter the expense amount:**",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard,
    )
    
    context.user_data["initiator_id"] = user_id
    context.user_data["card_msg_id"] = msg.message_id

    return AMOUNT


async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles amount entry, deletes user message, and prompts category selection."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.message:
        await safe_delete(context, chat_id, update.message.message_id)

    if user_id != context.user_data.get("initiator_id"):
        return AMOUNT

    text = update.message.text
    card_msg_id = context.user_data.get("card_msg_id")

    if text == "🏠 Main Menu":
        return await cancel_expense_callback(update, context)

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        if card_msg_id:
            error_msg = await context.bot.send_message(
                chat_id=chat_id,
                text="❌ **Please enter a valid positive numeric amount:**",
                parse_mode="Markdown",
            )
            auto_delete(context, chat_id, error_msg.message_id, delay=DELETE_AFTER_ERROR)
        return AMOUNT

    context.user_data["amount"] = amount

    if card_msg_id:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=card_msg_id,
            text=f"💰 **Amount:** ₹{amount:.2f}\n\n📂 **Select Category:**",
            reply_markup=category_inline_menu,
            parse_mode="Markdown",
        )

    return CATEGORY


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler for category selection."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != context.user_data.get("initiator_id"):
        await query.answer("❌ Only the person who started this flow can respond.", show_alert=True)
        return CATEGORY

    await query.answer()

    category = query.data.split(":")[1]
    context.user_data["category"] = category
    amount = context.user_data["amount"]

    await query.edit_message_text(
        text=f"💰 **Amount:** ₹{amount:.2f}\n📂 **Category:** {category}\n\n📝 **Enter description in chat, or tap Skip:**",
        reply_markup=description_inline_menu,
        parse_mode="Markdown",
    )
    return DESCRIPTION


async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text description input from chat."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.message:
        await safe_delete(context, chat_id, update.message.message_id)

    if user_id != context.user_data.get("initiator_id"):
        return DESCRIPTION

    text = update.message.text
    card_msg_id = context.user_data.get("card_msg_id")

    if text == "🏠 Main Menu":
        return await cancel_expense_callback(update, context)

    context.user_data["description"] = text
    amount = context.user_data["amount"]
    category = context.user_data["category"]

    if card_msg_id:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=card_msg_id,
            text=f"💰 **Amount:** ₹{amount:.2f}\n📂 **Category:** {category}\n📝 **Description:** {text}\n\n⚖️ **Select Split Type:**",
            reply_markup=split_inline_menu,
            parse_mode="Markdown",
        )

    return SPLIT


async def description_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query handler when user clicks 'Skip' description."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != context.user_data.get("initiator_id"):
        await query.answer("❌ Only the person who started this flow can respond.", show_alert=True)
        return DESCRIPTION

    await query.answer()

    context.user_data["description"] = ""
    amount = context.user_data["amount"]
    category = context.user_data["category"]

    await query.edit_message_text(
        text=f"💰 **Amount:** ₹{amount:.2f}\n📂 **Category:** {category}\n📝 **Description:** -\n\n⚖️ **Select Split Type:**",
        reply_markup=split_inline_menu,
        parse_mode="Markdown",
    )
    return SPLIT


async def execute_equal_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executes database entry and confirmation card for Equal Split."""
    group_id = context.user_data["group_id"]
    payer_member_id = context.user_data["payer_member_id"]
    amount = context.user_data["amount"]
    category = context.user_data["category"]
    description = context.user_data.get("description", "")
    members = context.user_data["members"]

    # If anyone selected hasn't confirmed they're actually in this group yet
    # (added via the group-join invite flow, still pending), hold this split
    # instead of finalizing it -- see handlers/join.py for how it gets
    # completed automatically once the last person confirms.
    member_ids = [m[0] for m in members]
    placeholders = ",".join("?" * len(member_ids))
    status_rows = execute(
        f"SELECT member_id, status FROM members WHERE member_id IN ({placeholders})",
        tuple(member_ids),
        fetch=True,
    )
    status_map = {r[0]: r[1] for r in status_rows} if status_rows else {}

    if any(status_map.get(m[0]) != "confirmed" for m in members):
        return await _hold_equal_split_for_confirmation(
            update, context, group_id, payer_member_id, amount, category, description, members, status_map
        )

    expense_id = execute(
        """
        INSERT INTO expenses
        (group_id, payer_member_id, amount, category, description, split_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (group_id, payer_member_id, amount, category, description, "⚖️ Equal", now_ist()),
        return_lastrowid=True,
    )

    member_count = len(members)

    # Precise (unrounded) share -- this is what gets stored and used for
    # member_debts accounting, so summing it across many transactions never
    # drifts away from the true total. Rounded to 6dp only to keep the DB
    # value tidy; 6dp is far below a paisa so it has no real-world effect.
    exact_share = round(amount / member_count, 6)
    shares = [(expense_id, row[0], exact_share) for row in members]

    execute(
        "INSERT INTO expense_shares (expense_id, member_id, share_amount) VALUES (?, ?, ?)",
        shares,
        many=True,
    )

    debt_payload = [(row[0], exact_share) for row in members]
    update_member_debts(group_id, expense_id, payer_member_id, debt_payload)
    await notify_split_participants(context, group_id, payer_member_id, amount, category, description, "Equal", debt_payload)

    # Whole-rupee breakdown for THIS transaction only, purely for display --
    # never stored, never used for balances. See equal_split_display_shares().
    display_shares = equal_split_display_shares(amount, member_count)
    summary = "\n".join(
        f"• {m[1]}: ₹{format_rupees(disp)}" for m, disp in zip(members, display_shares)
    )
    description_str = description if description else "-"
    await send_confirmation_card(update, context, expense_id, amount, category, description_str, "Equal", summary_text=summary)
    return ConversationHandler.END


async def _hold_equal_split_for_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    group_id: int,
    payer_member_id: int,
    amount: float,
    category: str,
    description: str,
    members: list,
    status_map: dict,
):
    """
    Stashes an equal-split expense as a pending_expenses row (instead of
    writing it straight to `expenses`) and posts a live "waiting on..."
    checklist with a Join Group deep-link button. Finalized automatically
    the instant the last pending member confirms -- see
    handlers/join.py::_refresh_or_finalize_pending_expense.
    """
    chat_id = update.effective_chat.id

    pending_id = execute(
        """
        INSERT INTO pending_expenses (group_id, payer_member_id, amount, category, description, status_chat_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (group_id, payer_member_id, amount, category, description, chat_id),
        return_lastrowid=True,
    )

    execute(
        "INSERT INTO pending_expense_members (pending_id, member_id, confirmed) VALUES (?, ?, ?)",
        [
            (pending_id, m[0], 1 if status_map.get(m[0]) == "confirmed" else 0)
            for m in members
        ],
        many=True,
    )

    lines = [f"{'✅' if status_map.get(m[0]) == 'confirmed' else '⏳'} {m[1]}" for m in members]
    desc_part = f"\n📝 {description}" if description else ""
    text = (
        f"🧾 **Expense pending confirmation**\n\n"
        f"💰 **Total:** ₹{amount:.2f}\n"
        f"📂 **Category:** {category}{desc_part}\n"
        f"⚖️ **Split:** Equal\n\n"
        + "\n".join(lines)
        + "\n\nWaiting on everyone above to join SplitMate for this group before the split is finalized."
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Join Group", url=f"https://t.me/{context.bot.username}?start=join_{group_id}")]]
    )

    card_msg_id = context.user_data.get("card_msg_id")
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
        status_message_id = update.callback_query.message.message_id
    elif card_msg_id:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=card_msg_id, text=text, reply_markup=keyboard, parse_mode="Markdown"
        )
        status_message_id = card_msg_id
    else:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="Markdown")
        status_message_id = msg.message_id

    execute("UPDATE pending_expenses SET status_message_id=? WHERE pending_id=?", (status_message_id, pending_id))

    context.user_data.clear()
    return ConversationHandler.END


async def execute_personal_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executes database entry and confirmation card for Personal Expense."""
    group_id = context.user_data["group_id"]
    payer_member_id = context.user_data["payer_member_id"]
    amount = context.user_data["amount"]
    category = context.user_data["category"]
    description = context.user_data.get("description", "")

    expense_id = execute(
        """
        INSERT INTO expenses
        (group_id, payer_member_id, amount, category, description, split_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (group_id, payer_member_id, amount, category, description, "👤 Personal", now_ist()),
        return_lastrowid=True,
    )

    execute(
        "INSERT INTO expense_shares (expense_id, member_id, share_amount) VALUES (?, ?, ?)",
        (expense_id, payer_member_id, amount),
    )

    description_str = description if description else "-"
    await send_confirmation_card(update, context, expense_id, amount, category, description_str, "Personal")
    return ConversationHandler.END


async def split_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles split type selection via Inline Keyboard with duplicate interlock safeguard."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    if user_id != context.user_data.get("initiator_id"):
        await query.answer("❌ Only the person who started this flow can respond.", show_alert=True)
        return SPLIT

    await query.answer()

    data_parts = query.data.split(":")
    split_type = data_parts[1] if len(data_parts) > 1 else data_parts[0]

    group = execute(
        "SELECT group_id FROM groups WHERE telegram_chat_id=?",
        (chat_id,),
        fetch=True,
    )
    if context.user_data.get("group_locked") and context.user_data.get("group_id"):
        # A receipt shared in the bot's DM already resolved its target group
        # explicitly (via the group picker). That choice must win outright --
        # it must NOT be re-derived from chat_id, because chat_id here is the
        # user's own private chat, and /start() also happily creates a
        # "group" row for private chats ("Personal Chat"). If we deferred to
        # the chat_id lookup like the normal in-group flow does, a user who
        # had ever run /start in their DM would silently have every
        # DM-shared receipt saved to that phantom personal group instead of
        # the one they actually picked.
        group_id = context.user_data["group_id"]
    elif group:
        group_id = group[0][0]
    else:
        await query.edit_message_text("❌ Group not found.")
        context.user_data.clear()
        return ConversationHandler.END

    member = execute(
        "SELECT member_id FROM members WHERE group_id=? AND telegram_user_id=?",
        (group_id, user_id),
        fetch=True,
    )
    if not member:
        await query.edit_message_text("❌ Please send /start first.")
        context.user_data.clear()
        return ConversationHandler.END

    payer_member_id = member[0][0]
    amount = context.user_data["amount"]
    category = context.user_data["category"]

    context.user_data["group_id"] = group_id
    context.user_data["payer_member_id"] = payer_member_id

    active_members = execute(
        "SELECT member_id, display_name FROM members WHERE group_id=? AND is_active=1 ORDER BY member_id",
        (group_id,),
        fetch=True,
    )
    if not active_members:
        await query.edit_message_text("❌ No active members found in this group.")
        context.user_data.clear()
        return ConversationHandler.END

    # Use the payer's own saved subgroup as the default participant list, so
    # they don't have to pick people every time. Falls back to everyone
    # active in the group if they've never saved a subgroup (or reset it).
    subgroup_ids = get_user_subgroup_member_ids(group_id, user_id)
    if subgroup_ids:
        members = [m for m in active_members if m[0] in subgroup_ids]
        if not members:
            # Saved subgroup no longer overlaps any active member (e.g. everyone
            # in it left the group) -- fall back rather than blocking the flow.
            members = active_members
    else:
        members = active_members

    if not members:
        await query.edit_message_text("❌ No active members found in this group.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["members"] = members
    context.user_data["active_members_cache"] = active_members

    # --- DUPLICATE INTERLOCK CHECK ---
    if not context.user_data.get("bypass_duplicate_check", False):
        is_dup = await check_duplicate_expense(group_id, amount, category)
        if is_dup:
            context.user_data["split_type"] = split_type
            await handle_duplicate_warning(update, context, amount, category, split_type)
            return SPLIT

    # --- EQUAL SPLIT ---
    if split_type in ["⚖️ Equal", "split_equal"]:
        return await execute_equal_split(update, context)

    # --- EQUALLY AMONG (pick who this one expense is shared with) ---
    elif split_type in ["🧮 Equally Among", "split_equal_among"]:
        context.user_data["split_type"] = "🧮 Equally Among"
        # Default to everyone active selected -- the user un-taps whoever
        # this particular expense should NOT be split with.
        context.user_data["equal_among_selected_ids"] = [m[0] for m in active_members]
        context.user_data["equal_among_members_cache"] = active_members

        await query.edit_message_text(
            f"💰 **Total:** ₹{amount:.2f}\n⚖️ **Split:** Equally Among\n\n"
            f"Tap to include/exclude members for **this expense only**, then Continue:",
            reply_markup=get_equal_among_menu(active_members, context.user_data["equal_among_selected_ids"]),
            parse_mode="Markdown",
        )
        return EQUAL_AMONG_SELECT

    # --- PERSONAL EXPENSE ---
    elif split_type in ["👤 Personal", "split_personal"]:
        return await execute_personal_split(update, context)

    # --- EXACT AMOUNT SPLIT ---
    elif split_type in ["💵 Exact Amount", "split_exact"]:
        context.user_data["split_type"] = "💵 Exact Amount"
        member_list_display = "\n".join([f"• `{m[0]}`: {m[1]}" for m in members])
        
        await query.edit_message_text(
            f"💰 **Total:** ₹{amount:.2f}\n⚖️ **Split:** Exact Amount\n\n"
            f"Enter the exact amount for each member space-separated in the order listed below:\n\n"
            f"{member_list_display}\n\n"
            f"*(Example: `100 150 50`)*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]]),
        )
        return EXACT_AMOUNT

    # --- RATIO SPLIT ---
    elif split_type in ["📐 Ratio", "split_ratio"]:
        context.user_data["split_type"] = "📐 Ratio"
        member_list_display = "\n".join([f"• `{m[0]}`: {m[1]}" for m in members])

        await query.edit_message_text(
            f"💰 **Total:** ₹{amount:.2f}\n⚖️ **Split:** Ratio\n\n"
            f"Enter ratios separated by `:` corresponding to the member order:\n\n"
            f"{member_list_display}\n\n"
            f"*(Example: `1:2:1`)*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]]),
        )
        return RATIO

    else:
        await query.edit_message_text(f"❌ Split type not recognized: {split_type}")
        context.user_data.clear()
        return ConversationHandler.END


async def equal_among_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles one member in/out of this one-off 'Equally Among' selection."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != context.user_data.get("initiator_id"):
        await query.answer("❌ Only the person who started this flow can respond.", show_alert=True)
        return EQUAL_AMONG_SELECT

    await query.answer()

    member_id = int(query.data.split(":")[1])
    selected_ids = context.user_data.get("equal_among_selected_ids", [])
    if member_id in selected_ids:
        selected_ids.remove(member_id)
    else:
        selected_ids.append(member_id)
    context.user_data["equal_among_selected_ids"] = selected_ids

    members = context.user_data.get("equal_among_members_cache", [])
    amount = context.user_data["amount"]

    await query.edit_message_text(
        f"💰 **Total:** ₹{amount:.2f}\n⚖️ **Split:** Equally Among\n\n"
        f"Tap to include/exclude members for **this expense only**, then Continue:",
        reply_markup=get_equal_among_menu(members, selected_ids),
        parse_mode="Markdown",
    )
    return EQUAL_AMONG_SELECT


async def equal_among_continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Locks in the chosen subset and runs the same equal-split execution
    path used by the regular 'Equal' option (including the pending-
    confirmation hold if anyone selected hasn't joined the group yet)."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != context.user_data.get("initiator_id"):
        await query.answer("❌ Only the person who started this flow can respond.", show_alert=True)
        return EQUAL_AMONG_SELECT

    selected_ids = context.user_data.get("equal_among_selected_ids", [])
    if not selected_ids:
        await query.answer("❌ Select at least one member first.", show_alert=True)
        return EQUAL_AMONG_SELECT

    await query.answer()

    members_cache = context.user_data.get("equal_among_members_cache", [])
    context.user_data["members"] = [m for m in members_cache if m[0] in selected_ids]

    return await execute_equal_split(update, context)


async def exact_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles bulk space-separated exact amount shares typed in chat."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.message:
        await safe_delete(context, chat_id, update.message.message_id)

    if user_id != context.user_data.get("initiator_id"):
        return EXACT_AMOUNT

    text = update.message.text
    card_msg_id = context.user_data.get("card_msg_id")

    if text == "🏠 Main Menu":
        return await cancel_expense_callback(update, context)

    members = context.user_data["members"]
    expense_total = context.user_data["amount"]

    try:
        amounts = [float(x) for x in text.replace(",", " ").split()]
        if len(amounts) != len(members) or any(a < 0 for a in amounts):
            raise ValueError
    except ValueError:
        if card_msg_id:
            error_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ **Invalid format or count.** Please enter exactly {len(members)} positive numbers separated by spaces.",
                parse_mode="Markdown",
            )
            auto_delete(context, chat_id, error_msg.message_id, delay=DELETE_AFTER_ERROR)
        return EXACT_AMOUNT

    total_entered = sum(amounts)
    if round(total_entered, 2) != round(expense_total, 2):
        if card_msg_id:
            error_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Sum of amounts (₹{total_entered:.2f}) does not match expense total (₹{expense_total:.2f}). Try again:",
                parse_mode="Markdown",
            )
            auto_delete(context, chat_id, error_msg.message_id, delay=DELETE_AFTER_ERROR)
        return EXACT_AMOUNT

    group_id = context.user_data["group_id"]
    payer_member_id = context.user_data["payer_member_id"]

    expense_id = execute(
        """
        INSERT INTO expenses
        (group_id, payer_member_id, amount, category, description, split_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_id,
            payer_member_id,
            expense_total,
            context.user_data["category"],
            context.user_data.get("description", ""),
            context.user_data["split_type"],
            now_ist(),
        ),
        return_lastrowid=True,
    )

    db_shares = [(expense_id, member[0], amt) for member, amt in zip(members, amounts)]
    execute(
        "INSERT INTO expense_shares (expense_id, member_id, share_amount) VALUES (?, ?, ?)",
        db_shares,
        many=True,
    )

    debt_payload = [(member[0], amt) for member, amt in zip(members, amounts)]
    update_member_debts(group_id, expense_id, payer_member_id, debt_payload)
    await notify_split_participants(
        context, group_id, payer_member_id, expense_total,
        context.user_data["category"], context.user_data.get("description", ""),
        "Exact Amount", debt_payload,
    )

    description_str = context.user_data.get("description") or "-"
    summary = "\n".join(f"• {m[1]}: ₹{amt:.2f}" for m, amt in zip(members, amounts))

    await send_confirmation_card(update, context, expense_id, expense_total, context.user_data["category"], description_str, "Exact Amount", summary_text=summary)
    return ConversationHandler.END


async def ratio_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ratio inputs typed in chat."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.message:
        await safe_delete(context, chat_id, update.message.message_id)

    if user_id != context.user_data.get("initiator_id"):
        return RATIO

    text = update.message.text
    card_msg_id = context.user_data.get("card_msg_id")

    if text == "🏠 Main Menu":
        return await cancel_expense_callback(update, context)

    members = context.user_data["members"]

    try:
        ratios = [float(x) for x in text.split(":")]
        if len(ratios) != len(members) or any(r < 0 for r in ratios) or sum(ratios) <= 0:
            raise ValueError
    except ValueError:
        if card_msg_id:
            error_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ **Invalid format or count.** Please enter exactly {len(members)} ratios separated by `:` (e.g. `1:2:1`).",
                parse_mode="Markdown",
            )
            auto_delete(context, chat_id, error_msg.message_id, delay=DELETE_AFTER_ERROR)
        return RATIO

    total_ratio = sum(ratios)
    shares = []
    expense_total = context.user_data["amount"]

    for member, ratio in zip(members, ratios):
        share = round(expense_total * ratio / total_ratio, 2)
        shares.append((member[0], share))

    difference = round(expense_total - sum(x[1] for x in shares), 2)
    if difference != 0:
        member_id, share = shares[-1]
        shares[-1] = (member_id, round(share + difference, 2))

    group_id = context.user_data["group_id"]
    payer_member_id = context.user_data["payer_member_id"]

    expense_id = execute(
        """
        INSERT INTO expenses
        (group_id, payer_member_id, amount, category, description, split_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_id,
            payer_member_id,
            expense_total,
            context.user_data["category"],
            context.user_data.get("description", ""),
            context.user_data["split_type"],
            now_ist(),
        ),
        return_lastrowid=True,
    )

    db_shares = [(expense_id, m_id, sh) for m_id, sh in shares]
    execute(
        "INSERT INTO expense_shares (expense_id, member_id, share_amount) VALUES (?, ?, ?)",
        db_shares,
        many=True,
    )

    update_member_debts(group_id, expense_id, payer_member_id, shares)
    await notify_split_participants(
        context, group_id, payer_member_id, expense_total,
        context.user_data["category"], context.user_data.get("description", ""),
        "Ratio", shares,
    )

    description_str = context.user_data.get("description") or "-"
    summary = "\n".join(
        f"• {member[1]}: ₹{share:.2f}"
        for member, (_, share) in zip(members, shares)
    )

    await send_confirmation_card(update, context, expense_id, expense_total, context.user_data["category"], description_str, "Ratio", summary_text=summary)
    return ConversationHandler.END