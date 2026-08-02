from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import execute, now_ist
from utils import format_rupees
from states import SETTLE_MENU, PARTIAL_SETTLEMENT


async def settle_up_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    group = execute(
        "SELECT group_id FROM groups WHERE telegram_chat_id=?",
        (chat_id,),
        fetch=True
    )

    if not group:
        await update.message.reply_text("❌ Group not found.")
        return ConversationHandler.END

    group_id = group[0][0]

    member = execute(
        """
        SELECT member_id
        FROM members
        WHERE group_id=?
        AND telegram_user_id=?
        """,
        (group_id, user_id),
        fetch=True,
    )

    if not member:
        await update.message.reply_text(
            "❌ Please send /start first."
        )
        return ConversationHandler.END

    current_member = member[0][0]

    debts = execute(
        """
        SELECT
            md.debt_id,
            md.creditor_member_id,
            md.amount,
            m.display_name
        FROM member_debts md
        JOIN members m
        ON md.creditor_member_id=m.member_id
        WHERE md.debtor_member_id=?
        AND md.amount>0
        ORDER BY md.amount DESC
        """,
        (current_member,),
        fetch=True,
    )

    if not debts:
        await update.message.reply_text(
            "🎉 You have no pending dues."
        )
        return ConversationHandler.END

    keyboard = []
    total_due = 0

    for debt_id, creditor_id, amount, creditor_name in debts:
        total_due += amount
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"💰 {creditor_name} • ₹{format_rupees(amount)}",
                    callback_data=f"settle_select:{debt_id}"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="settle_cancel"
            )
        ]
    )

    await update.message.reply_text(
        f"💸 Total Pending : ₹{format_rupees(total_due)}\n\n"
        f"Select the person you want to settle with.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SETTLE_MENU


async def settle_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    debt_id = int(query.data.split(":")[1])

    debt = execute(
        """
        SELECT
            md.debt_id,
            md.debtor_member_id,
            md.creditor_member_id,
            md.amount,
            m.display_name
        FROM member_debts md
        JOIN members m
        ON md.creditor_member_id=m.member_id
        WHERE md.debt_id=?
        """,
        (debt_id,),
        fetch=True,
    )

    if not debt:
        await query.edit_message_text(
            "❌ Debt not found."
        )
        return ConversationHandler.END

    debt_id, debtor_id, creditor_id, amount, creditor_name = debt[0]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Fully Settle Up",
                    callback_data=f"settle_full:{debt_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "💵 Partial Settle Up",
                    callback_data=f"settle_partial:{debt_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="settle_back"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="settle_cancel"
                )
            ]
        ]
    )

    await query.edit_message_text(
        f"🤝 **Settlement with {creditor_name}**\n\n"
        f"Remaining Debt : **₹{format_rupees(amount)}**\n\n"
        f"Choose how you'd like to settle.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return SETTLE_MENU


async def settle_up_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = query.data.split(":")[0]
    debt_id = int(query.data.split(":")[1])

    debt = execute(
        """
        SELECT
            md.debt_id,
            md.amount,
            md.creditor_member_id,
            m.display_name
        FROM member_debts md
        JOIN members m
        ON md.creditor_member_id=m.member_id
        WHERE md.debt_id=?
        """,
        (debt_id,),
        fetch=True,
    )

    if not debt:
        await query.edit_message_text(
            "❌ Debt not found."
        )
        return ConversationHandler.END

    debt_id, amount, creditor_id, creditor_name = debt[0]

    # ------------------------
    # FULL SETTLEMENT
    # ------------------------
    if action == "settle_full":
        context.user_data["settle_type"] = "full"
        context.user_data["debt_id"] = debt_id
        context.user_data["amount"] = amount

        await query.edit_message_text(
            f"✅ Full Settlement\n\n"
            f"Creditor : {creditor_name}\n"
            f"Amount : ₹{format_rupees(amount)}\n\n"
            f"Sending confirmation request...",
            parse_mode="Markdown",
        )

        debt_info = execute(
            """
            SELECT
                md.debtor_member_id,
                md.creditor_member_id,
                md.amount,
                d.telegram_user_id,
                c.telegram_user_id,
                d.display_name,
                c.display_name
            FROM member_debts md
            JOIN members d
            ON md.debtor_member_id=d.member_id
            JOIN members c
            ON md.creditor_member_id=c.member_id
            WHERE md.debt_id=?
            """,
            (debt_id,),
            fetch=True,
        )

        debtor_id, creditor_id, balance, debtor_tg, creditor_tg, debtor_name, creditor_name = debt_info[0]

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve",
                        callback_data=f"approve_full:{debt_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Decline",
                        callback_data=f"decline_full:{debt_id}"
                    )
                ]
            ]
        )

        await context.bot.send_message(
            chat_id=creditor_tg,
            text=f"🤝 Settlement Request\n\n"
                 f"**{debtor_name}** says they paid\n\n"
                 f"₹{format_rupees(amount)}\n\n"
                 f"Approve?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

        await query.edit_message_text(
            "✅ Approval request sent.",
        )

        return ConversationHandler.END

    # ------------------------
    # PARTIAL
    # ------------------------
    elif action == "settle_partial":
        context.user_data["settle_type"] = "partial"
        context.user_data["debt_id"] = debt_id
        context.user_data["max_amount"] = amount

        await query.edit_message_text(
            f"💵 Partial Settlement\n\n"
            f"Remaining Debt : ₹{format_rupees(amount)}\n\n"
            f"Please enter the amount you paid.",
            parse_mode="Markdown",
        )

        return PARTIAL_SETTLEMENT


async def partial_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
    except ValueError:
        await update.message.reply_text(
            "❌ Enter a valid amount."
        )
        return PARTIAL_SETTLEMENT

    max_amount = context.user_data["max_amount"]

    if amount <= 0 or amount > max_amount:
        await update.message.reply_text(
            f"❌ Amount should be between ₹0 and ₹{format_rupees(max_amount)}"
        )
        return PARTIAL_SETTLEMENT

    context.user_data["amount"] = amount
    debt_id = context.user_data["debt_id"]

    debt = execute(
        """
        SELECT
            md.debtor_member_id,
            md.creditor_member_id,
            d.telegram_user_id,
            c.telegram_user_id,
            d.display_name,
            c.display_name
        FROM member_debts md
        JOIN members d
        ON md.debtor_member_id=d.member_id
        JOIN members c
        ON md.creditor_member_id=c.member_id
        WHERE md.debt_id=?
        """,
        (debt_id,),
        fetch=True,
    )

    debtor_id, creditor_id, debtor_tg, creditor_tg, debtor_name, creditor_name = debt[0]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"approve_partial:{debt_id}:{amount}"
                ),
                InlineKeyboardButton(
                    "❌ Decline",
                    callback_data=f"decline_partial:{debt_id}:{amount}"
                )
            ]
        ]
    )

    await context.bot.send_message(
        chat_id=creditor_tg,
        text=f"🤝 Partial Settlement\n\n"
             f"{debtor_name} says they paid\n\n"
             f"₹{format_rupees(amount)}\n\n"
             f"Approve?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

    await update.message.reply_text(
        "✅ Approval request sent."
    )

    context.user_data.clear()
    return ConversationHandler.END


async def settle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split(":")
    action = parts[0]

    # -------------------------
    # FULL APPROVAL
    # -------------------------
    if action == "approve_full":
        debt_id = int(parts[1])

        debt = execute(
            """
            SELECT
                md.group_id,
                md.debtor_member_id,
                md.creditor_member_id,
                md.amount
            FROM member_debts md
            WHERE md.debt_id=?
            """,
            (debt_id,),
            fetch=True,
        )

        if not debt:
            await query.edit_message_text(
                "❌ Debt not found."
            )
            return

        group_id, debtor_id, creditor_id, amount = debt[0]

        execute(
            """
            INSERT INTO settlements
            (
                debt_id,
                group_id,
                debtor_member_id,
                creditor_member_id,
                amount,
                settled_at
            )
            VALUES (?,?,?,?,?,?)
            """,
            (
                debt_id,
                group_id,
                debtor_id,
                creditor_id,
                amount,
                now_ist(),
            ),
        )

        execute(
            """
            DELETE
            FROM member_debts
            WHERE debt_id=?
            """,
            (debt_id,),
        )

        debtor = execute(
            """
            SELECT telegram_user_id, display_name
            FROM members
            WHERE member_id=?
            """,
            (debtor_id,),
            fetch=True,
        )[0]

        creditor = execute(
            """
            SELECT display_name
            FROM members
            WHERE member_id=?
            """,
            (creditor_id,),
            fetch=True,
        )[0][0]

        await query.edit_message_text(
            f"✅ Full settlement approved.\n\nReceived ₹{format_rupees(amount)}"
        )

        try:
            await context.bot.send_message(
                debtor[0],
                f"✅ {creditor} approved your settlement.\n\nDebt cleared."
            )
        except Exception:
            pass

        return

    # -------------------------
    # PARTIAL APPROVAL
    # -------------------------
    elif action == "approve_partial":
        debt_id = int(parts[1])
        paid = float(parts[2])

        debt = execute(
            """
            SELECT
                md.group_id,
                md.debtor_member_id,
                md.creditor_member_id,
                md.amount
            FROM member_debts md
            WHERE md.debt_id=?
            """,
            (debt_id,),
            fetch=True,
        )

        if not debt:
            await query.edit_message_text(
                "❌ Debt not found."
            )
            return

        group_id, debtor_id, creditor_id, balance = debt[0]

        execute(
            """
            INSERT INTO settlements
            (
                debt_id,
                group_id,
                debtor_member_id,
                creditor_member_id,
                amount,
                settled_at
            )
            VALUES (?,?,?,?,?,?)
            """,
            (
                debt_id,
                group_id,
                debtor_id,
                creditor_id,
                paid,
                now_ist(),
            ),
        )

        remaining = round(balance - paid, 2)

        if remaining <= 0:
            execute(
                """
                DELETE
                FROM member_debts
                WHERE debt_id=?
                """,
                (debt_id,),
            )
        else:
            execute(
                """
                UPDATE member_debts
                SET amount=?
                WHERE debt_id=?
                """,
                (
                    remaining,
                    debt_id,
                ),
            )

        debtor = execute(
            """
            SELECT telegram_user_id, display_name
            FROM members
            WHERE member_id=?
            """,
            (debtor_id,),
            fetch=True,
        )[0]

        creditor = execute(
            """
            SELECT display_name
            FROM members
            WHERE member_id=?
            """,
            (creditor_id,),
            fetch=True,
        )[0][0]

        await query.edit_message_text(
            f"✅ Partial payment approved.\n\n"
            f"Received : ₹{format_rupees(paid)}\n"
            f"Remaining : ₹{format_rupees(max(remaining, 0))}"
        )

        try:
            await context.bot.send_message(
                debtor[0],
                f"✅ {creditor} approved your payment.\n\n"
                f"Paid : ₹{format_rupees(paid)}\n"
                f"Remaining : ₹{format_rupees(max(remaining, 0))}"
            )
        except Exception:
            pass

        return

    # -------------------------
    # DECLINE
    # -------------------------
    elif action.startswith("decline"):

        debt_id = int(parts[1])

        debt = execute(
            """
            SELECT
                md.debtor_member_id,
                md.creditor_member_id,
                d.telegram_user_id,
                d.display_name,
                c.display_name
            FROM member_debts md
            JOIN members d
                ON md.debtor_member_id = d.member_id
            JOIN members c
                ON md.creditor_member_id = c.member_id
            WHERE md.debt_id = ?
            """,
            (debt_id,),
            fetch=True,
        )

        if debt:
            debtor_id, creditor_id, debtor_tg, debtor_name, creditor_name = debt[0]

            await query.edit_message_text(
                f"❌ You declined the settlement request from **{debtor_name}**.",
                parse_mode="Markdown"
            )

            try:
                await context.bot.send_message(
                    chat_id=debtor_tg,
                    text=(
                        f"❌ **Settlement Declined**\n\n"
                        f"**{creditor_name}** has declined your settlement request."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Failed to notify debtor: {e}")

        else:
            await query.edit_message_text("❌ Debt record not found.")


async def settle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    fake_update = Update(
        update.update_id,
        message=query.message
    )

    fake_update.effective_chat = update.effective_chat
    fake_update.effective_user = update.effective_user

    await settle_up_start(fake_update, context)
    return SETTLE_MENU


async def settle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Settlement cancelled.")
    return ConversationHandler.END