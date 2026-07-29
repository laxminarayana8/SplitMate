from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE = "data/splitmate.db"

CATEGORIES = [
    "🍔 Food",
    "🛒 Grocery",
    "⛽ Fuel",
    "⚡ Electricity",
    "🏠 Rent",
    "🚕 Travel",
    "🎬 Entertainment",
    "📦 Other"
]

SPLITS = [
    "⚖️ Equal",
    "📐 Ratio",
    "💵 Exact Amount",
    "👤 Personal"
]