import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "9143623235:AAHG-tXzwP-rQnYxBo02mTIntIBtx9ltIlM")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://news-pro-5cab.onrender.com/webhook")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is missing.")

response = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={WEBHOOK_URL}")
print("Telegram Webhook Response:", response.json())
