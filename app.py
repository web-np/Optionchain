import os
import time
from datetime import datetime
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
import pandas as pd
import requests

# ==============================================================================
# SECTION 1: CONFIGURATION & SETTINGS
# Kaam: Yahan aap Asset (BTC/ETH), Expiry Date aur API ka Base URL set karte hain.
# ==============================================================================
BASE_URL = "https://api.india.delta.exchange/v2"  # Delta Exchange India Production URL
UNDERLYING_ASSET = "ETH"  # 'ETH' ya 'BTC' set karein
EXPIRY_DATE = "14-11-2026"  # Option Expiry Date (Format: DD-MM-YYYY) - Live expiry dalein!
INTERVAL_SECONDS = 60  # Har 60 seconds (1 minute) me data record hoga

# Request headers (Document ke hisab se User-Agent bhejna zaroori hai taaki 403 error na aaye)
HEADERS = {"Accept": "application/json", "User-Agent": "python-delta-recorder"}


# ==============================================================================
# SECTION 2: DATA FETCHING FUNCTIONS (REST API)
# Kaam: Delta Exchange ke public /v2/tickers endpoint se Option Chain aur Futures ka data lana.
# ==============================================================================
def fetch_option_chain(asset: str, expiry: str) -> list:
    """Diye gaye asset aur expiry ke sabhi Call aur Put options ka live ticker data lata hai."""
    url = f"{BASE_URL}/tickers"
    params = {
        "contract_types": "call_options,put_options",
        "underlying_asset_symbols": asset,
        "expiry_date": expiry,
    }
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            return data.get("result", [])
        else:
            print(f"[API Error - Options]: {data}")
            return []
    except Exception as e:
        print(f"[Network Error - Options]: {e}")
        return []


def fetch_futures_data(asset: str) -> list:
    """Diye gaye asset ke Perpetual aur Dated Futures ka live ticker data lata hai."""
    url = f"{BASE_URL}/tickers"
    params = {
        "contract_types": "perpetual_futures,futures",
        "underlying_asset_symbols": asset,
    }
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            return data.get("result", [])
        else:
            print(f"[API Error - Futures]: {data}")
            return []
    except Exception as e:
        print(f"[Network Error - Futures]: {e}")
        return []


# ==============================================================================
# SECTION 3: DATA PARSING & CLEANING
# Kaam: API se aaye raw JSON data me se zaroori fields (Market price, Bid/Ask, Volume, OI, Greeks)
#       nikal kar ek clean tabular format (list of dictionaries) me convert karna.
# ==============================================================================
def parse_ticker_records(
    raw_list: list, record_time: str, is_option: bool = True
) -> list:
    parsed_rows = []
    for item in raw_list:
        quotes = item.get("quotes") or {}
        greeks = item.get("greeks") or {}

        row = {
            "Timestamp": record_time,
            "Symbol": item.get("symbol"),
            "Contract_Type": item.get("contract_type"),
            "Strike_Price": (
                float(item.get("strike_price") or 0) if is_option else None
            ),
            "Spot_Price": float(item.get("spot_price") or 0),
            "Market_Price (Mark)": float(item.get("mark_price") or 0),
            "Last_Traded_Price (Close)": float(item.get("close") or 0),
            "Best_Bid (Buy_Price)": float(quotes.get("best_bid") or 0),
            "Bid_Size": float(quotes.get("bid_size") or 0),
            "Best_Ask (Ask_Price)": float(quotes.get("best_ask") or 0),
            "Ask_Size": float(quotes.get("ask_size") or 0),
            "Volume": float(item.get("volume") or 0),
            "Turnover_USD": float(item.get("turnover_usd") or 0),
            "Open_Interest (OI)": float(item.get("oi") or 0),
            "OI_Value_USD": float(item.get("oi_value_usd") or 0),
            "24h_Change_%": float(item.get("ltp_change_24h") or 0),
        }

        # Options ke liye extra IV aur Greeks add karna
        if is_option:
            row.update({
                "Bid_IV": float(quotes.get("bid_iv") or 0),
                "Ask_IV": float(quotes.get("ask_iv") or 0),
                "Mark_IV": float(item.get("mark_vol") or 0),
                "Delta": float(greeks.get("delta") or 0),
                "Gamma": float(greeks.get("gamma") or 0),
                "Theta": float(greeks.get("theta") or 0),
                "Vega": float(greeks.get("vega") or 0),
            })
        else:
            # Futures ke liye High/Low add karna
            row.update({
                "24h_High": float(item.get("high") or 0),
                "24h_Low": float(item.get("low") or 0),
            })

        parsed_rows.append(row)

    # Options ko Strike Price ke ascending order me sort karna
    if is_option and parsed_rows:
        parsed_rows.sort(
            key=lambda x: (x["Strike_Price"], x["Contract_Type"])
        )

    return parsed_rows


