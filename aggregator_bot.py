import os, asyncio, logging, re, time, csv
import pyotp
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.error import Conflict
from neo_api_client import NeoAPI

# ================= CONFIG =================
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))

# Broker credentials
NEO_CONSUMER_KEY = os.getenv("NEO_CONSUMER_KEY")
NEO_ID = os.getenv("NEO_ID")
NEO_PASSWORD = os.getenv("NEO_PASSWORD")
NEO_TOTP_SECRET = os.getenv("NEO_TOTP_SECRET")

neo = None  # will initialize after login

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

active_trade = {"symbol": None, "entry": 0, "type": None, "qty": 30}
active_watch = {"symbol": None, "alert_price": 0, "timestamp": 0, "type": None}

# ================= SCANNER =================

def identify_participant(text):
    t = text.upper()
    if "SHORT COVERING" in t: return "SHORT COVERING ↗️"
    if "UNWINDING" in t: return "UNWINDING ⤵️"
    if "WRITER" in t or "SHORT BUILDUP" in t: return "WRITER ✍️"
    if "BUYER" in t or "LONG BUILDUP" in t: return "BUYER 🔵"
    return "ACTION"

def summarize_alerts(alerts):
    passed = []
    p_sym = re.compile(r"Symbol\s*:\s*(.*?)\n", re.IGNORECASE)
    p_pr = re.compile(r"PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)
    p_oi = re.compile(r"OI CHANGE\s*:\s*([+-]?[0-9,]+)", re.IGNORECASE)

    for alert in alerts:
        try:
            s_m, pr_m, oi_m = p_sym.search(alert), p_pr.search(alert), p_oi.search(alert)
            if not (s_m and pr_m): continue

            symbol = s_m.group(1).strip()
            price = float(pr_m.group(1))
            action = identify_participant(alert)

            oi_val = abs(int(oi_m.group(1).replace(",", ""))) if oi_m else 0
            turnover = oi_val * price

            if turnover >= 10000000:
                passed.append(f"🏷 **{symbol}**\n⚡ **{action}**\n💰 **₹{turnover/1e7:.2f} Cr**\n📊 Price: {price}")

                active_watch.update({
                    "symbol": symbol,
                    "alert_price": price,
                    "timestamp": time.time(),
                    "type": "CE" if "CE" in symbol else "PE"
                })
        except:
            continue

    return "\n\n---\n\n".join(passed)

# ================= BROKER LTP =================

async def get_live_price(symbol):
    try:
        quote = neo.quotes(symbols=[{'symbol': symbol, 'exchange': 'NSE'}])
        return float(quote['data'][0]['last_price'])
    except Exception as e:
        logger.error(f"LTP Error: {e}")
        return None

def log_trade(action, symbol, price, pnl=0):
    with open("paper_trade_log.csv", "a", newline="") as f:
        csv.writer(f).writerow([time.ctime(), action, symbol, price, pnl])

# ================= PAPER ENGINE =================

async def trading_engine():
    global active_trade, active_watch

    while True:
        await asyncio.sleep(2)

        if active_watch["symbol"]:
            ltp = await get_live_price(active_watch["symbol"])
            if ltp and abs(ltp - active_watch["alert_price"]) >= 20:
                active_trade = {"symbol": active_watch["symbol"], "entry": ltp, "type": active_watch["type"], "qty": 30}
                log_trade("ENTER", active_trade["symbol"], ltp)
                active_watch["symbol"] = None

        if active_trade["symbol"]:
            ltp = await get_live_price(active_trade["symbol"])
            if ltp:
                pnl = ltp - active_trade["entry"]
                if pnl >= 40 or pnl <= -40:
                    log_trade("EXIT", active_trade["symbol"], ltp, pnl)
                    active_trade = {"symbol": None, "entry": 0, "type": None}

# ================= TELEGRAM =================

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
            batch = list(MESSAGE_BUFFER)
            MESSAGE_BUFFER.clear()

        summary = summarize_alerts(batch)
        if summary:
            await app.bot.send_message(TARGET_CHAT_ID, summary, parse_mode="Markdown")

async def post_init(app):
    global neo
    neo = NeoAPI(consumer_key=NEO_CONSUMER_KEY, environment='prod')
    totp = pyotp.TOTP(NEO_TOTP_SECRET).now()
    neo.login(password=NEO_PASSWORD)
    neo.allow_2fa(token=totp)
    logger.info("Kotak Neo login successful")

    asyncio.create_task(aggregation_task(app))
    asyncio.create_task(trading_engine())

# ================= MAIN =================

if __name__ == "__main__":
    while True:
        try:
            app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
            app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.TEXT & (~filters.COMMAND), message_handler))
            app.run_polling()
        except Conflict:
            time.sleep(15)
        except Exception:
            time.sleep(5)
