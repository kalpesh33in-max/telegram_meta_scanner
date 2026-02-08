import os, asyncio, logging, re, time, csv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.error import Conflict

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# ---------------- SCANNER LOGIC ----------------

def get_lot_size(symbol):
    s = symbol.upper()
    if "BANKNIFTY" in s: return 30
    if "HDFCBANK" in s: return 550
    if "ICICIBANK" in s: return 700
    if "NIFTY" in s and "BANK" not in s: return 75
    return 1

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
            lot_size = get_lot_size(symbol)
            turnover = (oi_val / lot_size * 100000) if "-I" in symbol else (oi_val * price)

            if turnover >= 10000000:
                turnover_cr = turnover / 10000000
                passed.append(f"🏷 **{symbol}**\n⚡ **{action}**\n💰 **₹{turnover_cr:.2f} Cr**\n📊 Price: {price}")
        except:
            continue

    return "\n\n---\n\n".join(passed)

# ---------------- HANDLERS ----------------

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.channel_post or update.message
    if m and str(m.chat.id) == str(SOURCE_CHAT_ID) and m.text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(m.text)

async def aggregation_task(app):
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL)
        async with BUFFER_LOCK:
            if not MESSAGE_BUFFER:
                continue
            batch = list(MESSAGE_BUFFER)
            MESSAGE_BUFFER.clear()

        summary = summarize_alerts(batch)
        if summary:
            try:
                await app.bot.send_message(TARGET_CHAT_ID, summary, parse_mode="Markdown")
            except:
                pass

async def post_init(app):
    asyncio.create_task(aggregation_task(app))
    logger.info("Bot started successfully.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        logger.warning("Conflict error detected. Retrying automatically.")
        return
    logger.error("Exception while handling update:", exc_info=context.error)

# ---------------- MAIN ----------------

if __name__ == "__main__":
    while True:
        try:
            app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

            # ✅ FIXED for v21
            app.add_handler(
                MessageHandler(filters.UpdateType.CHANNEL_POST, message_handler)
            )

            app.add_error_handler(error_handler)
            app.run_polling()

        except Conflict:
            logger.warning("Startup conflict. Waiting 15s...")
            time.sleep(15)
        except Exception as e:
            logger.error(f"Critical crash: {e}. Restarting in 5s.")
            time.sleep(5)
