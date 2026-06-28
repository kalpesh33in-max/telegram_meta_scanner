import os
import asyncio
import logging
import re
import time
import json
import uuid
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
# Set default logging level to INFO for startup
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress verbose logs from libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# =========================
# ENV & CONFIG
# =========================
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
    TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
    AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))

    # Matrix / Element X Credentials
    MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "https://matrix.org")
    MATRIX_ACCESS_TOKEN = os.getenv("MATRIX_ACCESS_TOKEN", "")
    MATRIX_USER = os.getenv("MATRIX_USER", "")
    MATRIX_PASS = os.getenv("MATRIX_PASS", "")
    MATRIX_TOKEN_FILE = "matrix_access_token.txt"
    # Check for custom name 'meta-scanner' or standard MATRIX_ROOM_ID
    MATRIX_ROOM_ID = os.getenv("meta-scanner") or os.getenv("MATRIX_ROOM_ID", "")
except KeyError as e:
    logger.critical(f"Missing Env Var: {e}")
    raise SystemExit

# Updated Thresholds based on your requirements
FUTURE_MIN_LOTS = 500
NEAR_MID_ITM_MIN_LOTS = 500
FAR_ITM_MIN_LOTS = 100
MCX_FUTURE_MIN_LOTS = 300
MCX_OPTION_MIN_LOTS = 200  # For Crude Oil Near-ITM as requested

# =========================
# MATRIX UTILS
# =========================

def perform_matrix_login():
    if not MATRIX_USER or not MATRIX_PASS:
        return None
    
    login_url = f"{MATRIX_HOMESERVER}/_matrix/client/v3/login"
    payload = {
        "type": "m.login.password",
        "user": MATRIX_USER,
        "password": MATRIX_PASS,
        "initial_device_display_name": "MetaScannerAuto"
    }
    
    try:
        response = requests.post(login_url, json=payload, timeout=15)
        if response.status_code == 200:
            token = response.json().get("access_token")
            if token:
                with open(MATRIX_TOKEN_FILE, "w") as f:
                    f.write(token)
                logger.info("Matrix auto-login successful.")
                return token
        else:
            logger.error(f"Matrix auto-login failed: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Matrix auto-login error: {e}")
    return None

def get_matrix_token():
    # 1. Try to read from file first
    token = None
    if os.path.exists(MATRIX_TOKEN_FILE):
        try:
            with open(MATRIX_TOKEN_FILE, "r") as f:
                token = f.read().strip()
        except Exception as e:
            logger.error(f"Error reading {MATRIX_TOKEN_FILE}: {e}")
    
    # 2. Fallback to environment variable
    if not token:
        token = MATRIX_ACCESS_TOKEN
        
    # 3. Auto-login if still no token
    if not token:
        token = perform_matrix_login()
        
    return token

# =========================
# DYNAMIC INSTRUMENT DATA
# =========================
INSTRUMENTS_LOCAL_PATH = "instruments.csv"
INSTRUMENTS_WINDOWS_PATH = r"C:\Users\kalpe\zarodha\instruments.csv"
INSTRUMENTS_REFRESH_INTERVAL = int(os.getenv("INSTRUMENTS_REFRESH_INTERVAL", 86400))
DYNAMIC_LOT_SIZES = {}
DYNAMIC_NEAR_ITM_RANGE = {}

