from datetime import datetime, timedelta
import calendar
import csv
import os

try:
    import pandas_market_calendars as mcal
    import pandas as pd
    HAS_MCAL = True
except ImportError:
    HAS_MCAL = False

class Config:
    NIFTY_FO_SYMBOL_MASTER_CSV = "nifty_fo_symbol_master.csv"

# Fallback official NSE trading holidays for 2026 if mcal is unavailable
NSE_TRADING_HOLIDAYS_FALLBACK = {
    "2026-01-26", # Republic Day
    "2026-03-21", # Id-Ul-Fitr
    "2026-10-02", # Mahatma Gandhi Birthday
    "2026-10-20", # Dussehra (Shifts weekly expiry to Oct 19, 2026)
    "2026-11-24", # Guru Nanak Jayanti
    "2026-12-25"  # Christmas
}

def is_nse_trading_day(date_obj: datetime) -> bool:
    """
    Checks if a given date is an open trading day on the NSE.
    Uses pandas_market_calendars if installed, otherwise falls back to weekends & manual list.
    """
    date_str = date_obj.strftime("%Y-%m-%d")
    
    # Weekend check (Saturday = 5, Sunday = 6)
    if date_obj.weekday() >= 5:
        return False
        
    if HAS_MCAL:
        try:
            nse = mcal.get_calendar("NSE")
            start_str = (date_obj - timedelta(days=3)).strftime("%Y-%m-%d")
            end_str = (date_obj + timedelta(days=3)).strftime("%Y-%m-%d")
            valid_days = nse.valid_days(start_date=start_str, end_date=end_str)
            valid_date_strs = [d.strftime("%Y-%m-%d") for d in valid_days]
            return date_str in valid_date_strs
        except Exception:
            pass
            
    # Fallback check
    return date_str not in NSE_TRADING_HOLIDAYS_FALLBACK

def get_monthly_expiry(target_date_str: str) -> datetime:
    """
    API Method: Given any date string (YYYY-MM-DD), calculates the upcoming/next 
    monthly expiry (the last Tuesday of that month, per NSE rules), adjusted backward for holidays.
    If the last Tuesday has already passed in that month, moves to the next month's expiry.
    """
    dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    year = dt.year
    month = dt.month
    
    def calculate_last_tuesday(y, m):
        last_day = calendar.monthrange(y, m)[1]
        last_date = datetime(y, m, last_day)
        # Tuesday weekday index is 1
        offset = (last_date.weekday() - 1) % 7
        last_tuesday = last_date - timedelta(days=offset)
        # Shift backward if holiday/weekend
        while not is_nse_trading_day(last_tuesday):
            last_tuesday -= timedelta(days=1)
        return last_tuesday

    monthly_exp = calculate_last_tuesday(year, month)
    
    # If the upcoming monthly expiry has already passed relative to input date, look at next month
    if monthly_exp.date() < dt.date():
        if month == 12:
            monthly_exp = calculate_last_tuesday(year + 1, 1)
        else:
            monthly_exp = calculate_last_tuesday(year, month + 1)
            
    return monthly_exp

def get_weekly_expiry(target_date_str: str) -> datetime:
    """
    API Method: Given any date string (YYYY-MM-DD), automatically finds the correct 
    upcoming weekly expiry date (Tuesday) for that week or next, adjusted backward for holidays.
    """
    dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    
    # Find Tuesday of the current week (Tuesday index = 1)
    current_weekday = dt.weekday()
    days_to_tuesday = 1 - current_weekday
    tuesday_of_week = dt + timedelta(days=days_to_tuesday)
    
    # Adjust for holiday/weekend to find actual scheduled trading day for that Tuesday's expiry
    actual_expiry = tuesday_of_week
    while not is_nse_trading_day(actual_expiry):
        actual_expiry -= timedelta(days=1)
        
    # If the upcoming/scheduled weekly expiry day has already passed, get next week's Tuesday
    if actual_expiry.date() < dt.date():
        tuesday_of_week += timedelta(days=7)
        actual_expiry = tuesday_of_week
        while not is_nse_trading_day(actual_expiry):
            actual_expiry -= timedelta(days=1)
            
    return actual_expiry

