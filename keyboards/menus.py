from telegram import ReplyKeyboardMarkup

main_menu = ReplyKeyboardMarkup(
    [
        ["➕ Add Expense"],
        ["💰 Balance", "📜 History"],
        ["📊 Monthly Summary", "🤝 Settle Up"],
        ["⚙️ Settings"]
    ],
    resize_keyboard=True
)
category_menu = ReplyKeyboardMarkup(
[
["🍔 Food","🛒 Grocery"],
["⛽ Fuel","⚡ Electricity"],
["🏠 Rent","🚕 Travel"],
["🎬 Entertainment","📦 Other"]
],
resize_keyboard=True
)
split_menu = ReplyKeyboardMarkup(
    [
        ["⚖️ Equal"],
        ["📐 Ratio"],
        ["💵 Exact Amount"],
        ["👤 Personal"]
    ],
    resize_keyboard=True
)