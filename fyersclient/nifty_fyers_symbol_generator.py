from datetime import datetime, timedelta
import calendar
import csv
import os

class Config:
    NIFTY_FO_SYMBOL_MASTER_CSV = "nifty_fo_symbol_master.csv"

def get_monthly_expiry(target_date_str: str) -> datetime:
    """
    Given a date (YYYY-MM-DD), returns the monthly expiry date 
    (the last Tuesday of that month, per NSE revised expiry rules).
    """
    dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    year = dt.year
    month = dt.month
    
    last_day = calendar.monthrange(year, month)[1]
    last_date = datetime(year, month, last_day)
    
    offset = (last_date.weekday() - 1) % 7
    last_tuesday = last_date - timedelta(days=offset)
    
    return last_tuesday

def get_weekly_expiry(target_date_str: str) -> datetime:
    """
    Given a date (YYYY-MM-DD), returns the upcoming or current week's weekly expiry date 
    (Tuesday of that week, per NSE revised rules).
    """
    dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    if dt.weekday() <= 1:
        days_until_tuesday = 1 - dt.weekday()
    else:
        days_until_tuesday = 7 - (dt.weekday() - 1)
        
    weekly_expiry = dt + timedelta(days=days_until_tuesday)
    return weekly_expiry

def get_fyers_month_code(month: int) -> str:
    """
    FYERS single-character month representation for weekly options:
    Jan-Sep: 1..9, Oct: O, Nov: N, Dec: D
    """
    if 1 <= month <= 9:
        return str(month)
    codes = {10: "O", 11: "N", 12: "D"}
    return codes.get(month, "1")

def generate_fyers_nfo_symbols(
    target_date_str: str, 
    spot_price: float, 
    csv_filename: str,
    symbol_name: str = "NIFTY", 
    num_strikes: int = 20, 
    expiry_type: str = "monthly"
) -> list:
    """
    Generates FYERS option symbols in the specified dictionary format and writes to CSV.
    """
    symbol_clean = symbol_name.upper().strip()
    
    # Set strike step and lot sizes
    if symbol_clean == "BANKNIFTY":
        strike_step = 100
        lot_size = 30
    else:
        strike_step = 50
        lot_size = 65
        
    if expiry_type.lower() == "weekly" and symbol_clean == "BANKNIFTY":
        print("[NOTICE] BANKNIFTY weekly contracts are discontinued. Defaulting to monthly.")
        expiry_type = "monthly"
            
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
        
        # Call Option Dict
        ce_symbol = f"NSE:{symbol_clean}{date_str_part}{strike}CE"
        ce_dict = {
            "underlying_symbol": symbol_clean,
            "option_type": "CE",
            "expiry_date": expiry_str,
            "strike_price": strike,
            "trading_symbol": ce_symbol,
            "lot_size": lot_size
        }
        
        # Put Option Dict
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

def save_contracts_to_csv(contracts: list, filename: str):
    """
    Writes the list of option contract dictionaries to CSV.
    """
    fieldnames = ["underlying_symbol", "option_type", "expiry_date", "strike_price", "trading_symbol", "lot_size"]
    with open(filename, mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for contract in contracts:
            writer.writerow(contract)
    print(f"[SUCCESS] Successfully saved {len(contracts)} contracts to {filename}")

if __name__ == "__main__":
    input_date = "2026-10-12"
    nifty_spot = 22363.35
    
    # Generate dictionary list for monthly NIFTY options
    nifty_contracts = generate_fyers_nfo_symbols(
        target_date_str=input_date, 
        spot_price=nifty_spot, 
        symbol_name="NIFTY", 
        num_strikes=20, 
        expiry_type="weekly",
        csv_filename=Config.NIFTY_FO_SYMBOL_MASTER_CSV
    )
    
    # Save to config.NIFTY_FO_SYMBOL_MASTER_CSV
    #save_contracts_to_csv(nifty_contracts)
    
    print("\nSample Generated Dictionary Records:")
    for row in nifty_contracts[:4]:
        print(row)