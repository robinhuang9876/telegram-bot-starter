#!/usr/bin/env python
import asyncio
import time
import logging
import httpx
import os
from functools import wraps
from difflib import SequenceMatcher
from collections import defaultdict
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.error import NetworkError

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Configuration ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")

SIMILARITY_THRESHOLD = 0.65
NIGHT_MODE_ENABLED = True
MESSAGE_LIMIT = 5
TIME_WINDOW = 30  # seconds

# Spam keywords (can be moved to external DB later)
SPAM_KEYWORDS = [
    "buy now", "discount", "limited offer", "click here",
    "make money", "earn cash now", "investment", "free gift",
    "提奔驰", "开宝马", "看竹叶", "看我筑夜", "煮叶进",
    "下个月让你", "两个月后直接", "安排到位", "稳定长久",
    "安全无忧", "多个社区多个机会", "随意交流", "靠普能干事的兄弟",
    "说到做到", "给你安排到位", "不妨来看看",
    "加微信", "加V", "加薇", "加我", "私聊",
    "赚钱项目", "高回报", "稳赚不赔", "兼职",
    "内部渠道", "特殊资源", "独家代理"
]

# --- Helper Functions ---
def retry_on_network_error(max_retries=10, initial_delay=5, backoff_factor=2):
    """Decorator for retrying network operations"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            delay = initial_delay
            last_error = None
            
            while retries < max_retries:
                try:
                    return await func(*args, **kwargs)
                except (httpx.NetworkError, NetworkError) as e:
                    last_error = e
                    retries += 1
                    if retries < max_retries:
                        logger.warning(f"Network error (attempt {retries}/{max_retries}): {e}")
                        await asyncio.sleep(delay)
                        delay *= backoff_factor
                except Exception as e:
                    logger.error(f"Unexpected error: {e}")
                    raise
            raise last_error
        return wrapper
    return decorator

# --- Spam Detection Logic ---
user_messages = defaultdict(list)
recent_actions = []
missed_ads = []

def is_similar(text1, text2):
    return SequenceMatcher(None, text1.lower(), text2.lower()).ratio() >= SIMILARITY_THRESHOLD

def contains_spam_keywords(text):
    text_lower = text.lower()
    matches = [kw for kw in SPAM_KEYWORDS if kw in text_lower or is_similar(text_lower, kw)]
    return matches if matches else False

def is_high_frequency(user_id, message_text):
    now = time.time()
    user_messages[user_id] = [t for t in user_messages[user_id] if now - t[0] <= TIME_WINDOW]
    similar_count = sum(1 for _, msg in user_messages[user_id] if is_similar(msg, message_text))
    user_messages[user_id].append((now, message_text))
    return similar_count >= MESSAGE_LIMIT

def is_night_time():
    beijing_tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(beijing_tz)
    return now.hour >= 23 or now.hour < 7

# --- Telegram Handlers ---
@retry_on_network_error()
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str = "delete"):
    if NIGHT_MODE_ENABLED and is_night_time():
        try:
            await update.effective_message.delete()
            return
        except Exception as e:
            logger.error(f"Night mode delete failed: {e}")

    message = update.effective_message
    if not message.text and not message.caption:
        return

    text = message.text or message.caption
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name or ""

    if contains_spam_keywords(text) or is_high_frequency(user_id, text):
        try:
            await message.delete()
            
            if action == "mute":
                await context.bot.restrict_chat_member(
                    chat_id=message.chat_id,
                    user_id=user_id,
                    permissions=ChatPermissions(can_send_messages=False)
                )
            elif action == "kick":
                await context.bot.ban_chat_member(
                    chat_id=message.chat_id,
                    user_id=user_id,
                    until_date=int(time.time()) + 60
                )
            
            warning_msg = await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"@{username} ⚠️ 消息已删除（广告检测）"
            )
            asyncio.create_task(delete_message_after(warning_msg, 14*60))
            
        except Exception as e:
            logger.error(f"Failed to handle spam: {e}")

async def delete_message_after(message, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"Failed to delete warning message: {e}")

# --- Command Handlers ---
async def reload_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reload spam keywords from DB (placeholder)"""
    await update.message.reply_text("✅ 关键词列表已刷新（模拟）")

async def night_mode_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global NIGHT_MODE_ENABLED
    NIGHT_MODE_ENABLED = True
    await update.message.reply_text("🌙 夜间模式已开启")

async def night_mode_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global NIGHT_MODE_ENABLED
    NIGHT_MODE_ENABLED = False
    await update.message.reply_text("☀️ 夜间模式已关闭")

# --- Main Application ---
def setup_application():
    application = ApplicationBuilder() \
        .token(TOKEN) \
        .read_timeout(30) \
        .write_timeout(30) \
        .connect_timeout(30) \
        .pool_timeout(30) \
        .build()

    # Command handlers
    application.add_handler(CommandHandler("reload_keywords", reload_keywords))
    application.add_handler(CommandHandler("nightmode_on", night_mode_on))
    application.add_handler(CommandHandler("nightmode_off", night_mode_off))
    
    # Message handler
    application.add_handler(MessageHandler(
        filters.TEXT | filters.CAPTION,
        lambda update, ctx: handle_message(update, ctx, action="delete"))
    
    return application

def main():
    app = setup_application()
    
    if os.getenv("RAILWAY_ENVIRONMENT") == "production":
        PORT = int(os.getenv("PORT", 8443))
        WEBHOOK_URL = os.getenv("WEBHOOK_URL")
        
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}"
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
