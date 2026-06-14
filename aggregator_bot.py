import os
import asyncio
import logging
import re
import time
import pandas as pd
import requests
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
DEEP_ITM_DIFF_THRESHOLD = 500
NEAR_ITM_DIFF_THRESHOLD = 100
NEAR_ITM_MIN_LOTS = 1000
FUTURE_MIN_LOTS = 500
MCX_FUTURE_MIN_LOTS = 300
MCX_OPTION_MIN_LOTS = 100

# =========================
# DYNAMIC INSTRUMENT DATA
# =========================
INSTRUMENTS_LOCAL_PATH = "instruments.csv"
INSTRUMENTS_WINDOWS_PATH = r"C:\Users\kalpe\zarodha\instruments.csv"
DYNAMIC_LOT_SIZES = {}
DYNAMIC_NEAR_ITM_RANGE = {}

def load_instrument_data():
    """Loads lot sizes and strike intervals. Downloads if not found locally."""
    global DYNAMIC_LOT_SIZES, DYNAMIC_NEAR_ITM_RANGE
    csv_path = None

    # 1. Check priority paths
    if os.path.exists(INSTRUMENTS_LOCAL_PATH):
        csv_path = INSTRUMENTS_LOCAL_PATH
    elif os.path.exists(INSTRUMENTS_WINDOWS_PATH):
        csv_path = INSTRUMENTS_WINDOWS_PATH
    
    # 2. If not found, download from Zerodha (for Container/Railway environments)
    if not csv_path:
        try:
            logger.info("Instruments CSV not found. Downloading fresh copy from Zerodha...")
            r = requests.get("https://api.kite.trade/instruments", timeout=60)
            if r.status_code == 200:
                with open(INSTRUMENTS_LOCAL_PATH, "wb") as f:
                    f.write(r.content)
                csv_path = INSTRUMENTS_LOCAL_PATH
                logger.info("Download successful.")
            else:
                logger.error(f"Failed to download instruments. Status: {r.status_code}")
        except Exception as e:
            logger.error(f"Download error: {e}")

    if not csv_path:
        logger.warning("Could not obtain instruments.csv. Using hardcoded fallbacks.")
        return

    try:
        logger.info(f"Processing instrument data from {csv_path}...")
        df = pd.read_csv(csv_path, low_memory=False)
        
        # Build Lot Sizes
        for name, group in df.groupby("name"):
            if pd.isna(name): continue
            lots = group["lot_size"].mode()
            if not lots.empty:
                DYNAMIC_LOT_SIZES[name] = int(lots[0])
        
        # Calculate Near ITM Range from Options (Focusing on June 2026)
        opt_df = df[df["segment"].str.contains("-OPT", na=False)].copy()
        current_month_str = "2026-06"
        current_opt_df = opt_df[opt_df["expiry"].str.startswith(current_month_str, na=False)].copy()
        processing_df = current_opt_df if not current_opt_df.empty else opt_df
        
        for name, group in processing_df.groupby("name"):
            strikes = sorted(group[group["strike"] > 0]["strike"].unique())
            if len(strikes) >= 2:
                interval = strikes[1] - strikes[0]
                DYNAMIC_NEAR_ITM_RANGE[name] = interval
            else:
                DYNAMIC_NEAR_ITM_RANGE[name] = 100

        logger.info(f"Successfully loaded {len(DYNAMIC_LOT_SIZES)} symbols. RELIANCE Range: {DYNAMIC_NEAR_ITM_RANGE.get('RELIANCE', 'N/A')}")
    except Exception as e:
        logger.error(f"Error processing CSV: {e}")

# Initial load
load_instrument_data()

NSE_TRACK_SYMBOLS = [
    "BANKNIFTY", "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK",
    "BAJFINANCE", "BAJAJFINSV", "INDUSINDBK", "BANKBARODA", "PNB", "RELIANCE",
    "ONGC", "NTPC", "POWERGRID", "COALINDIA", "BPCL", "GAIL", "INFOSYS", "TCS",
    "HCLTECH", "WIPRO", "TECHM", "TATAMOTORS", "M&M", "MARUTI", "ASHOKLEY",
    "LT", "SUNPHARMA", "ITC", "HINDUNILVR", "NIFTY", "SENSEX", "MIDCPNIFTY", "FINNIFTY",
]
MCX_TRACK_SYMBOLS = [
    "CRUDEOIL",
]
TRACK_SYMBOLS = NSE_TRACK_SYMBOLS + MCX_TRACK_SYMBOLS

