
import os, asyncio, logging, re, time, csv, pyotp
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.error import Conflict
from neo_api_client import NeoAPI 

# =========================
# CONFIG & LOGGING
# =========================
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ENV VARS FROM RAILWAY
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))

# KOTAK NEO CREDENTIALS
NEO_CONS_KEY = os.getenv("NEO_CONSUMER_KEY")
NEO_ID = os.getenv("NEO_ID")
NEO_PASS = os.getenv("NEO_PASSWORD")
NEO_TOTP_SEC = os.getenv("NEO_TOTP_SECRET")

# =========================
# TRADING STATE & INITIALIZATION
# =========================
active_trade = {"symbol": None, "entry": 0, "type": None, "qty": 30}
active_watch = {"symbol": None, "alert_price": 0, "timestamp": 0, "type": None}

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# UPDATED: Initialize for v2.0+ (removed consumer_secret)
neo = NeoAPI(consumer_key=NEO_CONS_KEY, environment='prod')

# =========================
# TRADING ENGINE (PAPER TRADE)
# =========================

async def get_live_price(symbol):
    """Fetches real-time LTP from Kotak Neo."""
    try:
        # Segment is typically 'nse_fo' for options
        quote = neo.quotes(symbols=[{'symbol': symbol, 'exchange': 'NSE'}])
        return float(quote['data'][0]['last_price'])
    except Exception as e:
        logger.error(f"LTP Error for {symbol}: {e}")
        return None

def log_trade(action, symbol, price, pnl=0):
    """Saves paper trade results to a local CSV file."""
    with open('trade_log.csv', 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([time.ctime(), action, symbol, price, pnl])

async def trading_engine():
    """Logic for watching price triggers and managing trades."""
    global active_trade, active_watch
    while True:
        await asyncio.sleep(1) # Check price every second
        
        # MONITOR WATCHLIST (10-Minute Window)
        if active_watch["symbol"]:
            if time.time() - active_watch["timestamp"] > 600: #
                logger.info(f"Watch window expired for {active_watch['symbol']}")
                active_watch["symbol"] = None
                continue
            
            ltp = await get_live_price(active_watch["symbol"])
            if ltp:
                # ENTRY TRIGGER: +/- 20 points from Alert
                if abs(ltp - active_watch["alert_price"]) >= 20:
                    # REVERSAL: Close opposite trade
                    if active_trade["symbol"] and active_trade["type"] != active_watch["type"]:
                        exit_price = await get_live_price(active_trade["symbol"])
                        pnl = exit_price - active_trade["entry"]
                        log_trade("EXIT (REVERSAL)", active_trade["symbol"], exit_price, pnl)
                    
                    # ENTER NEW TRADE
                    active_trade = {"symbol": active_watch["symbol"], "entry": ltp, "type": active_watch["type"], "qty": 30}
                    active_watch["symbol"] = None
                    log_trade("ENTER", active_trade["symbol"], ltp)

        # MONITOR ACTIVE POSITION (40-Point Target/SL)
        if active_trade["symbol"]:
            ltp = await get_live_price(active_trade["symbol"])
            if ltp:
                pnl = ltp - active_trade["entry"]
                if pnl >= 40 or pnl <= -40: #
                    log_trade("EXIT (TGT/SL)", active_trade["symbol"], ltp, pnl)
                    active_trade = {"symbol": None, "entry": 0, "type": None}

# =========================
# SCANNER LOGIC
# =========================

def get_lot_size(symbol):
    """2026 Lot Sizes."""
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
    global active_watch
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
            
            # TRADE TRIGGER: BankNifty only, no Unwinding/Short Covering
            if "BANKNIFTY" in symbol and ("CE" in symbol or "PE" in symbol):
                if any(x in action for x in ["WRITER", "BUYER", "ACTION"]):
                    active_watch = {
                        "symbol": symbol,
                        "alert_price": price,
                        "timestamp": time.time(),
                        "type": "CE" if "CE" in symbol else "PE"
                    }
            
            # SCANNER AGGREGATION
            oi_val = abs(int(oi_m.group(1).replace(",", ""))) if oi_m else 0
            lot_size = get_lot_size(symbol)
            turnover = (oi_val / lot_size * 100000) if "-I" in symbol else (oi_val * price)
            
            if turnover >= 10000000: # 1 Crore Threshold
                turnover_cr = turnover / 10000000
                passed.append(f"🏷 **{symbol}**\n⚡ **{action}**\n💰 **₹{turnover_cr:.2f} Cr**\n📊 Price: {price}")
        except: continue
    return "\n\n---\n\n".join(passed)

# =========================
# HANDLERS & INITIALIZATION
# =========================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.channel_post or update.message
    if m:
        logger.info(f"DEBUG: Message received in chat ID: {m.chat.id}")
    if m and str(m.chat.id) == str(SOURCE_CHAT_ID) and m.text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(m.text)

async def post_init(app):
    """Auto-Login for Kotak Neo."""
    try:
        # totp_gen = pyotp.TOTP(NEO_TOTP_SEC)
        # current_totp = totp_gen.now()
        
        # # New login sequence for v2.0+
        # neo.login(password=NEO_PASS)
        # neo.allow_2fa(token=current_totp)
        # logger.info("Successfully logged into Kotak Neo.")
        
        # asyncio.create_task(trading_engine())
        logger.info("Kotak Neo login is temporarily disabled for debugging.")
        asyncio.create_task(aggregation_task(app))
    except Exception as e:
        logger.error(f"Setup Failed: {e}")

async def aggregation_task(app):
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL)
        async with BUFFER_LOCK:
            if not MESSAGE_BUFFER: continue
            batch = list(MESSAGE_BUFFER); MESSAGE_BUFFER.clear()
        summary = summarize_alerts(batch)
        if summary:
            try: await app.bot.send_message(TARGET_CHAT_ID, summary, parse_mode="Markdown")
            except: pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log specific conflict errors and other exceptions."""
    if isinstance(context.error, Conflict):
        logger.warning("Conflict error detected, likely a deployment overlap. The library will handle the retry.")
        return
    logger.error("Exception while handling an update:", exc_info=context.error)


if __name__ == "__main__":
    while True:
        try:
            app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
            app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
            app.add_error_handler(error_handler)
            app.run_polling()
        except Conflict:
            logger.warning("Conflict on initial startup. Waiting 15s to retry...")
            time.sleep(15)
        except Exception as e:
            logger.error(f"Critical error in main loop: {e}. Restarting in 5s.")
            time.sleep(5)