def get_fyers_month_code(month: int) -> str:
    """
    FYERS single-character month representation for weekly options:
    Jan-Sep: 1..9, Oct: O, Nav: N, Dec: D
    """
    if 1 <= month <= 9:
        return str(month)
    codes = {10: "O", 11: "N", 12: "D"}
    return codes.get(month, "1")

def generate_fyers_nfo_symbols(
    target_date_str: str, 
    spot_price: float, 
    csv_filename: str ,
    symbol_name: str = "NIFTY", 
    num_strikes: int = 20, 
    expiry_type: str = "Monthly",
    
) -> list:
    """
    Generates FYERS option symbols in the specified dictionary format 
    containing underlying, option_type, expiry, strike, trading_symbol, and lot_size.
    """
    symbol_clean = symbol_name.upper().strip()
    
    # Set strike step and lot sizes per index
    if symbol_clean == "BANKNIFTY":
        strike_step = 100
        lot_size = 30
        if expiry_type.lower() == "weekly":
            expiry_type = "monthly"  # Bank Nifty weekly options are discontinued
    else:
        strike_step = 50
        lot_size = 65
            
    if expiry_type.lower() == "weekly":
        expiry_date = get_weekly_expiry(target_date_str)
        yy = expiry_date.strftime("%y")
        m_code = get_fyers_month_code(expiry_date.month)
        dd = expiry_date.strftime("%d")
        date_str_part = f"{yy}{m_code}{dd}"
    else:
        expiry_date = get_monthly_expiry(target_date_str)
        yy = expiry_date.strftime("%y")
        mmm = expiry_date.strftime("%b").upper()
        date_str_part = f"{yy}{mmm}"
    
    atm_strike = round(spot_price / strike_step) * strike_step
    half_range = num_strikes // 2
    start_strike = atm_strike - (half_range * strike_step)
    
    contracts_list = []
    expiry_str = expiry_date.strftime("%Y-%m-%d")
    
    for i in range(num_strikes):
        strike = int(start_strike + (i * strike_step))
        
        ce_symbol = f"NSE:{symbol_clean}{date_str_part}{strike}CE"
        ce_dict = {
            "underlying_symbol": symbol_clean,
            "option_type": "CE",
            "expiry_date": expiry_str,
            "strike_price": strike,
            "trading_symbol": ce_symbol,
            "lot_size": lot_size
        }
        
        pe_symbol = f"NSE:{symbol_clean}{date_str_part}{strike}PE"
        pe_dict = {
            "underlying_symbol": symbol_clean,
            "option_type": "PE",
            "expiry_date": expiry_str,
            "strike_price": strike,
            "trading_symbol": pe_symbol,
            "lot_size": lot_size
        }
        
        contracts_list.append(ce_dict)
        contracts_list.append(pe_dict)
    save_contracts_to_csv(contracts_list, csv_filename)    
    return contracts_list

def save_contracts_to_csv(contracts: list, filename: str = Config.NIFTY_FO_SYMBOL_MASTER_CSV):
    """
    Writes the list of option contract dictionaries to CSV with requested columns.
    """
    fieldnames = ["underlying_symbol", "option_type", "expiry_date", "strike_price", "trading_symbol", "lot_size"]
    with open(filename, mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for contract in contracts:
            writer.writerow(contract)
    print(f"[SUCCESS] Successfully saved {len(contracts)} contracts to {filename}")

if __name__ == "__main__":
    # Test date during October 2026
    input_date = "2026-10-11"
    nifty_spot = 22363.35
    
    print(f"Target Input Date: {input_date} | Spot Price: {nifty_spot}")
    
    weekly_exp = get_weekly_expiry(input_date)
    monthly_exp = get_monthly_expiry(input_date)
    print(f"[API] Calculated Upcoming Weekly Expiry: {weekly_exp.strftime('%Y-%m-%d')}")
    print(f"[API] Calculated Upcoming Monthly Expiry: {monthly_exp.strftime('%Y-%m-%d')}")
    
    nifty_contracts = generate_fyers_nfo_symbols(
        target_date_str=input_date, 
        spot_price=nifty_spot, 
        symbol_name="NIFTY", 
        num_strikes=20, 
        expiry_type="Monthly",
        csv_filename = "net.csv"
    )
    
    save_contracts_to_csv(nifty_contracts)
    
    print("\nSample Generated Dictionary Records:")
    for row in nifty_contracts[:4]:
        print(row)