# Hardcoded Fallbacks
NEAR_ITM_RANGE_FALLBACK = {
    "BANKNIFTY": 100, "NIFTY": 50, "RELIANCE": 10, "MIDCPNIFTY": 25, "FINNIFTY": 50, "SENSEX": 100
}

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# =========================
# UTILITIES & LOGIC
# =========================

def is_market_hours():
    """Checks if current time is inside NSE or MCX alert windows."""
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist).time()
    nse_start = datetime.strptime("09:00", "%H:%M").time()
    nse_end = datetime.strptime("15:30", "%H:%M").time()
    mcx_start = datetime.strptime("15:30", "%H:%M").time()
    mcx_end = datetime.strptime("23:30", "%H:%M").time()
    return (nse_start <= now <= nse_end) or (mcx_start <= now <= mcx_end)

def get_lot_size(symbol):
    """Returns accurate lot sizes, prioritizing dynamic data from CSV."""
    s = symbol.upper().replace(" ", "")
    for name in TRACK_SYMBOLS:
        if name in s:
            if name in DYNAMIC_LOT_SIZES:
                return DYNAMIC_LOT_SIZES[name]
    
    # Static Fallbacks (April 2026)
    if "BANKNIFTY" in s: return 30
    if "HDFCBANK" in s: return 550
    if "ICICIBANK" in s: return 700
    if "SBIN" in s: return 1500
    if "AXISBANK" in s: return 625
    if "KOTAKBANK" in s: return 400
    if "BAJFINANCE" in s: return 125
    if "BAJAJFINSV" in s: return 500
    if "INDUSINDBK" in s: return 500
    if "BANKBARODA" in s: return 4850
    if "PNB" in s: return 8000
    if "RELIANCE" in s: return 250
    if "ONGC" in s: return 3850
    if "NTPC" in s: return 3000
    if "POWERGRID" in s: return 3600
    if "COALINDIA" in s: return 2100
    if "BPCL" in s: return 1800
    if "GAIL" in s: return 4550
    if "INFOSYS" in s: return 400
    if "TCS" in s: return 175
    if "HCLTECH" in s: return 700
    if "WIPRO" in s: return 1500
    if "TECHM" in s: return 600
    if "TATAMOTORS" in s: return 550
    if "M&M" in s: return 350
    if "MARUTI" in s: return 50
    if "ASHOKLEY" in s: return 5000
    if "LT" in s: return 150
    if "SUNPHARMA" in s: return 700
    if "ITC" in s: return 1600
    if "HINDUNILVR" in s: return 300
    if "NIFTY" in s: return 65
    if "SENSEX" in s: return 20
    if "MIDCPNIFTY" in s: return 120
    if "FINNIFTY" in s: return 60
    if "CRUDEOIL" in s: return 1
    return 1 

def is_mcx_symbol(symbol):
    s = symbol.upper().replace(" ", "")
    return any(name in s for name in MCX_TRACK_SYMBOLS)

def classify_strike(strike, option_type, future_price, symbol=None):
    try:
        strike = float(strike)
        future_price = float(future_price)
        if symbol in MCX_TRACK_SYMBOLS:
            if option_type == "CE":
                return "ITM" if strike < future_price else "OTM"
            if option_type == "PE":
                return "ITM" if strike > future_price else "OTM"

        near_range = DYNAMIC_NEAR_ITM_RANGE.get(symbol, NEAR_ITM_RANGE_FALLBACK.get(symbol, 100))
        if abs(strike - future_price) <= near_range:
            return "ITM"
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
    if "BUYER" in t or "LONG BUILDUP" in t or "FUTURE BUY" in t or "CALL BUY" in t or "PUT BUY" in t: return "BUYER 🔵"
    return "ACTION"

