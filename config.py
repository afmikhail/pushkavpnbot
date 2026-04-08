import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "8531605953:AAHQqfMr2gI0LSP9iKnz6JH9Hft5WmnsBNo")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://afmikhail.github.io/pushkavpnbot")

# Firebase
FIREBASE_CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH", "firebase-credentials.json")
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "https://pushkavpnbot-default-rtdb.europe-west1.firebasedatabase.app")

# 3x-ui panel
XUI_URL = os.getenv("XUI_URL", "https://158.160.7.201:26989/4UZ4HUosd4tHF3Snfo")
XUI_USERNAME = os.getenv("XUI_USERNAME", "eqYDz5hb6Y")
XUI_PASSWORD = os.getenv("XUI_PASSWORD", "xXtQ6v7W3J")
XUI_INBOUND_ID = int(os.getenv("XUI_INBOUND_ID", "3"))  # ID вашего VLESS inbound

# v-kassa payment (старый метод, оставлен для совместимости)
VKASSA_SHOP_ID = os.getenv("VKASSA_SHOP_ID", "454636")
VKASSA_SECRET_KEY = os.getenv("VKASSA_SECRET_KEY", "live_AUbzb8OgeJlXSXdUcdS9XLAgQ1TL5qVPND03P7-x2eE")
VKASSA_API_URL = "https://api.v-kassa.ru/v2"

# ЮKassa через Telegram Payments (BotFather → Payments → ЮKassa)
YUKASSA_PROVIDER_TOKEN = os.getenv("YUKASSA_PROVIDER_TOKEN", "390540012:LIVE:92627")

# Plans config
PLANS = {
    "month": {
        "name": "📆 Месячный",
        "days": 30,
        "price": 250,
        "traffic_gb": 50,
        "devices": 2,
    },
    "year": {
        "name": "📅 Годовой",
        "days": 365,
        "price": 2490,
        "traffic_gb": 600,
        "devices": 2,
    },
}

# Trial config
TRIAL_DAYS = 1
TRIAL_TRAFFIC_GB = 5

# Referral
REFERRAL_COMMISSION_PERCENT = 40
REFERRAL_MIN_PAYOUT = 100  # rubles

# Admin
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]
