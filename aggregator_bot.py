import os
import asyncio
import logging
import re
import time
from datetime import datetime
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.error import Conflict

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
    AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))
except KeyError as e:
    logger.critical(f"Missing Env Var: {e}")
    raise SystemExit

# Updated Thresholds based on your requirements
FUTURE_THRESHOLD = 60000000       # 6 Crore
WRITER_SC_THRESHOLD = 10000000    # 1 Crore
BUYER_UW_THRESHOLD = 10000000     # 1 Crore

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# =========================
# UTILITIES & LOGIC
# =========================

def is_market_hours():
    """Checks if current time is between 09:30 and 15:00 IST."""
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist).time()
    start = datetime.strptime("09:30", "%H:%M").time()
    end = datetime.strptime("15:00", "%H:%M").time()
    return start <= now <= end

def get_lot_size(symbol):
    """Returns accurate lot sizes for April 2026."""
    s = symbol.upper().replace(" ", "")
    if "BANKNIFTY" in s: return 30
    return 1 

def classify_strike(strike, option_type, future_price):
    try:
        strike = float(strike)
        future_price = float(future_price)
        if option_type == "CE":
            return "ITM" if strike < future_price else "OTM"
        elif option_type == "PE":
            return "ITM" if strike > future_price else "OTM"
    except: pass
    return "N/A"

def identify_participant(text):
    t = text.upper()
    if "SHORT COVERING" in t: return "SHORT COVERING ↗️"
    if "LONG UNWINDING" in t: return "UNWINDING ⤵️"
    if "WRITER" in t or "SHORT BUILDUP" in t or "FUTURE SELL" in t: return "WRITER ✍️"
    if "BUYER" in t or "LONG BUILDUP" in t or "FUTURE BUY" in t: return "BUYER 🔵"
    return "ACTION"

def summarize_alerts(alerts):
    passed = []
    p_sym = re.compile(r"Symbol\s*:\s*(.*?)(?:\n|$)", re.IGNORECASE)
    p_oi = re.compile(r"OI CHANGE\s*:\s*([+-]?[0-9,]+)", re.IGNORECASE)
    p_pr = re.compile(r"PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)
    p_fut = re.compile(r"FUTURE PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)

    for alert in alerts:
        try:
            # ONLY PROCESS BANKNIFTY
            if "BANKNIFTY" not in alert.upper():
                continue

            s_m, oi_m, pr_m, f_m = p_sym.search(alert), p_oi.search(alert), p_pr.search(alert), p_fut.search(alert)
            if not (s_m and oi_m and pr_m and f_m): continue

            symbol = s_m.group(1).strip()
            price = float(pr_m.group(1))
            future_price = float(f_m.group(1))
            oi_val = abs(int(oi_m.group(1).replace(",", "")))
            
            lot_size = get_lot_size(symbol)
            num_lots = oi_val / lot_size
            action = identify_participant(alert)

            # --- ITM Logic with Difference ---
            zone_label = ""
            if "FUT" not in symbol.upper():
                strike_m = re.search(r"(\d+)(CE|PE)$", symbol.upper())
                if strike_m:
                    strike_val = float(strike_m.group(1))
                    option_type = strike_m.group(2)
                    zone = classify_strike(strike_val, option_type, future_price)
                    
                    # Calculate Difference
                    diff = round(abs(strike_val - future_price), 2)
                    
                    # FILTER: Only ITM and Difference > 500
                    if zone == "ITM" and diff > 500:
                        zone_label = f" ({zone}-{diff}-diff)"
                    else:
                        continue # Skip Near ITM or OTM
                else:
                    continue # Skip if not an option
            else:
                continue # Skip Futures as per "only banknify option data need"

            # --- TURNOVER CALCULATION ---
            if "WRITER" in action or "SHORT COVERING" in action:
                turnover = num_lots * 90000
                current_threshold = WRITER_SC_THRESHOLD
            else:
                turnover = oi_val * price
                current_threshold = BUYER_UW_THRESHOLD

            if turnover < current_threshold:
                continue

            turnover_cr = turnover / 10000000
            
            passed.append(
                f"🏷 **{symbol}{zone_label}**\n"
                f"⚡ **{action}**\n"
                f"💰 **Turnover: ₹{turnover_cr:.2f} Cr**\n"
                f"📦 Lots: {int(num_lots)} (Qty: {oi_val})\n"
                f"📊 Price: {price}\n"
                f"🔹 **Fut Price: {future_price}**"
            )
        except Exception as e:
            logger.error(f"Error processing alert: {e}")
            continue
            
    return "\n\n---\n\n".join(passed)

# =========================
# TELEGRAM HANDLERS
# =========================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only buffer messages during market hours
    if not is_market_hours():
        return

    m = update.channel_post or update.message
    if m and str(m.chat_id) == str(SOURCE_CHAT_ID) and m.text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(m.text)

async def aggregation_task(app):
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL)
        
        # Only process buffer during market hours
        if not is_market_hours():
            async with BUFFER_LOCK:
                MESSAGE_BUFFER.clear() # Keep buffer empty outside hours
            continue

        async with BUFFER_LOCK:
            if not MESSAGE_BUFFER: continue
            batch = list(MESSAGE_BUFFER); MESSAGE_BUFFER.clear()
        
        summary = summarize_alerts(batch)
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
            logger.info("Bot starting with 6Cr/3Cr/1Cr thresholds...")
            app.run_polling()
        except Conflict:
            time.sleep(15)
        except Exception as e:
            time.sleep(5)