def load_instrument_data(force_download=False):
    """Loads lot sizes and strike intervals. Downloads fresh copy when requested."""
    global DYNAMIC_LOT_SIZES, DYNAMIC_NEAR_ITM_RANGE
    csv_path = None

    if not force_download and os.path.exists(INSTRUMENTS_LOCAL_PATH):
        csv_path = INSTRUMENTS_LOCAL_PATH
    elif not force_download and os.path.exists(INSTRUMENTS_WINDOWS_PATH):
        csv_path = INSTRUMENTS_WINDOWS_PATH
    
    if force_download or not csv_path:
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

    if not csv_path and os.path.exists(INSTRUMENTS_LOCAL_PATH):
        csv_path = INSTRUMENTS_LOCAL_PATH
    elif not csv_path and os.path.exists(INSTRUMENTS_WINDOWS_PATH):
        csv_path = INSTRUMENTS_WINDOWS_PATH

    if not csv_path:
        logger.warning("Could not obtain instruments.csv. Using hardcoded fallbacks.")
        return

    try:
        logger.info(f"Processing instrument data from {csv_path}...")
        df = pd.read_csv(csv_path, low_memory=False)
        fo_df = df[df["segment"].str.contains("-FUT|-OPT", na=False)].copy()
        
        for name, group in fo_df.groupby("name"):
            if pd.isna(name): continue
            lots = group["lot_size"].mode()
            if not lots.empty:
                DYNAMIC_LOT_SIZES[name] = int(lots[0])
        
        opt_df = fo_df[fo_df["segment"].str.contains("-OPT", na=False)].copy()
        opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], errors="coerce")

        # Prefer the nearest future expiry so the step logic follows the active contract
        # rollover automatically (for example, June -> July without manual edits).
        today_ist = pd.Timestamp.now(tz=pytz.timezone("Asia/Kolkata")).tz_localize(None).normalize()
        future_expiries = opt_df[opt_df["expiry_dt"].notna() & (opt_df["expiry_dt"] >= today_ist)]
        active_opt_df = future_expiries if not future_expiries.empty else opt_df[opt_df["expiry_dt"].notna()]
        active_expiry = None
        if not active_opt_df.empty:
            active_expiry = active_opt_df["expiry_dt"].min()

        # Build strike interval map from the active option universe so stocks and indices
        # both get a dynamic step size from instruments.csv.
        for name, group in active_opt_df.groupby("name"):
            strikes = sorted(group[group["strike"] > 0]["strike"].unique())
            if len(strikes) >= 2:
                diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
                valid_diffs = [d for d in diffs if d >= 1]
                if valid_diffs:
                    DYNAMIC_NEAR_ITM_RANGE[name] = min(valid_diffs)
                else:
                    DYNAMIC_NEAR_ITM_RANGE[name] = min(diffs)

        logger.info(
            f"Successfully loaded {len(DYNAMIC_LOT_SIZES)} symbols. "
            f"RELIANCE Range: {DYNAMIC_NEAR_ITM_RANGE.get('RELIANCE', 'N/A')}"
        )
        if pd.notna(active_expiry) if active_expiry is not None else False:
            logger.info(f"Active expiry selected: {active_expiry.date().isoformat()}")
    except Exception as e:
        logger.error(f"Error processing CSV: {e}")

async def refresh_instrument_data_task():
    """Periodically refreshes instrument metadata from disk or Zerodha."""
    while True:
        await asyncio.sleep(INSTRUMENTS_REFRESH_INTERVAL)
        try:
            load_instrument_data(force_download=True)
            logger.info("Instrument data refreshed successfully.")
        except Exception as e:
            logger.error(f"Instrument refresh failed: {e}")

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

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()
MARKET_SESSION_REFRESHED = False

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

def get_step_interval(symbol):
    """Returns the strike step interval from instruments.csv, with a small generic fallback."""
    s = symbol.upper().replace(" ", "")
    base_symbol = next((name for name in TRACK_SYMBOLS if name in s), None)
    if base_symbol and base_symbol in DYNAMIC_NEAR_ITM_RANGE:
        return DYNAMIC_NEAR_ITM_RANGE[base_symbol]
    return 100

