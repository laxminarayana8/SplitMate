from telegram import Update
from telegram.ext import ContextTypes
from database import (
    execute,
    get_user_subgroup_member_ids,
    save_user_subgroup,
    reset_user_subgroup,
    get_member_currency,
    set_member_currency,
)
from keyboards.menus import settings_inline_menu, get_member_selection_menu, get_currency_menu, main_menu
from utils import auto_delete, DELETE_AFTER_ACK, DELETE_AFTER_NOTICE, SUPPORTED_CURRENCIES

SUBGROUP_HEADER = (
    "👥 **Your Subgroup**\n\n"
    "This is *your* default participant list for expenses you add — it "
    "doesn't affect anyone else's splits. Tap members to include/exclude "
    "them, then Save. It stays active until you reset it, even across "
    "restarts.\n\n"
)


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: Displays the main settings menu."""
    chat_id = update.effective_chat.id

    if update.message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass

    old_msg_id = context.user_data.get("settings_msg_id")
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except Exception:
            pass

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⚙️ **SplitMate Settings**\n\nConfigure your personal default group for expense splits:",
        reply_markup=settings_inline_menu,
        parse_mode="Markdown"
    )
    context.user_data["settings_msg_id"] = msg.message_id


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles actions from the main settings menu."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    if data == "settings:close":
        await query.edit_message_text("⚙️ Settings closed.")
        auto_delete(context, chat_id, query.message.message_id, delay=DELETE_AFTER_ACK)
        msg = await context.bot.send_message(chat_id=chat_id, text="🏠 **Main Menu:**", reply_markup=main_menu, parse_mode="Markdown")
        auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_ACK)
        context.user_data.pop("settings_msg_id", None)
        return

    elif data == "settings:back":
        await query.edit_message_text(
            text="⚙️ **SplitMate Settings**\n\nConfigure your personal default group for expense splits:",
            reply_markup=settings_inline_menu,
            parse_mode="Markdown",
        )
        return

    elif data == "settings:currency":
        acting_user_id = query.from_user.id

        group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat_id,), fetch=True)
        if not group:
            await query.edit_message_text("❌ Group not found.")
            return
        group_id = group[0][0]

        member = execute(
            "SELECT member_id FROM members WHERE group_id=? AND telegram_user_id=?",
            (group_id, acting_user_id),
            fetch=True,
        )
        if not member:
            await query.edit_message_text("❌ You're not registered in this group yet -- send /start first.")
            return

        current_currency = get_member_currency(member[0][0])
        context.user_data["group_id"] = group_id

        await query.edit_message_text(
            text=(
                "💱 **Your Currency**\n\n"
                "This is the currency your own expenses are entered in. "
                "Everyone else keeps splitting normally -- anyone whose currency "
                "differs from yours just also sees their share converted into "
                "theirs, frozen at the moment you add the expense.\n\n"
                f"Current: **{SUPPORTED_CURRENCIES.get(current_currency, '')} {current_currency}**"
            ),
            reply_markup=get_currency_menu(current_currency),
            parse_mode="Markdown",
        )
        return

    elif data == "settings:select_users":
        acting_user_id = query.from_user.id

        # 1. Resolve Group ID
        group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat_id,), fetch=True)
        if not group:
            await query.edit_message_text("❌ Group not found.")
            return
        group_id = group[0][0]

        # 2. Fetch the pool of selectable members: everyone currently active
        # in the group. (is_active here just means "still part of the group" --
        # it's no longer overwritten by anyone's subgroup choice.)
        members = execute(
            "SELECT member_id, display_name FROM members WHERE group_id=? AND is_active=1 ORDER BY member_id",
            (group_id,),
            fetch=True
        )
        if not members:
            await query.edit_message_text("❌ No members found in this group.")
            return

        member_list = [(m[0], m[1]) for m in members]

        # 3. Load THIS user's own saved subgroup, defaulting to "everyone"
        # if they've never saved one (or have reset it).
        selected_ids = context.user_data.get("selected_subgroup_ids")
        if selected_ids is None:
            saved = get_user_subgroup_member_ids(group_id, acting_user_id)
            selected_ids = saved if saved is not None else [m[0] for m in member_list]
            context.user_data["selected_subgroup_ids"] = selected_ids

        context.user_data["group_id"] = group_id
        context.user_data["members_cache"] = member_list

        await query.edit_message_text(
            text=SUBGROUP_HEADER,
            reply_markup=get_member_selection_menu(member_list, selected_ids),
            parse_mode="Markdown"
        )


async def subgroup_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles individual member selection states."""
    query = update.callback_query
    await query.answer()

    data = query.data
    member_id = int(data.split(":")[1])

    selected_ids = context.user_data.get("selected_subgroup_ids", [])
    if member_id in selected_ids:
        selected_ids.remove(member_id)
    else:
        selected_ids.append(member_id)

    context.user_data["selected_subgroup_ids"] = selected_ids
    members = context.user_data.get("members_cache", [])

    await query.edit_message_text(
        text=SUBGROUP_HEADER,
        reply_markup=get_member_selection_menu(members, selected_ids),
        parse_mode="Markdown"
    )


