import os
import asyncio
import logging
import re
import time
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.error import Conflict, TelegramError

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
    TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
    AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))
except KeyError as e:
    logger.critical(f"Missing Env Var: {e}")
    raise SystemExit

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# --- Utility Functions ---
def get_moneyness(symbol, fut_price):
    try:
        strike_match = re.search(r"(\d{5,6})", symbol)
        if not strike_match: return ""
        strike = float(strike_match.group(1))
        opt_type = "CE" if "CE" in symbol else "PE"
        if abs(strike - fut_price) <= (fut_price * 0.0015): return "ATM"
        if opt_type == "CE":
            return "ITM" if strike < fut_price else "OTM"
        return "ITM" if strike > fut_price else "OTM"
    except: return ""

def identify_action(text):
    t = text.upper()
    if "SHORT COVERING" in t: return "SHORT COVERING ↗️"
    if "LONG UNWINDING" in t: return "UNWINDING ⤵️"
    if "WRITER" in t or "SHORT BUILDUP" in t: return "WRITER ✍️"
    if "BUYER" in t or "LONG BUILDUP" in t: return "BUYER 🔵"
    return "SIGNAL"

def process_alerts(alerts):
    passed = []
    p_sym = re.compile(r"Symbol: (.*?)\n")
    p_oi = re.compile(r"OI CHANGE\s+:\s*([+-]?[0-9,]+)")
    p_pr = re.compile(r"PRICE:\s*([\d\.]+)")
    p_fut = re.compile(r"FUT PRICE:\s*([\d\.]+)")

    for alert in alerts:
        try:
            s_m, oi_m, pr_m, f_m = p_sym.search(alert), p_oi.search(alert), p_pr.search(alert), p_fut.search(alert)
            if not (s_m and oi_m and pr_m): continue
            symbol, price, oi_val = s_m.group(1).strip(), float(pr_m.group(1)), int(oi_m.group(1).replace(",", ""))
            turnover = abs(oi_val * price)
            if turnover < 10000000: continue
            
            turnover_cr = turnover / 10000000
            action = identify_action(alert)
            money = f"| **{get_moneyness(symbol, float(f_m.group(1)))}**" if f_m else ""

            passed.append(f"🏷 **{symbol}**\n⚡ **{action}** {money}\n💰 **Turnover: ₹{turnover_cr:.2f} Cr**\n📊 Price: {price}")
        except: continue
    return "\n\n---\n\n".join(passed)

# --- Bot Logic ---
async def message_handler(update, context):
    m = update.channel_post or update.message
    if m and str(m.chat.id) == str(SOURCE_CHAT_ID) and m.text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(m.text)

async def aggregation_task(app):
    # Startup Message
    try:
        await app.bot.send_message(TARGET_CHAT_ID, "🚀 **Scanner is LIVE & Monitoring > 1 Cr Alerts**", parse_mode="Markdown")
    except: pass

    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL)
        async with BUFFER_LOCK:
            if not MESSAGE_BUFFER: continue
            batch = list(MESSAGE_BUFFER); MESSAGE_BUFFER.clear()
        summary = process_alerts(batch)
        if summary:
            try:
                await app.bot.send_message(TARGET_CHAT_ID, summary, parse_mode="Markdown")
            except: pass

async def post_init(app):
    asyncio.create_task(aggregation_task(app))

if __name__ == "__main__":
    while True:
        try:
            app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
            app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
            app.run_polling()
        except Conflict:
            logger.warning("Conflict detected! Another instance is running. Retrying in 10s...")
            time.sleep(10)
        except Exception as e:
            logger.error(f"Error: {e}")
            time.sleep(5)