def classify_strike(strike, option_type, future_price, symbol=None):
    try:
        strike = float(strike)
        future_price = float(future_price)
        if symbol in MCX_TRACK_SYMBOLS:
            if option_type == "CE":
                return "ITM" if strike < future_price else "OTM"
            if option_type == "PE":
                return "ITM" if strike > future_price else "OTM"

        near_range = get_step_interval(symbol)
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
    p_lots = re.compile(r"LOTS\s*:\s*([0-9,]+)", re.IGNORECASE)
    p_oi = re.compile(r"OI CHANGE\s*:\s*([+-]?[0-9,]+)", re.IGNORECASE)
    p_pr = re.compile(r"PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)
    p_fut = re.compile(r"FUTURE PRICE\s*:\s*([\d\.]+)", re.IGNORECASE)

    for alert in alerts:
        try:
            upper_alert = alert.upper()
            if not any(name in upper_alert for name in TRACK_SYMBOLS):
                continue

            s_m = p_sym.search(alert)
            lots_m = p_lots.search(alert)
            oi_m = p_oi.search(alert)
            pr_m = p_pr.search(alert)
            f_m = p_fut.search(alert)
            if not (s_m and lots_m and oi_m and pr_m and f_m): continue

            symbol = s_m.group(1).strip()
            price = float(pr_m.group(1))
            future_price = float(f_m.group(1))
            oi_val = abs(int(oi_m.group(1).replace(",", "")))
            raw_lots = float(lots_m.group(1).replace(",", ""))
            
            lot_size = get_lot_size(symbol)
            num_lots = raw_lots if raw_lots > 0 else (oi_val / lot_size)
            action = identify_participant(alert)
            base_symbol = next((name for name in TRACK_SYMBOLS if name in symbol.upper()), None)

            if is_mcx_symbol(symbol):
                if "FUT" in symbol.upper():
                    if num_lots < MCX_FUTURE_MIN_LOTS: continue
                    passed.append(f"🏷 {symbol}\n⚡ {action}\n💰 Turnover: ₹{(num_lots * 120000) / 10000000:.2f} Cr\n📦 Lots: {int(num_lots)} (Qty: {oi_val})\n📊 Price: {price}\n🔹 Fut Price: {future_price}")
                    continue
                strike_m = re.search(r"(\d+)(CE|PE)$", symbol.upper())
                if not strike_m: continue
                strike_val = float(strike_m.group(1))
                option_type = strike_m.group(2)
                zone = classify_strike(strike_val, option_type, future_price, base_symbol)
                
                # Crude Oil specific Near-ITM rule
                current_mcx_threshold = MCX_OPTION_MIN_LOTS
                if "CRUDEOIL" in symbol.upper():
                    interval = get_step_interval(base_symbol or symbol)
                    diff = abs(strike_val - future_price)
                    if diff <= interval: # Near-ITM
                        current_mcx_threshold = 200
                
                if zone != "ITM" or num_lots < current_mcx_threshold: continue
                diff = round(abs(strike_val - future_price), 2)
                passed.append(f"🏷 {symbol} (MCX-ITM-{diff}-diff)\n⚡ {action}\n💰 Turnover: ₹{(oi_val * price) / 10000000:.2f} Cr\n📦 Lots: {int(num_lots)} (Qty: {oi_val})\n📊 Price: {price}\n🔹 Fut Price: {future_price}")
                continue

            turnover = oi_val * price
            
            zone_label = ""
            if "FUT" in symbol.upper():
                if num_lots <= FUTURE_MIN_LOTS: continue
                turnover_cr = turnover / 10000000
                passed.append(f"🏷 {symbol}\n⚡ {action}\n💰 Turnover: ₹{turnover_cr:.2f} Cr\n📦 Lots: {int(num_lots)} (Qty: {oi_val})\n📊 Price: {price}\n🔹 Fut Price: {future_price}")
                continue
            else:
                strike_m = re.search(r"(\d+)(CE|PE)$", symbol.upper())
                if strike_m:
                    strike_val = float(strike_m.group(1))
                    option_type = strike_m.group(2)
                    zone = classify_strike(strike_val, option_type, future_price, base_symbol)
                    
                    diff = round(abs(strike_val - future_price), 2)
                    interval = get_step_interval(base_symbol or symbol)
                    far_itm_threshold = interval * 5
                    
                    # FILTER Logic:
                    # 1. Far ITM: lots must be at least 100
                    # 2. Near/Mid ITM: lots must be greater than 500
                    if zone == "ITM" and diff >= far_itm_threshold:
                        if num_lots < FAR_ITM_MIN_LOTS: continue
                        zone_label = f" (FAR-ITM-{diff}-diff)"
                    elif zone == "ITM" and diff >= interval:
                        if num_lots <= NEAR_MID_ITM_MIN_LOTS: continue
                        zone_label = f" (MID-ITM-{diff}-diff)"
                    elif diff <= interval:
                        if num_lots <= NEAR_MID_ITM_MIN_LOTS: continue
                        zone_label = f" (NEAR-ITM-{diff}-diff)"
                    else:
                        continue
                else:
                    continue

            turnover_cr = turnover / 10000000
            passed.append(
                f"🏷 {symbol}{zone_label}\n"
                f"⚡ {action}\n"
                f"💰 Turnover: ₹{turnover_cr:.2f} Cr\n"
                f"📦 Lots: {int(num_lots)} (Qty: {oi_val})\n"
                f"📊 Price: {price}\n"
                f"🔹 Fut Price: {future_price}"
            )
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

async def send_matrix_message(message):
    token = get_matrix_token()
    if not (token and MATRIX_ROOM_ID):
        return
    try:
        txn_id = str(uuid.uuid4())
        url = f"{MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{MATRIX_ROOM_ID}/send/m.room.message/{txn_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "msgtype": "m.text",
            "body": message
        }
        
        def do_request(h):
            return requests.put(url, headers=h, data=json.dumps(payload), timeout=10)

        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: do_request(headers))
        
        if res.status_code == 401:
            logger.warning("Matrix token expired. Attempting auto-login...")
            new_token = perform_matrix_login()
            if new_token:
                headers["Authorization"] = f"Bearer {new_token}"
                res = await loop.run_in_executor(None, lambda: do_request(headers))
        
        if res.status_code != 200:
            logger.error(f"Matrix Delivery Error: {res.status_code} - {res.text}")
    except Exception as e:
        logger.error(f"Matrix Exception: {e}")

