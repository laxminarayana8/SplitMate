from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from config import CATEGORIES, SPLIT_TYPES


def chunk_list_inline(items, callback_prefix, chunk_size=3):
    """Utility function to convert a list into rows of InlineButtons."""
    grid = []
    for i in range(0, len(items), chunk_size):
        row = [
            InlineKeyboardButton(text=item, callback_data=f"{callback_prefix}:{item}")
            for item in items[i : i + chunk_size]
        ]
        grid.append(row)
    return grid


# -------------------------
# Main Menu (ReplyKeyboard kept for bottom control bar)
# -------------------------
main_menu = ReplyKeyboardMarkup(
    [
        ["➕ Add Expense"],
        ["💰 Balance", "📜 History", "📊 Monthly Summary"],
        ["🤝 Settle Up", "🤝 Request Settlement", "⚙️ Settings"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


# -------------------------
# Inline Category Menu
# -------------------------
category_inline_menu = InlineKeyboardMarkup(
    chunk_list_inline(CATEGORIES, "cat", 3)
    + [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]]
)

# Alias for backward compatibility with edit/other handlers
category_menu = category_inline_menu


# -------------------------
# Inline Description / Skip Menu
# -------------------------
description_inline_menu = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("⏭️ Skip", callback_data="desc:skip"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense"),
        ]
    ]
)


# -------------------------
# Inline Split Type Menu
# -------------------------
split_inline_menu = InlineKeyboardMarkup(
    chunk_list_inline(SPLIT_TYPES, "split", 2)
    + [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]]
)


# -------------------------
# Inline History Filter Menu
# -------------------------
history_filter_inline_menu = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🔟 Last 10", callback_data="hist:last10"),
        InlineKeyboardButton("👤 My History", callback_data="hist:user"),
    ],
    [
        InlineKeyboardButton("📅 Last 1 Month", callback_data="hist:month"),
        InlineKeyboardButton("🌐 Whole History", callback_data="hist:all"),
    ],
    [
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_history")
    ]
])


# -------------------------
# Inline Settings & Subgroup Menus (NEW)
# -------------------------
settings_inline_menu = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Select Users (Subgroup)", callback_data="settings:select_users")],
    [InlineKeyboardButton("❌ Close", callback_data="settings:close")]
])


def get_member_selection_menu(members, selected_ids):
    """Generates an inline toggle grid for selecting subgroup members.

    Column count scales with group size so a 10+ member group doesn't turn
    into a tall wall of one-per-row buttons: 1 column for tiny groups, 2 for
    medium, 3 once it gets large.
    """
    if len(members) >= 9:
        columns = 3
    elif len(members) >= 4:
        columns = 2
    else:
        columns = 1

    buttons = []
    for member_id, display_name in members:
        is_selected = member_id in selected_ids
        icon = "✅" if is_selected else "➕"
        buttons.append(
            InlineKeyboardButton(
                text=f"{icon} {display_name}",
                callback_data=f"subgroup_toggle:{member_id}"
            )
        )

    grid = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]

    grid.append([
        InlineKeyboardButton("💾 Save Subgroup", callback_data="subgroup_save"),
        InlineKeyboardButton("🔄 Reset to All", callback_data="subgroup_reset")
    ])
    grid.append([InlineKeyboardButton("❌ Cancel", callback_data="settings:close")])
    return InlineKeyboardMarkup(grid)


def get_equal_among_menu(members, selected_ids):
    """Inline toggle grid for picking exactly who this ONE expense should be
    split equally among -- a one-off choice for this transaction only, not
    saved anywhere (unlike the subgroup settings menu above)."""
    if len(members) >= 9:
        columns = 3
    elif len(members) >= 4:
        columns = 2
    else:
        columns = 1

    buttons = []
    for member_id, display_name in members:
        is_selected = member_id in selected_ids
        icon = "✅" if is_selected else "➕"
        buttons.append(
            InlineKeyboardButton(
                text=f"{icon} {display_name}",
                callback_data=f"eq_among_toggle:{member_id}"
            )
        )

    grid = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]

    grid.append([InlineKeyboardButton("▶️ Continue", callback_data="eq_among_continue")])
    grid.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")])
    return InlineKeyboardMarkup(grid)