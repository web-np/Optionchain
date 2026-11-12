from datetime import datetime, timedelta, timezone
import threading
import time
import gspread
import pandas as pd
import requests
import streamlit as st

# ==============================================================================
# SECTION 1: PAGE CONFIG & CONSTANTS
# Kaam: Streamlit dashboard ka layout aur Delta Exchange API settings define karna.
# ==============================================================================
st.set_page_config(
    page_title="Delta Exchange 1m Auto Option & Futures Logger",
    layout="wide",
)

BASE_URL = "https://api.india.delta.exchange/v2"
HEADERS = {"Accept": "application/json", "User-Agent": "python-delta-streamlit"}
IST = timezone(timedelta(hours=5, minutes=30))  # Indian Standard Time


# ==============================================================================
# SECTION 2: GOOGLE SHEETS CONNECTION (USING SPREADSHEET ID)
# Kaam: Streamlit Secrets se Service Account credentials lekar Google Sheet connect karna.
#       open_by_key() use kiya gaya hai taaki Google Drive API 403 error na aaye.
# ==============================================================================
@st.cache_resource
def get_google_sheet():
    creds_dict = dict(st.secrets["gcp_service_account"])
    gc = gspread.service_account_from_dict(creds_dict)
    sheet_id = st.secrets["SPREADSHEET_ID"]
    return gc.open_by_key(sheet_id)


def get_or_create_worksheet(spreadsheet, title: str, headers: list):
    """Agar worksheet exist nahi karti to banata hai aur header row add karta hai."""
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
    return ws