async def subgroup_reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resets subgroup selection to include all members."""
    query = update.callback_query
    await query.answer("🔄 Reset to all members selected.")

    members = context.user_data.get("members_cache", [])
    selected_ids = [m[0] for m in members]
    context.user_data["selected_subgroup_ids"] = selected_ids

    await query.edit_message_text(
        text=SUBGROUP_HEADER,
        reply_markup=get_member_selection_menu(members, selected_ids),
        parse_mode="Markdown"
    )


async def subgroup_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Persists the ACTING user's own subgroup selection -- affects only their future expenses."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    acting_user_id = query.from_user.id

    selected_ids = context.user_data.get("selected_subgroup_ids", [])
    group_id = context.user_data.get("group_id")
    members = context.user_data.get("members_cache", [])

    if not selected_ids:
        await query.answer("❌ You must select at least one member!", show_alert=True)
        return

    if group_id:
        all_member_ids = {m[0] for m in members}
        if set(selected_ids) >= all_member_ids:
            # Selection covers everyone currently in the group -- treat this
            # as "no subgroup" rather than freezing today's member list, so
            # anyone who joins later is automatically included too.
            reset_user_subgroup(group_id, acting_user_id)
        else:
            save_user_subgroup(group_id, acting_user_id, selected_ids)

    await query.answer("✅ Subgroup saved successfully!", show_alert=True)
    await query.edit_message_text("✅ **Your subgroup is saved!** It'll be used as the default participant list for expenses you add, until you reset it.")
    auto_delete(context, chat_id, query.message.message_id, delay=DELETE_AFTER_NOTICE)

    msg = await context.bot.send_message(chat_id=chat_id, text="🏠 **Main Menu:**", reply_markup=main_menu, parse_mode="Markdown")
    auto_delete(context, chat_id, msg.message_id, delay=DELETE_AFTER_NOTICE)
    context.user_data.clear()


async def currency_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the acting user's personal currency (for this group) from the
    Currency picker. New expenses they pay for from now on are recorded in
    this currency; past expenses/history are untouched (see
    stamp_expense_currency, which freezes the currency at creation time)."""
    query = update.callback_query
    acting_user_id = query.from_user.id
    chat_id = update.effective_chat.id

    currency = query.data.split(":", 1)[1]
    if currency not in SUPPORTED_CURRENCIES:
        await query.answer("❌ Unsupported currency.", show_alert=True)
        return

    group_id = context.user_data.get("group_id")
    if not group_id:
        group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat_id,), fetch=True)
        if not group:
            await query.answer("❌ Group not found.", show_alert=True)
            return
        group_id = group[0][0]

    set_member_currency(group_id, acting_user_id, currency)
    await query.answer(f"✅ Currency set to {currency}")

    await query.edit_message_text(
        text=(
            "💱 **Your Currency**\n\n"
            f"Updated -- your expenses are now entered in **{SUPPORTED_CURRENCIES[currency]} {currency}**."
        ),
        reply_markup=get_currency_menu(currency),
        parse_mode="Markdown",
    )