# ==============================================================================
# SECTION 4: DAILY ANALYSIS GENERATOR
# Kaam: Har 1 minute ke option snapshot se PCR (Put-Call Ratio), Max Call OI (Resistance),
#       Max Put OI (Support), aur Total Call/Put Volume calculate karna.
# ==============================================================================
def generate_analysis_summary(
    df_options: pd.DataFrame, df_futures: pd.DataFrame, record_time: str
) -> pd.DataFrame:
    if df_options.empty:
        return pd.DataFrame()

    calls = df_options[df_options["Contract_Type"] == "call_options"]
    puts = df_options[df_options["Contract_Type"] == "put_options"]

    total_call_oi = calls["Open_Interest (OI)"].sum()
    total_put_oi = puts["Open_Interest (OI)"].sum()
    pcr_oi = round(total_put_oi / total_call_oi, 4) if total_call_oi > 0 else 0

    total_call_vol = calls["Volume"].sum()
    total_put_vol = puts["Volume"].sum()
    pcr_vol = (
        round(total_put_vol / total_call_vol, 4) if total_call_vol > 0 else 0
    )

    # Highest OI wale strikes (Support & Resistance levels)
    max_call_oi_strike = (
        calls.loc[calls["Open_Interest (OI)"].idxmax(), "Strike_Price"]
        if not calls.empty
        else 0
    )
    max_put_oi_strike = (
        puts.loc[puts["Open_Interest (OI)"].idxmax(), "Strike_Price"]
        if not puts.empty
        else 0
    )

    spot_price = df_options["Spot_Price"].iloc[0]

    # Perpetual Futures ka mark price aur volume nikalna
    perp_fut = df_futures[df_futures["Contract_Type"] == "perpetual_futures"]
    fut_price = (
        perp_fut["Market_Price (Mark)"].iloc[0] if not perp_fut.empty else 0
    )
    fut_vol = perp_fut["Volume"].iloc[0] if not perp_fut.empty else 0

    # Market Sentiment signal
    sentiment = (
        "Bullish"
        if pcr_oi > 1.1
        else ("Bearish" if pcr_oi < 0.8 else "Sideways / Neutral")
    )

    summary_row = {
        "Timestamp": record_time,
        "Spot_Price": spot_price,
        "Perp_Futures_Price": fut_price,
        "Perp_Futures_Volume": fut_vol,
        "Total_Call_OI": total_call_oi,
        "Total_Put_OI": total_put_oi,
        "PCR (OI_Based)": pcr_oi,
        "Total_Call_Volume": total_call_vol,
        "Total_Put_Volume": total_put_vol,
        "PCR (Volume_Based)": pcr_vol,
        "Resistance (Max_Call_OI_Strike)": max_call_oi_strike,
        "Support (Max_Put_OI_Strike)": max_put_oi_strike,
        "Market_Sentiment": sentiment,
    }
    return pd.DataFrame([summary_row])