# ==============================================================================
# SECTION 3: AUTO-EXPIRY DETECTOR & DELTA EXCHANGE API FETCHERS
# Kaam:
#   1. fetch_available_expiries(): Live active options ki sabhi future expiry dates
#      nikal kar chronological order me sort karta hai (nearest expiry sabse upar).
#   2. fetch_option_chain() & fetch_futures_data(): Live market data fetch karte hain.
# ==============================================================================
def fetch_available_expiries(asset: str) -> list:
    """Delta Exchange se live call/put options ki sabhi upcoming expiry dates (DD-MM-YYYY) lata hai."""
    url = f"{BASE_URL}/products"
    params = {
        "contract_types": "call_options",
        "states": "live",
        "page_size": 500,
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return []

        expiries_dt = set()
        now_utc = datetime.now(timezone.utc)

        for prod in data.get("result", []):
            # Symbol format: C-ETH-3200-141126 (DDMMYY at the end)
            sym = prod.get("symbol", "")
            parts = sym.split("-")
            if len(parts) == 4 and parts[1] == asset:
                # settlement_time ISO format se exact expiry date nikalna
                settle_str = prod.get("settlement_time")
                if settle_str:
                    dt = datetime.fromisoformat(
                        settle_str.replace("Z", "+00:00")
                    )
                    if dt > now_utc:
                        expiries_dt.add(dt.date())

        sorted_dates = sorted(list(expiries_dt))
        return [d.strftime("%d-%m-%Y") for d in sorted_dates]
    except Exception as e:
        print(f"[Expiry Fetch Error]: {e}")
        return []


def fetch_option_chain(asset: str, expiry: str) -> list:
    url = f"{BASE_URL}/tickers"
    params = {
        "contract_types": "call_options,put_options",
        "underlying_asset_symbols": asset,
        "expiry_date": expiry,
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("result", []) if data.get("success") else []
    except Exception as e:
        print(f"[Option API Error]: {e}")
        return []


def fetch_futures_data(asset: str) -> list:
    url = f"{BASE_URL}/tickers"
    params = {
        "contract_types": "perpetual_futures,futures",
        "underlying_asset_symbols": asset,
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("result", []) if data.get("success") else []
    except Exception as e:
        print(f"[Futures API Error]: {e}")
        return []


# ==============================================================================
# SECTION 4: DATA PARSING & DAILY ANALYSIS CALCULATOR
# Kaam: Raw JSON ko clean rows me convert karna aur PCR, Support, Resistance nikalna.
# ==============================================================================
def parse_records(raw_list: list, timestamp_str: str, is_option: bool) -> list:
    rows = []
    for item in raw_list:
        quotes = item.get("quotes") or {}
        greeks = item.get("greeks") or {}

        base_row = {
            "Timestamp_IST": timestamp_str,
            "Symbol": item.get("symbol"),
            "Contract_Type": item.get("contract_type"),
            "Strike_Price": (
                float(item.get("strike_price") or 0) if is_option else 0
            ),
            "Spot_Price": float(item.get("spot_price") or 0),
            "Market_Price": float(item.get("mark_price") or 0),
            "LTP_Close": float(item.get("close") or 0),
            "Best_Bid": float(quotes.get("best_bid") or 0),
            "Bid_Size": float(quotes.get("bid_size") or 0),
            "Best_Ask": float(quotes.get("best_ask") or 0),
            "Ask_Size": float(quotes.get("ask_size") or 0),
            "Volume": float(item.get("volume") or 0),
            "Turnover_USD": float(item.get("turnover_usd") or 0),
            "OI": float(item.get("oi") or 0),
            "OI_Value_USD": float(item.get("oi_value_usd") or 0),
        }

        if is_option:
            base_row.update({
                "Bid_IV": float(quotes.get("bid_iv") or 0),
                "Ask_IV": float(quotes.get("ask_iv") or 0),
                "Mark_IV": float(item.get("mark_vol") or 0),
                "Delta": float(greeks.get("delta") or 0),
                "Gamma": float(greeks.get("gamma") or 0),
                "Theta": float(greeks.get("theta") or 0),
                "Vega": float(greeks.get("vega") or 0),
            })
        else:
            base_row.update({
                "High_24h": float(item.get("high") or 0),
                "Low_24h": float(item.get("low") or 0),
                "Change_24h_%": float(item.get("ltp_change_24h") or 0),
            })
        rows.append(base_row)

    if is_option and rows:
        rows.sort(key=lambda x: (x["Strike_Price"], x["Contract_Type"]))
    return rows


def build_analysis_row(
    df_opt: pd.DataFrame, df_fut: pd.DataFrame, timestamp_str: str, expiry: str
) -> dict:
    calls = df_opt[df_opt["Contract_Type"] == "call_options"]
    puts = df_opt[df_opt["Contract_Type"] == "put_options"]

    call_oi = float(calls["OI"].sum())
    put_oi = float(puts["OI"].sum())
    pcr_oi = round(put_oi / call_oi, 4) if call_oi > 0 else 0

    call_vol = float(calls["Volume"].sum())
    put_vol = float(puts["Volume"].sum())
    pcr_vol = round(put_vol / call_vol, 4) if call_vol > 0 else 0

    res_strike = (
        float(calls.loc[calls["OI"].idxmax(), "Strike_Price"])
        if not calls.empty
        else 0
    )
    sup_strike = (
        float(puts.loc[puts["OI"].idxmax(), "Strike_Price"])
        if not puts.empty
        else 0
    )
    spot = float(df_opt["Spot_Price"].iloc[0]) if not df_opt.empty else 0

    perp = df_fut[df_fut["Contract_Type"] == "perpetual_futures"]
    fut_price = float(perp["Market_Price"].iloc[0]) if not perp.empty else 0
    fut_vol = float(perp["Volume"].iloc[0]) if not perp.empty else 0

    sentiment = (
        "Bullish"
        if pcr_oi > 1.1
        else ("Bearish" if pcr_oi < 0.8 else "Sideways")
    )

    return {
        "Timestamp_IST": timestamp_str,
        "Active_Expiry": expiry,
        "Spot_Price": spot,
        "Perp_Futures_Price": fut_price,
        "Perp_Futures_Volume": fut_vol,
        "Total_Call_OI": call_oi,
        "Total_Put_OI": put_oi,
        "PCR_OI": pcr_oi,
        "Total_Call_Volume": call_vol,
        "Total_Put_Volume": put_vol,
        "PCR_Volume": pcr_vol,
        "Resistance_Max_Call_OI": res_strike,
        "Support_Max_Put_OI": sup_strike,
        "Sentiment": sentiment,
    }


# ==============================================================================
# SECTION 5: AUTO-START BACKGROUND WORKER THREAD
# Kaam: App boot hote hi apne aap nearest expiry detect karke har 60 seconds me
#       Google Sheet me data append karta rehta hai. Agar purani expiry expire ho
#       jaye to automatically next nearest expiry par switch kar leta hai.
# ==============================================================================
class BackgroundRecorder:

    def __init__(self):
        self.running = False
        self.asset = "ETH"
        self.auto_expiry_mode = True
        self.available_expiries = fetch_available_expiries(self.asset)
        self.expiry = (
            self.available_expiries[0]
            if self.available_expiries
            else "14-11-2026"
        )
        self.last_run = "Initializing..."
        self.last_status = "Auto-starting on boot..."
        self.latest_analysis = {}
        self.latest_options_df = pd.DataFrame()
        self.latest_futures_df = pd.DataFrame()
        self.thread = None

    def start(self, asset: str, expiry: str, auto_mode: bool = True):
        self.asset = asset
        self.auto_expiry_mode = auto_mode
        if auto_mode:
            fresh_expiries = fetch_available_expiries(asset)
            if fresh_expiries:
                self.available_expiries = fresh_expiries
                self.expiry = fresh_expiries[0]
        else:
            self.expiry = expiry

        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        self.last_status = "Manually paused by user"

    def _loop(self):
        spreadsheet = get_google_sheet()
        while self.running:
            start_dt = datetime.now(IST)
            ts_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            date_tag = start_dt.strftime("%Y-%m-%d")

            try:
                # Agar Auto-Expiry ON hai to har cycle me verify karein ki nearest expiry active hai
                if self.auto_expiry_mode:
                    fresh_expiries = fetch_available_expiries(self.asset)
                    if fresh_expiries:
                        self.available_expiries = fresh_expiries
                        self.expiry = fresh_expiries[0]

                raw_opt = fetch_option_chain(self.asset, self.expiry)
                raw_fut = fetch_futures_data(self.asset)

                opt_rows = parse_records(raw_opt, ts_str, is_option=True)
                fut_rows = parse_records(raw_fut, ts_str, is_option=False)

                df_opt = pd.DataFrame(opt_rows)
                df_fut = pd.DataFrame(fut_rows)

                # 1. Option Chain Tab (Date-wise)
                if not df_opt.empty:
                    opt_tab = f"{self.asset}_Options_{date_tag}"
                    ws_opt = get_or_create_worksheet(
                        spreadsheet, opt_tab, list(df_opt.columns)
                    )
                    ws_opt.append_rows(
                        df_opt.values.tolist(), value_input_option="USER_ENTERED"
                    )

                # 2. Futures Tab (Date-wise)
                if not df_fut.empty:
                    fut_tab = f"{self.asset}_Futures_{date_tag}"
                    ws_fut = get_or_create_worksheet(
                        spreadsheet, fut_tab, list(df_fut.columns)
                    )
                    ws_fut.append_rows(
                        df_fut.values.tolist(), value_input_option="USER_ENTERED"
                    )

                # 3. Daily Analysis Tab
                if not df_opt.empty:
                    analysis_dict = build_analysis_row(
                        df_opt, df_fut, ts_str, self.expiry
                    )
                    ws_ana = get_or_create_worksheet(
                        spreadsheet, "Daily_Analysis", list(analysis_dict.keys())
                    )
                    ws_ana.append_row(
                        list(analysis_dict.values()),
                        value_input_option="USER_ENTERED",
                    )
                    self.latest_analysis = analysis_dict

                self.latest_options_df = df_opt
                self.latest_futures_df = df_fut
                self.last_run = ts_
