from telegram import Update
from telegram.ext import ContextTypes

from database import execute
from keyboards.menus import main_menu
from utils import auto_delete, DELETE_AFTER_CONFIRMATION  # <--- Imported auto_delete helper
from handlers.join import send_join_confirmation_prompt


async def safe_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Safely deletes a message without crashing if it's already deleted or lacks rights."""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id
    group_name = update.effective_chat.title

    if group_name is None:
        group_name = "Personal Chat"

    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    # -----------------------------
    # 1. Delete Incoming /start Command
    # -----------------------------
    if update.message:
        await safe_delete(context, chat_id, update.message.message_id)

    # -----------------------------
    # 2. Create Group (if not exists)
    # -----------------------------
    group = execute(
        "SELECT group_id FROM groups WHERE telegram_chat_id = ?",
        (chat_id,),
        fetch=True,
    )

    if group:
        group_id = group[0][0]

    else:

        execute(
            """
            INSERT INTO groups
            (telegram_chat_id, group_name)
            VALUES (?, ?)
            """,
            (chat_id, group_name),
        )

        group_id = execute(
            "SELECT group_id FROM groups WHERE telegram_chat_id = ?",
            (chat_id,),
            fetch=True,
        )[0][0]

    # -----------------------------
    # 3. Register Member (if not exists)
    # -----------------------------
    member = execute(
        """
        SELECT member_id
        FROM members
        WHERE group_id = ?
        AND telegram_user_id = ?
        """,
        (group_id, user_id),
        fetch=True,
    )

    if not member:

        execute(
            """
            INSERT INTO members
            (
                group_id,
                telegram_user_id,
                display_name
            )
            VALUES (?, ?, ?)
            """,
            (
                group_id,
                user_id,
                user_name,
            ),
        )

    else:

        execute(
            """
            UPDATE members
            SET
                display_name = ?,
                is_active = 1
            WHERE
                member_id = ?
            """,
            (
                user_name,
                member[0][0],
            ),
        )

    # -----------------------------
    # 4. Send Welcome Message & Schedule Auto-Delete
    # -----------------------------
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"👋 Welcome {user_name}!\n\nWelcome to SplitMate.\n\nChoose an option below.",
        reply_markup=main_menu,
    )

    # Automatically delete the welcome prompt after 15 seconds
    auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_CONFIRMATION)

    # -----------------------------
    # 5. Deep-link "join_<group_id>" payload (from the "✅ Join Group" button
    #    tapped in a group chat) -- show a Confirm prompt for that specific
    #    group instead of leaving the person on the generic welcome screen.
    # -----------------------------
    if context.args and len(context.args) == 1 and context.args[0].startswith("join_"):
        try:
            invite_group_id = int(context.args[0].split("_", 1)[1])
        except (IndexError, ValueError):
            invite_group_id = None

        if invite_group_id is not None:
            await send_join_confirmation_prompt(update, context, invite_group_id)