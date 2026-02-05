import os
import asyncio
import logging
import re
import time
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

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# =========================
# UTILITIES & LOGIC
# =========================

def get_lot_size(symbol):
    """Returns accurate lot sizes for Feb 2026."""
    s = symbol.upper()
    if "BANKNIFTY" in s: return 30
    if "HDFCBANK" in s: return 550
    if "ICICIBANK" in s: return 700
    if "NIFTY" in s and "BANK" not in s: return 75
    return 1 

def get_moneyness(symbol, fut_price):
    """Calculates ITM, ATM, or OTM for Options."""
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

def identify_participant(text):
    t = text.upper()
    if "SHORT COVERING" in t: return "SHORT COVERING ↗️"
    if "LONG UNWINDING" in t: return "UNWINDING ⤵️"
    if "WRITER" in t or "SHORT BUILDUP" in t: return "WRITER ✍️"
    if "BUYER" in t or "LONG BUILDUP" in t: return "BUYER 🔵"
    return "ACTION"

def summarize_alerts(alerts):
    passed = []
    p_sym = re.compile(r"Symbol\s*:\s*(.*?)\n", re.IGNORECASE)
    p_oi = re.compile(r"OI CHANGE\s*:\s*([+-]?[0-9,]+)", re.IGNORECASE)
    p_pr = re.compile(r"PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)
    p_fut = re.compile(r"FUT PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)

    for alert in alerts:
        try:
            s_m, oi_m, pr_m, f_m = p_sym.search(alert), p_oi.search(alert), p_pr.search(alert), p_fut.search(alert)
            if not (s_m and oi_m and pr_m): continue

            symbol = s_m.group(1).strip()
            price = float(pr_m.group(1))
            oi_val = abs(int(oi_m.group(1).replace(",", "")))
            lot_size = get_lot_size(symbol)
            num_lots = oi_val / lot_size
            
            # --- CUSTOM TURNOVER LOGIC ---
            if "-I" in symbol or "FUT" in symbol.upper():
                # Future: 1 Lot = 100,000 Turnover (Ignoring Price)
                turnover = num_lots * 100000 
            else:
                # Option: Price * Qty (OI Value)
                turnover = oi_val * price

            # --- 1 CRORE FILTER ---
            if turnover < 10000000: continue

            turnover_cr = turnover / 10000000
            action = identify_participant(alert)
            
            money_tag = ""
            fut_display = ""
            if f_m:
                f_price = float(f_m.group(1))
                if "CE" in symbol or "PE" in symbol:
                    m_status = get_moneyness(symbol, f_price)
                    money_tag = f" | **{m_status}**" if m_status else ""
                fut_display = f"\n🔹 **Fut Price: {f_price}**"

            passed.append(
                f"🏷 **{symbol}**\n"
                f"⚡ **{action}**{money_tag}\n"
                f"💰 **Turnover: ₹{turnover_cr:.2f} Cr**\n"
                f"📦 Lots: {int(num_lots)} (Qty: {oi_val})\n"
                f"📊 Price: {price}{fut_display}"
            )
        except Exception as e:
            logger.error(f"Error: {e}")
            continue
            
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
            logger.info("Bot is starting polling...")
            app.run_polling()
        except Conflict:
            time.sleep(15)
        except Exception as e:
            time.sleep(5)
