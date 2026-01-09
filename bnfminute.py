from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
import os

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

async def debug_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("CHAT ID =", update.effective_chat.id)

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.ALL, debug_chat_id))
app.run_polling()
