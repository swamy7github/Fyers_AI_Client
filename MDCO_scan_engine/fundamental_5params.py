import yfinance as yf
import pandas as pd
import numpy as np

def calculate_fundamental_metrics(ticker_symbol: str) -> dict:
    """
    Fetches fundamental statements via yfinance and computes:
    1. eps_growth_qtr_yoy
    2. sales_growth_qtr_yoy
    3. eps_acceleration
    4. debt_to_equity
    5. margin_expanding
    """
    ticker = yf.Ticker(ticker_symbol)
    
    # 1. Fetch quarterly financial statements
    inc = ticker.quarterly_income_stmt
    bal = ticker.quarterly_balance_sheet
    
    if inc is None or inc.empty or bal is None or bal.empty:
        return {"error": f"Insufficient statement data returned for {ticker_symbol}"}
    
    # Sort columns chronologically ascending (Oldest -> Newest)
    inc = inc.sort_index(axis=1, ascending=True)
    bal = bal.sort_index(axis=1, ascending=True)
    
    # Helper function to find matching row key from common yfinance aliases
    def extract_row(df, candidate_keys):
        for key in candidate_keys:
            if key in df.index:
                return df.loc[key]
        return None

    # Extract required metric series
    eps_series = extract_row(inc, ['Diluted EPS', 'Basic EPS'])
    rev_series = extract_row(inc, ['Total Revenue', 'Operating Revenue'])
    op_inc_series = extract_row(inc, ['Operating Income', 'EBIT'])
    debt_series = extract_row(bal, ['Total Debt'])
    equity_series = extract_row(bal, ['Stockholders Equity', 'Total Equity Gross Minority Interest'])

    num_qtrs = inc.shape[1]
    metrics = {
        "ticker": ticker_symbol,
        "quarters_available": num_qtrs,
        "latest_quarter_date": str(inc.columns[-1].date()) if num_qtrs > 0 else None
    }

    # Helper function for YoY Growth with Absolute Denominator: (Q_t - Q_t-4) / |Q_t-4|
    def calc_yoy_growth(series, target_idx, base_idx):
        if series is None or len(series) <= max(target_idx, base_idx):
            return None
        val_t = series.iloc[target_idx]
        val_base = series.iloc[base_idx]
        if pd.isna(val_t) or pd.isna(val_base) or val_base == 0:
            return None
        return (val_t - val_base) / abs(val_base)

    # --- Metric 1: eps_growth_qtr_yoy (Comparing latest quarter t against t-4) ---
    g_eps_t = None
    if num_qtrs >= 5 and eps_series is not None:
        # Index -1 is current quarter (t), Index -5 is same quarter last year (t-4)
        g_eps_t = calc_yoy_growth(eps_series, -1, -5)
        metrics["eps_growth_qtr_yoy"] = round(g_eps_t, 4) if g_eps_t is not None else None
    else:
        metrics["eps_growth_qtr_yoy"] = None

    # --- Metric 2: sales_growth_qtr_yoy (Comparing latest quarter t against t-4) ---
    if num_qtrs >= 5 and rev_series is not None:
        g_sales_t = calc_yoy_growth(rev_series, -1, -5)
        metrics["sales_growth_qtr_yoy"] = round(g_sales_t, 4) if g_sales_t is not None else None
    else:
        metrics["sales_growth_qtr_yoy"] = None

    # --- Metric 3: eps_acceleration (g_eps_t - g_eps_t-1) ---
    # Requires 6 quarters (t, t-1, t-4, t-5)
    if num_qtrs >= 6 and eps_series is not None:
        g_eps_t1 = calc_yoy_growth(eps_series, -2, -6)  # YoY growth for previous quarter (t-1 vs t-5)
        if g_eps_t is not None and g_eps_t1 is not None:
            metrics["eps_acceleration"] = round(g_eps_t - g_eps_t1, 4)
        else:
            metrics["eps_acceleration"] = None
    else:
        metrics["eps_acceleration"] = "N/A (Requires 6 trailing quarters; caching needed)"

    # --- Metric 4: debt_to_equity (Total Debt / Stockholders Equity) ---
    if debt_series is not None and equity_series is not None and len(debt_series) > 0 and len(equity_series) > 0:
        latest_debt = debt_series.iloc[-1]
        latest_equity = equity_series.iloc[-1]
        
        if pd.notna(latest_debt) and pd.notna(latest_equity):
            if latest_equity > 0:
                metrics["debt_to_equity"] = round(latest_debt / latest_equity, 4)
            else:
                metrics["debt_to_equity"] = "N/M (Negative Equity)"
        else:
            metrics["debt_to_equity"] = None
    else:
        metrics["debt_to_equity"] = None

    # --- Metric 5: margin_expanding (Operating Margin t vs t-4) ---
    if num_qtrs >= 5 and op_inc_series is not None and rev_series is not None:
        op_t, rev_t = op_inc_series.iloc[-1], rev_series.iloc[-1]
        op_t4, rev_t4 = op_inc_series.iloc[-5], rev_series.iloc[-5]
        
        if all(pd.notna(x) for x in [op_t, rev_t, op_t4, rev_t4]) and rev_t > 0 and rev_t4 > 0:
            om_t = op_t / rev_t
            om_t4 = op_t4 / rev_t4
            metrics["margin_expanding"] = bool(om_t > om_t4)
        else:
            metrics["margin_expanding"] = None
    else:
        metrics["margin_expanding"] = None

    return metrics


# --- Example Usage ---
if __name__ == "__main__":
    # Test on NSE symbol (Infosys) and US symbol (Apple)
    tickers_to_test = ["COFORGE.NS", "SKYGOLD.NS"]
    
    for symbol in tickers_to_test:
        print(f"\n--- Calculating Metrics for: {symbol} ---")
        results = calculate_fundamental_metrics(symbol)
        for key, value in results.items():
            print(f"{key}: {value}")