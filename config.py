from dotenv import load_dotenv
import os

load_dotenv()

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Anthropic API Key (used to read payment amounts off receipt screenshots)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# SQLite Database
DATABASE_NAME = "data/splitmate.db"

# -----------------------------
# Subscription (Pro Plan) -- Razorpay Payment Links, direct INR
# -----------------------------
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")  # set when configuring the webhook in the Razorpay dashboard

SUBSCRIPTION_PRICE_INR = 99      # <-- set this to whatever you want to charge, in rupees
SUBSCRIPTION_DAYS = 30           # length of one purchase, in days
SUBSCRIPTION_PLAN_NAME = "pro"

# Public HTTPS base URL where Razorpay can reach your webhook (behind
# Nginx/certbot -- see deployment guide). NOT localhost/an internal IP.
WEBHOOK_PUBLIC_URL = os.getenv("WEBHOOK_PUBLIC_URL", "")   # e.g. https://splitmate.yourdomain.com
WEBHOOK_LISTEN_PORT = int(os.getenv("WEBHOOK_LISTEN_PORT", "8000"))

# Expense Categories
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

# Split Types
SPLIT_TYPES = [
    "⚖️ Equal",
    "🧮 Equally Among",
    "💵 Exact Amount",
    "👤 Personal"
]