async def aggregation_task(app):
    global MARKET_SESSION_REFRESHED
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL)
        if not is_market_hours():
            MARKET_SESSION_REFRESHED = False
            async with BUFFER_LOCK: MESSAGE_BUFFER.clear()
            continue

        if not MARKET_SESSION_REFRESHED:
            try:
                load_instrument_data(force_download=True)
                MARKET_SESSION_REFRESHED = True
                logger.info("Market-open instrument refresh completed.")
            except Exception as e:
                logger.error(f"Market-open refresh failed: {e}")

        async with BUFFER_LOCK:
            if not MESSAGE_BUFFER: continue
            batch = list(MESSAGE_BUFFER); MESSAGE_BUFFER.clear()
        summary = summarize_alerts(batch)
        if summary:
            # Send to Telegram
            try: 
                await app.bot.send_message(TARGET_CHAT_ID, summary, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Telegram Send Error: {e}")
            
            # Send to Matrix
            await send_matrix_message(summary)

async def post_init(app):
    try:
        load_instrument_data(force_download=True)
        logger.info("Startup instrument refresh completed.")
    except Exception as e:
        logger.error(f"Startup refresh failed: {e}")
    asyncio.create_task(aggregation_task(app))
    asyncio.create_task(refresh_instrument_data_task())

if __name__ == "__main__":
    while True:
        try:
            app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
            app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
            
            # Silent during off-market hours
            if is_market_hours():
                logger.info("Bot starting in ACTIVE mode (Market Hours)...")
            else:
                logger.info("Bot starting in SILENT mode (Off-Market Hours)...")

            app.run_polling()
        except Conflict: time.sleep(15)
        except Exception as e: time.sleep(5)