def summarize_alerts(alerts):
    passed = []
    p_sym = re.compile(r"Symbol\s*:\s*(.*?)(?:\n|$)", re.IGNORECASE)
    p_oi = re.compile(r"OI CHANGE\s*:\s*([+-]?[0-9,]+)", re.IGNORECASE)
    p_pr = re.compile(r"PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)
    p_fut = re.compile(r"FUTURE PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)

    for alert in alerts:
        try:
            upper_alert = alert.upper()
            if not any(name in upper_alert for name in TRACK_SYMBOLS):
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
            base_symbol = next((name for name in TRACK_SYMBOLS if name in symbol.upper()), None)

            if is_mcx_symbol(symbol):
                if "FUT" in symbol.upper():
                    if num_lots < MCX_FUTURE_MIN_LOTS: continue
                    passed.append(f"🏷 **{symbol}**\n⚡ **{action}**\n📦 Lots: {int(num_lots)} (Qty: {oi_val})\n📊 Price: {price}\n🔹 **Fut Price: {future_price}**")
                    continue
                strike_m = re.search(r"(\d+)(CE|PE)$", symbol.upper())
                if not strike_m: continue
                strike_val = float(strike_m.group(1))
                option_type = strike_m.group(2)
                zone = classify_strike(strike_val, option_type, future_price, base_symbol)
                if zone != "ITM" or num_lots < MCX_OPTION_MIN_LOTS: continue
                diff = round(abs(strike_val - future_price), 2)
                passed.append(f"🏷 **{symbol} (MCX-ITM-{diff}-diff)**\n⚡ **{action}**\n📦 Lots: {int(num_lots)} (Qty: {oi_val})\n📊 Price: {price}\n🔹 **Fut Price: {future_price}**")
                continue

            zone_label = ""
            if "FUT" in symbol.upper():
                if num_lots < FUTURE_MIN_LOTS: continue
                turnover = oi_val * price
                if turnover < FUTURE_THRESHOLD: continue
                turnover_cr = turnover / 10000000
                passed.append(f"🏷 **{symbol}**\n⚡ **{action}**\n💰 **Turnover: ₹{turnover_cr:.2f} Cr**\n📦 Lots: {int(num_lots)} (Qty: {oi_val})\n📊 Price: {price}\n🔹 **Fut Price: {future_price}**")
                continue
            else:
                strike_m = re.search(r"(\d+)(CE|PE)$", symbol.upper())
                if strike_m:
                    strike_val = float(strike_m.group(1))
                    option_type = strike_m.group(2)
                    zone = classify_strike(strike_val, option_type, future_price, base_symbol)
                    diff = round(abs(strike_val - future_price), 2)
                    near_itm_diff_threshold = DYNAMIC_NEAR_ITM_RANGE.get(base_symbol, NEAR_ITM_RANGE_FALLBACK.get(base_symbol, NEAR_ITM_DIFF_THRESHOLD))
                    if zone == "ITM" and diff >= DEEP_ITM_DIFF_THRESHOLD: zone_label = f" ({zone}-{diff}-diff)"
                    elif zone == "ITM" and diff < DEEP_ITM_DIFF_THRESHOLD and num_lots >= NEAR_ITM_MIN_LOTS: zone_label = f" (ITM-{diff}-diff-HIGHLOTS)"
                    elif diff <= near_itm_diff_threshold and num_lots >= NEAR_ITM_MIN_LOTS: zone_label = f" (NEAR-ITM-{diff}-diff)"
                    else: continue
                else: continue

            turnover = (num_lots * 120000) if ("WRITER" in action or "SHORT COVERING" in action) else (oi_val * price)
            current_threshold = WRITER_SC_THRESHOLD if ("WRITER" in action or "SHORT COVERING" in action) else BUYER_UW_THRESHOLD
            if turnover < current_threshold: continue
            turnover_cr = turnover / 10000000
            passed.append(f"🏷 **{symbol}{zone_label}**\n⚡ **{action}**\n💰 **Turnover: ₹{turnover_cr:.2f} Cr**\n📦 Lots: {int(num_lots)} (Qty: {oi_val})\n📊 Price: {price}\n🔹 **Fut Price: {future_price}**")
        except Exception as e:
            logger.error(f"Error processing alert: {e}")
            continue
    return "\n\n---\n\n".join(passed)

# =========================
# TELEGRAM HANDLERS
# =========================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_market_hours(): return
    m = update.channel_post or update.message
    if m and str(m.chat_id) == str(SOURCE_CHAT_ID) and m.text:
        async with BUFFER_LOCK: MESSAGE_BUFFER.append(m.text)

async def aggregation_task(app):
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL)
        if not is_market_hours():
            async with BUFFER_LOCK: MESSAGE_BUFFER.clear()
            continue
        async with BUFFER_LOCK:
            if not MESSAGE_BUFFER: continue
            batch = list(MESSAGE_BUFFER); MESSAGE_BUFFER.clear()
        summary = summarize_alerts(batch)
        if summary:
            try: await app.bot.send_message(TARGET_CHAT_ID, summary, parse_mode="Markdown")
            except: pass

async def post_init(app):
    asyncio.create_task(aggregation_task(app))

if __name__ == "__main__":
    while True:
        try:
            app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
            app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
            logger.info("Bot starting with dynamic instrument data (June 2026)...")
            app.run_polling()
        except Conflict: time.sleep(15)
        except Exception as e: time.sleep(5)
