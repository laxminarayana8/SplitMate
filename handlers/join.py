import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import execute, now_ist, get_or_create_group_id, deactivate_member
from utils import auto_delete, DELETE_AFTER_NOTICE


# -----------------------------
# 1. Bot added to a group -- seed the `groups` row immediately, without
#    waiting for any human to send /start.
# -----------------------------
async def bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires on telegram's my_chat_member update -- specifically when the
    bot's own membership status in a chat changes (e.g. it gets added)."""
    cmu = update.my_chat_member
    if not cmu:
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    was_out = old_status in ("left", "kicked")
    is_in = new_status in ("member", "administrator")

    if was_out and is_in:
        get_or_create_group_id(chat.id, chat.title)


# -----------------------------
# 2. Humans added to a group -- register them as 'pending' members (once
#    each, ever, per group) and post an invite card with a Join Group button.
# -----------------------------
async def new_chat_members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Previously this whole function ran with no error handling. Telegram can
    deliver a big "add 6 people" action as several separate service
    messages in quick succession, and any unhandled exception here (a
    flaky get_or_create_group_id race -- now fixed separately -- a
    transient Telegram API error on send_message, etc.) was silently
    swallowed by PTB's default error handler: the welcome message for that
    particular batch just never went out, with nothing in the chat to show
    it failed. That matches "sometimes it's working and sometimes not."
    Wrapping the body and adding one retry around the final send_message
    (for transient/rate-limit errors) makes a single bad update fail loud
    (logged) instead of silently, and recover on its own where possible.
    """
    try:
        await _handle_new_chat_members(update, context)
    except Exception:
        import logging
        logging.exception("new_chat_members_handler failed for chat %s", update.effective_chat.id if update.effective_chat else "?")


async def _handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat

    if not message or not message.new_chat_members:
        return

    group_id = get_or_create_group_id(chat.id, chat.title)

    newly_pending_names = []
    for user in message.new_chat_members:
        if user.is_bot:
            continue

        existing = execute(
            "SELECT member_id FROM members WHERE group_id=? AND telegram_user_id=?",
            (group_id, user.id),
            fetch=True,
        )
        if existing:
            # Already known for this group (pending OR confirmed) -- never
            # re-register or re-prompt them for the same group. Also
            # covers someone who was previously removed (is_active=0) and
            # has now been re-added: welcome them back as active again
            # rather than leaving them stuck inactive.
            execute(
                "UPDATE members SET is_active=1 WHERE member_id=?",
                (existing[0][0],),
            )
            continue

        execute(
            """
            INSERT INTO members (group_id, telegram_user_id, display_name, status)
            VALUES (?, ?, ?, 'pending')
            """,
            (group_id, user.id, user.first_name),
        )
        newly_pending_names.append(user.first_name)

    if not newly_pending_names:
        return

    bot_username = context.bot.username
    join_url = f"https://t.me/{bot_username}?start=join_{group_id}"

    names_block = "\n".join(f"• {name}" for name in newly_pending_names)
    text = (
        f"👋 Welcome {', '.join(newly_pending_names)}!\n\n"
        f"Waiting for {len(newly_pending_names)} member(s) to join SplitMate for this group:\n\n"
        f"{names_block}\n\n"
        f"Tap the button below -- it opens a DM with me, just press Confirm there."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Join Group", url=join_url)]])

    for attempt in range(2):
        try:
            await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=keyboard)
            break
        except Exception:
            if attempt == 1:
                raise
            await asyncio.sleep(1.5)


# -----------------------------
# 2b. A human left OR was removed from the group -- mark them inactive.
# -----------------------------
# There was previously no handler for this at all. A member who got
# removed (especially one who was still 'pending' -- added by mistake and
# removed before they ever confirmed) stayed is_active=1 forever, so every
# "active members" query -- including the equal-split participant list --
# kept including them. Equal split would then hold the expense open
# forever "waiting" on a Join Group confirmation from someone who isn't
# even in the chat anymore. This listens for Telegram's own "member
# left/removed" service message and flips them inactive so future splits
# stop counting them.
async def chat_member_left_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat

    if not message or not message.left_chat_member:
        return

    user = message.left_chat_member
    if user.is_bot:
        return

    group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat.id,), fetch=True)
    if not group:
        return
    group_id = group[0][0]

    deactivate_member(group_id, user.id)


# -----------------------------
# 3. Deep-link landed in DM ("/start join_<group_id>") -- show the Confirm
#    prompt for that specific group. Called from handlers/start.py.
# -----------------------------
async def send_join_confirmation_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: int):
    user = update.effective_user
    chat_id = update.effective_chat.id

    group_row = execute("SELECT group_name FROM groups WHERE group_id=?", (group_id,), fetch=True)
    group_name = group_row[0][0] if group_row else "that group"

    member = execute(
        "SELECT member_id, status FROM members WHERE group_id=? AND telegram_user_id=?",
        (group_id, user.id),
        fetch=True,
    )

    if not member:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ I don't have an invite for you in **{group_name}**. Ask whoever added you to the group to check it's still there.",
            parse_mode="Markdown",
        )
        auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_NOTICE)
        return

    member_id, status = member[0]

    if status == "confirmed":
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ You're already part of **{group_name}** -- nothing to confirm.",
            parse_mode="Markdown",
        )
        auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_NOTICE)
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_join:{group_id}")]]
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"👋 Hi {user.first_name}!\n\n"
            f"You were added to **{group_name}** on SplitMate.\n\n"
            f"Press Confirm to join -- you'll be included in expense splits for this group from then on."
        ),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# -----------------------------
# 4. "Confirm" tapped in DM -- register them for good, then resolve any
#    expense that was waiting specifically on this person.
# -----------------------------
async def confirm_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    group_id = int(query.data.split(":", 1)[1])

    member = execute(
        "SELECT member_id, status FROM members WHERE group_id=? AND telegram_user_id=?",
        (group_id, user.id),
        fetch=True,
    )
    if not member:
        await query.answer("⚠️ No invite found for you in that group.", show_alert=True)
        return

    member_id, status = member[0]
    group_row = execute("SELECT group_name FROM groups WHERE group_id=?", (group_id,), fetch=True)
    group_name = group_row[0][0] if group_row else "the group"

    if status == "confirmed":
        await query.answer("Already confirmed ✅", show_alert=True)
        return

    await query.answer("🎉 You're in!")

    execute(
        "UPDATE members SET status='confirmed', is_active=1, display_name=? WHERE member_id=?",
        (user.first_name, member_id),
    )

    await query.edit_message_text(
        f"🎉 You're in **{group_name}**! You'll be included in future splits.",
        parse_mode="Markdown",
    )

    pending_rows = execute(
        """
        SELECT pe.pending_id
        FROM pending_expense_members pem
        JOIN pending_expenses pe ON pem.pending_id = pe.pending_id
        WHERE pem.member_id = ? AND pem.confirmed = 0 AND pe.group_id = ?
        """,
        (member_id, group_id),
        fetch=True,
    )

    for (pending_id,) in (pending_rows or []):
        execute(
            "UPDATE pending_expense_members SET confirmed=1 WHERE pending_id=? AND member_id=?",
            (pending_id, member_id),
        )
        await _refresh_or_finalize_pending_expense(context, pending_id)


async def _refresh_or_finalize_pending_expense(context: ContextTypes.DEFAULT_TYPE, pending_id: int):
    """Re-renders the waiting checklist, or -- if everyone required has now
    confirmed -- writes the real expense and posts the final split."""
    row = execute(
        """
        SELECT group_id, payer_member_id, amount, category, description,
               status_chat_id, status_message_id
        FROM pending_expenses WHERE pending_id=?
        """,
        (pending_id,),
        fetch=True,
    )
    if not row:
        return
    group_id, payer_member_id, amount, category, description, status_chat_id, status_message_id = row[0]

    member_rows = execute(
        """
        SELECT pem.member_id, m.display_name, pem.confirmed
        FROM pending_expense_members pem
        JOIN members m ON pem.member_id = m.member_id
        WHERE pem.pending_id=?
        ORDER BY pem.member_id
        """,
        (pending_id,),
        fetch=True,
    )
    if not member_rows:
        return

    if not all(confirmed for _, _, confirmed in member_rows):
        lines = [f"{'✅' if confirmed else '⏳'} {name}" for _, name, confirmed in member_rows]
        desc_part = f"\n📝 {description}" if description else ""
        text = (
            f"🧾 **Expense pending confirmation**\n\n"
            f"💰 **Total:** ₹{amount:.2f}\n"
            f"📂 **Category:** {category}{desc_part}\n"
            f"⚖️ **Split:** Equal\n\n"
            + "\n".join(lines)
            + "\n\nWaiting on everyone above to join before this split is finalized."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Join Group", url=f"https://t.me/{context.bot.username}?start=join_{group_id}")]]
        )
        try:
            await context.bot.edit_message_text(
                chat_id=status_chat_id, message_id=status_message_id, text=text,
                reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # Everyone required has confirmed -- finalize for real now.
    from handlers.expense import (
        update_member_debts,
        notify_split_participants,
        equal_split_display_shares,
        format_rupees,
    )

    expense_id = execute(
        """
        INSERT INTO expenses (group_id, payer_member_id, amount, category, description, split_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (group_id, payer_member_id, amount, category, description, "⚖️ Equal", now_ist()),
        return_lastrowid=True,
    )

    members = [(m_id, name) for m_id, name, _ in member_rows]
    member_count = len(members)
    exact_share = round(amount / member_count, 6)

    execute(
        "INSERT INTO expense_shares (expense_id, member_id, share_amount) VALUES (?, ?, ?)",
        [(expense_id, m_id, exact_share) for m_id, _ in members],
        many=True,
    )

    debt_payload = [(m_id, exact_share) for m_id, _ in members]
    update_member_debts(group_id, expense_id, payer_member_id, debt_payload)
    await notify_split_participants(context, group_id, payer_member_id, amount, category, description, "Equal", debt_payload, expense_id=expense_id)

    display_shares = equal_split_display_shares(amount, member_count)
    summary = "\n".join(f"• {name}: ₹{format_rupees(disp)}" for (_, name), disp in zip(members, display_shares))
    desc_part = f"📝 {description}\n" if description else ""

    text = (
        f"🎉 **Expense Split Successfully!**\n\n"
        f"💰 **Total:** ₹{amount:.2f}\n"
        f"{desc_part}⚖️ **Split:** Equal\n\n"
        f"{summary}"
    )
    try:
        await context.bot.edit_message_text(
            chat_id=status_chat_id, message_id=status_message_id, text=text, parse_mode="Markdown",
        )
    except Exception:
        pass

    execute("DELETE FROM pending_expenses WHERE pending_id=?", (pending_id,))
