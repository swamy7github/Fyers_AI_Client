import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional

class SEPAUniverseRanker:
    """
    Batch Screener & Ranking Engine for 2,000+ Stock Universes based on:
      1. SEPA Fundamental Growth Score (EPS Acceleration, Sales, Margins, ROE, Debt/Equity)
      2. Technical Stage 2 Trend Template & VCP Contraction Quality
      3. Risk-to-Reward (R:R) Efficiency Ratio

    Outputs a sorted dataframe/list ranking stocks from highest to lowest potential.
    """
    def __init__(self, account_size: float = 100000.0, risk_pct_per_trade: float = 1.0):
        self.account_size = account_size
        self.risk_pct_per_trade = risk_pct_per_trade

    def evaluate_stock(self, ticker: str, df_ohlcv: pd.DataFrame, fundamentals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Evaluates a single stock's Technicals, Fundamentals, and Risk:Reward dynamics.
        Returns a dictionary with metric scores or None if hard technical criteria fail.
        """
        if df_ohlcv is None or len(df_ohlcv) < 200:
            return None  # Needs sufficient historical price data for 200 SMA

        df = df_ohlcv.copy()
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])
            df.set_index('Date', inplace=True)
        df.sort_index(inplace=True)

        close = df['Close']
        high = df['High']
        low = df['Low']
        volume = df['Volume']

        # --- 1. TECHNICAL INDICATORS ---
        sma50 = close.rolling(50).mean().iloc[-1]
        sma150 = close.rolling(150).mean().iloc[-1]
        sma200 = close.rolling(200).mean().iloc[-1]
        sma200_21d = close.rolling(200).mean().shift(21).iloc[-1]
        
        high_52w = high.rolling(252, min_periods=100).max().iloc[-1]
        low_52w = low.rolling(252, min_periods=100).min().iloc[-1]
        vol_sma50 = volume.rolling(50).mean().iloc[-1]

        latest_close = close.iloc[-1]
        latest_vol = volume.iloc[-1]

        # --- 2. MINERVINI TREND TEMPLATE CHECK (STAGE 2) ---
        c1 = latest_close > sma150 and latest_close > sma200
        c2 = sma150 > sma200
        c3 = not pd.isna(sma200_21d) and (sma200 > sma200_21d)
        c4 = sma50 > sma150 and sma50 > sma200
        c5 = latest_close > sma50
        c6 = latest_close >= (low_52w * 1.30)   # >= 30% above 52w low
        c7 = latest_close >= (high_52w * 0.75)  # Within 25% of 52w high

        trend_template_pass = c1 and c2 and c3 and c4 and c5 and c6 and c7
        if not trend_template_pass:
            return None  # Filter out stocks not in Stage 2

        # --- 3. VCP CONTRACTION & PIVOT ANALYSIS ---
        recent_20 = df.iloc[-20:]
        pivot_high = round(float(recent_20['High'].max()), 2)
        tight_low = round(float(recent_20['Low'].min()), 2)
        contraction_depth = round(float(((pivot_high - tight_low) / pivot_high) * 100.0), 2)
        
        vol_dry_up = recent_20['Volume'].mean() < (vol_sma50 * 0.8)
        vol_surge = latest_vol >= (1.4 * vol_sma50)

        # VCP Technical Score (0 - 100)
        vcp_score = 100.0
        if contraction_depth > 15.0:
            vcp_score -= (contraction_depth - 15.0) * 3.0  # Penalize looser bases
        if not vol_dry_up:
            vcp_score -= 15.0
        if vol_surge:
            vcp_score += 15.0
        vcp_score = max(0.0, min(100.0, vcp_score))

        # --- 4. SEPA FUNDAMENTAL SCORE (0 - 100) ---
        fund_score = 0.0
        eps_g = fundamentals.get('eps_growth_qtr_yoy', 0.0)
        sales_g = fundamentals.get('sales_growth_qtr_yoy', 0.0)
        roe_val = fundamentals.get('roe', 0.0)
        de_ratio = fundamentals.get('debt_to_equity', 1.0)
        margin_exp = fundamentals.get('margin_expanding', False)
        eps_accel = fundamentals.get('eps_acceleration', False)

        if eps_g >= 20.0: fund_score += 20.0
        if eps_g >= 40.0: fund_score += 15.0  # Bonus for high EPS velocity
        if eps_accel: fund_score += 15.0
        if sales_g >= 15.0: fund_score += 15.0
        if sales_g >= 30.0: fund_score += 10.0
        if margin_exp: fund_score += 10.0
        if roe_val >= 15.0: fund_score += 10.0
        if de_ratio <= 0.5: fund_score += 5.0
        fund_score = max(0.0, min(100.0, fund_score))

        # --- 5. RISK TO RETURN DYNAMICS (R:R RATIO) ---
        raw_risk_pct = ((pivot_high - tight_low) / pivot_high) * 100.0
        risk_pct = min(8.0, max(2.5, raw_risk_pct)) # Capped at max 8% stop
        
        stop_loss_price = round(pivot_entry if False else pivot_high * (1.0 - (risk_pct / 100.0)), 2)
        risk_per_share = round(pivot_high - stop_loss_price, 2)

        # Upside Potential Targets
        target_2r = round(pivot_high + (2.0 * risk_per_share), 2)
        target_3r = round(pivot_high + (3.0 * risk_per_share), 2)
        upside_3r_pct = round(((target_3r - pivot_high) / pivot_high) * 100.0, 2)

        # Risk-to-Reward Efficiency Score (0 - 100)
        # Higher reward potential with tighter stop risk = higher efficiency score
        rr_ratio = 3.0  # Default 3:1 reward-to-risk
        rr_efficiency_score = max(0.0, min(100.0, (upside_3r_pct / risk_pct) * 25.0))

        # --- 6. COMPOSITE PROFITABILITY SCORE ---
        # Weights: Fundamentals (40%), VCP Setup (35%), R:R Efficiency (25%)
        composite_rank_score = round(
            (fund_score * 0.40) + (vcp_score * 0.35) + (rr_efficiency_score * 0.25), 2
        )

        # Position Sizing
        dollar_risk = self.account_size * (self.risk_pct_per_trade / 100.0)
        shares_to_buy = int(dollar_risk // risk_per_share) if risk_per_share > 0 else 0
        total_position_cost = round(shares_to_buy * pivot_high, 2)

        return {
            "Ticker": ticker,
            "Composite_Score": composite_rank_score,
            "Fundamental_Score": round(fund_score, 1),
            "VCP_Setup_Score": round(vcp_score, 1),
            "RR_Efficiency_Score": round(rr_efficiency_score, 1),
            "Target_Entry_Price": pivot_high,
            "Stop_Loss_Price": stop_loss_price,
            "Risk_Pct": round(risk_pct, 2),
            "Target_2R_Price": target_2r,
            "Target_3R_Price": target_3r,
            "Upside_3R_Pct": upside_3r_pct,
            "Shares_To_Buy": shares_to_buy,
            "Position_Cost": total_position_cost,
            "EPS_Growth_YoY": f"{eps_g}%",
            "Sales_Growth_YoY": f"{sales_g}%",
            "Contraction_Depth_Pct": f"{contraction_depth}%"
        }

    def process_universe(self, universe_data: List[Dict[str, Any]]) -> pd.DataFrame:
        """
        Processes batch universe of stocks and returns a sorted DataFrame.
        Each item in universe_data must contain:
          - 'ticker': str
          - 'ohlcv': pd.DataFrame
          - 'fundamentals': dict
        """
        qualified_results = []

        for stock in universe_data:
            ticker = stock['ticker']
            df_ohlcv = stock.get('ohlcv')
            fundamentals = stock.get('fundamentals', {})

            res = self.evaluate_stock(ticker, df_ohlcv, fundamentals)
            if res is not None:
                qualified_results.append(res)

        if not qualified_results:
            return pd.DataFrame()

        results_df = pd.DataFrame(qualified_results)
        # Sort by Composite Score descending
        results_df.sort_values(by="Composite_Score", ascending=False, inplace=True)
        results_df.reset_index(drop=True, inplace=True)
        results_df.index += 1  # 1-based ranking index
        return results_df

# --- Demo Batch Test ---
if __name__ == "__main__":
    np.random.seed(42)
    n_days = 300
    dates = pd.date_range("2025-01-01", periods=n_days, freq="B")

    # Generate 5 mock stocks with varying fundamental/technical profiles
    sample_universe = []
    tickers = ["TICKER_A.NS", "TICKER_B.NS", "TICKER_C.NS", "TICKER_D.NS", "TICKER_E.NS"]
    
    fundamentals_list = [
        {"eps_growth_qtr_yoy": 45.0, "eps_acceleration": True, "sales_growth_qtr_yoy": 32.0, "roe": 22.0, "debt_to_equity": 0.15, "margin_expanding": True}, # A: Top Tier
        {"eps_growth_qtr_yoy": 25.0, "eps_acceleration": False, "sales_growth_qtr_yoy": 18.0, "roe": 16.0, "debt_to_equity": 0.35, "margin_expanding": True}, # B: Mid Tier
        {"eps_growth_qtr_yoy": 5.0, "eps_acceleration": False, "sales_growth_qtr_yoy": 8.0, "roe": 10.0, "debt_to_equity": 0.8, "margin_expanding": False},  # C: Weak
        {"eps_growth_qtr_yoy": 60.0, "eps_acceleration": True, "sales_growth_qtr_yoy": 42.0, "roe": 25.0, "debt_to_equity": 0.10, "margin_expanding": True}, # D: Exceptional
        {"eps_growth_qtr_yoy": 30.0, "eps_acceleration": True, "sales_growth_qtr_yoy": 22.0, "roe": 18.0, "debt_to_equity": 0.25, "margin_expanding": True}  # E: Strong
    ]

    for i, t in enumerate(tickers):
        trend = np.linspace(100, 200 + (i * 10), 280)
        consolidation = np.array([200 + (i * 10)] * 20)
        prices = np.concatenate([trend, consolidation]) + np.random.normal(0, 1.0, n_days)
        
        highs = prices + 2.0
        lows = prices - 2.0
        df = pd.DataFrame({
            "Date": dates, "Open": prices, "High": highs, "Low": lows, "Close": prices,
            "Volume": np.random.randint(500000, 1500000, size=n_days)
        })
        sample_universe.append({"ticker": t, "ohlcv": df, "fundamentals": fundamentals_list[i]})

    ranker = SEPAUniverseRanker(account_size=100000.0, risk_pct_per_trade=1.0)
    ranked_df = ranker.process_universe(sample_universe)
    print(ranked_df[["Ticker", "Composite_Score", "Fundamental_Score", "VCP_Setup_Score", "Target_Entry_Price", "Stop_Loss_Price", "Target_3R_Price", "Upside_3R_Pct"]])
"""
import yfinance as yf

# Append '.NS' to symbol for the NSE market
ticker = yf.Ticker("TCS.NS") 

# Fetch ratios and statements
info = ticker.info
financials = ticker.quarterly_financials

# Example extractions
roe = info.get('returnOnEquity') * 100
debt_to_equity = info.get('debtToEquity')

=============================================
=============SCREENER.IN=====================
=============================================
YOY Quarterly profit growth > 45 AND 
Profit growth last quarter > YOY Quarterly profit growth AND 
YOY Quarterly sales growth > 32 AND 
Return on equity > 22 AND 
Debt to equity < 0.15 AND 
Net profit margin > Net profit margin preceding quarter

Your JSON Key	==> Screener.in Equivalence	Why/How it Works
"eps_growth_qtr_yoy": 45.0====> 	YOY Quarterly profit growth > 45	Because equity share counts rarely change drastically quarter-over-quarter, Net Profit growth YoY serves as the direct proxy for EPS growth YoY.
"eps_acceleration": True	===> Profit growth last quarter > YOY Quarterly profit growth	Compares the absolute most recent quarter's growth rate against the trailing annualized average to isolate accelerating earnings momentum.
"sales_growth_qtr_yoy": 32.0	===> YOY Quarterly sales growth > 32	Evaluates top-line quarterly revenue expansion relative to the same period in the previous fiscal year.
"roe": 22.0	======> Return on equity > 22	Identifies companies generating high returns on shareholder capital.
"debt_to_equity": 0.15	=====> Debt to equity < 0.15	Filters for highly conservative leverage (clean balance sheets).
"margin_expanding": True ======>	Net profit margin > Net profit margin preceding quarter	Confirms operational efficiency by ensuring profit margins are widening sequentially.


"""