# ==============================================================================
# SECTION 5: EXCEL STORAGE & FORMATTING
# Kaam: Data ko daily Excel file me 3 alag-alag tabs (Daily_Analysis, Option_Chain_1m,
#       Futures_1m) me append karna aur header styling apply karna.
# ==============================================================================
def append_df_to_excel(file_path: str, sheet_name: str, df: pd.DataFrame):
    if df.empty:
        return

    if not os.path.exists(file_path):
        with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=sheet_name, index=False)
        style_excel_sheet(file_path, sheet_name)
    else:
        wb = openpyxl.load_workbook(file_path)
        if sheet_name not in wb.sheetnames:
            wb.close()
            with pd.ExcelWriter(
                file_path, engine="openpyxl", mode="a"
            ) as writer:
                df.to_excel(writer, sheet_name=sheet_name, index=False)
            style_excel_sheet(file_path, sheet_name)
        else:
            ws = wb[sheet_name]
            for row in df.itertuples(index=False, name=None):
                ws.append(list(row))
            wb.save(file_path)
            wb.close()


def style_excel_sheet(file_path: str, sheet_name: str):
    """Nayi sheet banne par headers ko dark color, bold white font aur auto column width deta hai."""
    wb = openpyxl.load_workbook(file_path)
    ws = wb[sheet_name]

    header_fill = PatternFill(
        start_color="1F4E78", end_color="1F4E78", fill_type="solid"
    )
    header_font = Font(color="FFFFFF", bold=True)

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # Auto-fit column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max_len + 4

    ws.freeze_panes = "A2"  # Top header row ko freeze karein
    wb.save(file_path)
    wb.close()


# ==============================================================================
# SECTION 6: MAIN EXECUTION LOOP (1-MINUTE SCHEDULER)
# Kaam: Har minute ke start par exact time sync karke data fetch aur save karna.
# ==============================================================================
def run_recorder():
    print(f"=== Delta Exchange 1-Minute Recorder Started ===")
    print(
        f"Asset: {UNDERLYING_ASSET} | Expiry: {EXPIRY_DATE} | Interval: {INTERVAL_SECONDS}s\n"
    )

    while True:
        now = datetime.now()
        current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        today_date_str = now.strftime("%Y-%m-%d")

        # Har din ke liye nayi Excel file banegi
        excel_filename = (
            f"Delta_{UNDERLYING_ASSET}_Data_{today_date_str}.xlsx"
        )

        print(f"[{current_time_str}] Fetching Option Chain & Futures data...")

        # 1. API se data fetch karein
        raw_options = fetch_option_chain(UNDERLYING_ASSET, EXPIRY_DATE)
        raw_futures = fetch_futures_data(UNDERLYING_ASSET)

        # 2. Data ko clean aur parse karein
        option_rows = parse_ticker_records(
            raw_options, current_time_str, is_option=True
        )
        futures_rows = parse_ticker_records(
            raw_futures, current_time_str, is_option=False
        )

        df_options = pd.DataFrame(option_rows)
        df_futures = pd.DataFrame(futures_rows)

        # 3. Daily Analysis summary calculate karein
        df_analysis = generate_analysis_summary(
            df_options, df_futures, current_time_str
        )

        # 4. Excel file ke 3 tabs me save karein
        append_df_to_excel(excel_filename, "Daily_Analysis", df_analysis)
        append_df_to_excel(excel_filename, "Option_Chain_1m", df_options)
        append_df_to_excel(excel_filename, "Futures_1m", df_futures)

        print(
            f"   -> Saved {len(df_options)} Options & {len(df_futures)} Futures rows to '{excel_filename}'"
        )
        if not df_analysis.empty:
            pcr = df_analysis["PCR (OI_Based)"].iloc[0]
            sup = df_analysis["Support (Max_Put_OI_Strike)"].iloc[0]
            res = df_analysis["Resistance (Max_Call_OI_Strike)"].iloc[0]
            print(
                f"   -> Live Snapshot | Spot: {df_analysis['Spot_Price'].iloc[0]} | PCR: {pcr} | Support: {sup} | Resistance: {res}\n"
            )

        # Agle exact minute tak wait karein (drift avoid karne ke liye)
        elapsed = (datetime.now() - now).total_seconds()
        sleep_time = max(1, INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    try:
        run_recorder()
    except KeyboardInterrupt:
        print("\nRecorder manually stopped by user. All data safely saved!")
