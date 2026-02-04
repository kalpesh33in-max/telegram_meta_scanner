import os
import asyncio
import logging
import re
import time
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.error import Conflict, TelegramError

# =========================
# LOGGING SETUP
# =========================
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================
# ENV & CONFIG
# =========================
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
    TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
    # Set to 5-10 seconds for "Fast" updates
    AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))
except KeyError as e:
    logger.critical(f"Missing Env Var: {e}")
    raise SystemExit

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# =========================
# EXTRACTION LOGIC
# =========================
def get_moneyness(symbol, fut_price):
    try:
        strike_match = re.search(r"(\d{5,7})", symbol)
        if not strike_match: return ""
        strike = float(strike_match.group(1))
        opt_type = "CE" if "CE" in symbol else "PE"
        if abs(strike - fut_price) <= (fut_price * 0.0015): return "ATM"
        if opt_type == "CE":
            return "ITM" if strike < fut_price else "OTM"
        return "ITM" if strike > fut_price else "OTM"
    except: return ""

def process_alerts(alerts):
    passed = []
    # Patterns matching your GDFL_RAW_ALERTS format
    p_act = re.compile(r"🚨 (.*?)\n")
    p_sym = re.compile(r"Symbol:\n(.*?)\n")
    p_lot = re.compile(r"LOTS:\s*(\d+)")
    p_pr = re.compile(r"PRICE:\s*([\d\.]+)")
    p_fut = re.compile(r"FUTURE PRICE:\s*([\d\.]+)")
    p_oi = re.compile(r"OI CHANGE\s+:\s*([+-]?[0-9,]+)")

    for alert in alerts:
        try:
            s_m, oi_m, pr_m, f_m = p_sym.search(alert), p_oi.search(alert), p_pr.search(alert), p_fut.search(alert)
            if not (s_m and oi_m and pr_m): continue

            symbol = s_m.group(1).strip()
            price = float(pr_m.group(1))
            oi_val = int(oi_m.group(1).replace(",", ""))
            turnover = abs(oi_val * price)

            # --- 1 CRORE FILTER ---
            if turnover < 10000000: continue

            turnover_cr = turnover / 10000000
            action = p_act.search(alert).group(1).strip() if p_act.search(alert) else "SIGNAL"
            lots = p_lot.search(alert).group(1) if p_lot.search(alert) else "N/A"
            money = f"| **{get_moneyness(symbol, float(f_m.group(1)))}**" if f_m else ""

            passed.append(
                f"🏷 **{symbol}**\n"
                f"⚡ **{action}** {money}\n"
                f"💰 **Turnover: ₹{turnover_cr:.2f} Cr**\n"
                f"📦 **Lots:** {lots} | **OI Chg:** {oi_val:,}\n"
                f"📊 Price: {price}"
            )
        except: continue
    return "\n\n---\n\n".join(passed)

# =========================
# TELEGRAM HANDLERS
# =========================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.channel_post or update.message
    if m and str(m.chat.id) == str(SOURCE_CHAT_ID) and m.text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(m.text)

async def aggregation_task(app):
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
    """This runs the MOMENT the bot connects to Telegram."""
    # 1. Send the Start Message immediately
    try:
        await app.bot.send_message(
            chat_id=TARGET_CHAT_ID, 
            text="🚀 **Scanner is Start**\n✅ Monitoring for > 1 Cr Alerts",
            parse_mode="Markdown"
        )
        logger.info("Startup alert sent to Telegram.")
    except Exception as e:
        logger.error(f"Could not send startup message: {e}")
    
    # 2. Start the background monitoring task
    asyncio.create_task(aggregation_task(app))

# =========================
# MAIN LOOP
# =========================
if __name__ == "__main__":
    while True:
        try:
            # Building the application with the startup message in post_init
            app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
            app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
            
            logger.info("Starting Polling...")
            # drop_pending_updates=True prevents the bot from crashing on old data
            app.run_polling(drop_pending_updates=True) 
            
        except Conflict:
            logger.warning("Conflict detected (Old instance running). Waiting 10s...")
            time.sleep(10)
        except Exception as e:
            logger.error(f"Error: {e}")
            time.sleep(5)
