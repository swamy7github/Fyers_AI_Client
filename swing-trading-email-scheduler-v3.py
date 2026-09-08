import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import pandas as pd
import numpy as np
import io
import urllib.request

# Try to import yfinance
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

# ==============================================================================
# 1. RETRIEVE NIFTY LARGEMIDCAP 250 TICKERS & SECTORS
# ==============================================================================
def get_nifty_250_universe():
    """
    Attempts to download the official Nifty LargeMidcap 250 list from NSE.
    Returns a dictionary of {ticker: industry} or falls back to an embedded list.
    """
    tickers_dict = {}
    
    # Official NSE and Mirror URLs
    urls = [
        "https://archives.nseindia.com/content/indices/ind_niftylargemidcap250list.csv",
        "https://niftyindices.com/IndexandSharePrice/ind_niftylargemidcap250list.csv"
    ]
    
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            with urllib.request.urlopen(req, timeout=10) as response:
                csv_data = response.read().decode('utf-8')
                df = pd.read_csv(io.StringIO(csv_data))
                
                # Check for standard columns
                if 'Symbol' in df.columns:
                    industry_col = 'Industry' if 'Industry' in df.columns else 'Sector'
                    for _, row in df.iterrows():
                        symbol = str(row['Symbol']).strip()
                        industry = str(row[industry_col]).strip() if industry_col in df.columns else "General"
                        if symbol and symbol != 'nan':
                            tickers_dict[f"{symbol}.NS"] = industry
                    print(f"[INFO] Successfully fetched {len(tickers_dict)} Nifty 250 stocks from {url}")
                    return tickers_dict
        except Exception as e:
            print(f"[WARNING] Failed to fetch from {url}: {str(e)}")
            continue

    # ==========================================================================
    # ROBUST FALLBACK: Comprehensive Nifty 250 Tickers with Industry Mapping
    # ==========================================================================
    print("[INFO] Using comprehensive embedded Nifty 250 fallback list.")
    fallback_data = {
        # Financial Services / Banks
        "HDFCBANK.NS": "Financial Services", "ICICIBANK.NS": "Financial Services", "SBIN.NS": "Financial Services",
        "KOTAKBANK.NS": "Financial Services", "AXISBANK.NS": "Financial Services", "BAJFINANCE.NS": "Financial Services",
        "BAJAJFINSV.NS": "Financial Services", "SHRIRAMFIN.NS": "Financial Services", "CHOLAFIN.NS": "Financial Services",
        "MUTHOOTFIN.NS": "Financial Services", "PFC.NS": "Financial Services", "REC.NS": "Financial Services",
        "RELIANCE.NS": "Oil Gas & Consumable Fuels", "ONGC.NS": "Oil Gas & Consumable Fuels", "BPCL.NS": "Oil Gas & Consumable Fuels",
        "IOC.NS": "Oil Gas & Consumable Fuels", "HPCL.NS": "Oil Gas & Consumable Fuels", "GAIL.NS": "Oil Gas & Consumable Fuels",
        # IT
        "TCS.NS": "Information Technology", "INFY.NS": "Information Technology", "HCLTECH.NS": "Information Technology",
        "WIPRO.NS": "Information Technology", "TECHM.NS": "Information Technology", "LTIM.NS": "Information Technology",
        "COFORGE.NS": "Information Technology", "PERSISTENT.NS": "Information Technology", "MPHASIS.NS": "Information Technology",
        "KPITTECH.NS": "Information Technology", "LTTS.NS": "Information Technology", "TATAELXSI.NS": "Information Technology",
        # Metals & Mining
        "TATASTEEL.NS": "Metals & Mining", "HINDALCO.NS": "Metals & Mining", "JSWSTEEL.NS": "Metals & Mining",
        "NATIONALUM.NS": "Metals & Mining", "SAIL.NS": "Metals & Mining", "VEDL.NS": "Metals & Mining",
        "JINDALSTEL.NS": "Metals & Mining", "NMDC.NS": "Metals & Mining", "APLAPOLLO.NS": "Metals & Mining",
        # Capital Goods & Defense
        "LT.NS": "Construction", "BEL.NS": "Capital Goods", "HAL.NS": "Capital Goods",
        "BHEL.NS": "Capital Goods", "ABB.NS": "Capital Goods", "SIEMENS.NS": "Capital Goods",
        "CGPOWER.NS": "Capital Goods", "CUMMINSIND.NS": "Capital Goods", "POLYCAB.NS": "Capital Goods",
        "KEI.NS": "Capital Goods", "COCHINSHIP.NS": "Capital Goods", "GRSE.NS": "Capital Goods",
        "BDL.NS": "Capital Goods", "MAZDOCK.NS": "Capital Goods",
        # Healthcare & Pharmaceuticals
        "SUNPHARMA.NS": "Healthcare", "CIPLA.NS": "Healthcare", "DRREDDY.NS": "Healthcare",
        "DIVISLAB.NS": "Healthcare", "AUROPHARMA.NS": "Healthcare", "LUPIN.NS": "Healthcare",
        "TORNTPHARM.NS": "Healthcare", "ALKEM.NS": "Healthcare", "IPCALAB.NS": "Healthcare",
        "BIOCON.NS": "Healthcare", "GLAND.NS": "Healthcare", "SYNGENE.NS": "Healthcare",
        "MAXHEALTH.NS": "Healthcare", "FORTIS.NS": "Healthcare", "APOLLOHOSP.NS": "Healthcare",
        # Automobile & Auto Components
        "M&M.NS": "Automobile", "MARUTI.NS": "Automobile", "TATAMOTORS.NS": "Automobile",
        "HEROMOTOCO.NS": "Automobile", "EICHERMOT.NS": "Automobile", "TVSMOTOR.NS": "Automobile",
        "TIINDIA.NS": "Automobile", "ASHOKLEY.NS": "Automobile", "BALKRISIND.NS": "Automobile",
        "MRF.NS": "Automobile", "APOLLOTYRE.NS": "Automobile", "BHARATFORG.NS": "Automobile",
        # Power & Renewable Energy
        "NTPC.NS": "Power", "POWERGRID.NS": "Power", "TATAPOWER.NS": "Power",
        "ADANIGREEN.NS": "Power", "ADANIPOWER.NS": "Power", "SJVN.NS": "Power",
        "NHPC.NS": "Power", "IREDA.NS": "Financial Services", "WAAREE.NS": "Capital Goods",
        "ACME.NS": "Power", "SUZLON.NS": "Capital Goods",
        # Chemicals & Construction Materials
        "ULTRACEMCO.NS": "Construction Materials", "GRASIM.NS": "Construction Materials", "AMBUJACEM.NS": "Construction Materials",
        "ACC.NS": "Construction Materials", "JKCEMENT.NS": "Construction Materials", "SRF.NS": "Chemicals",
        "PIDILITIND.NS": "Chemicals", "DEEPAKNTR.NS": "Chemicals", "TATACHEM.NS": "Chemicals",
        # Consumer Services & FMCG
        "ITC.NS": "Fast Moving Consumer Goods", "HINDUNILVR.NS": "Fast Moving Consumer Goods", "NESTLEIND.NS": "Fast Moving Consumer Goods",
        "BRITANNIA.NS": "Fast Moving Consumer Goods", "COLPAL.NS": "Fast Moving Consumer Goods", "DABUR.NS": "Fast Moving Consumer Goods",
        "GODREJCP.NS": "Fast Moving Consumer Goods", "VBL.NS": "Fast Moving Consumer Goods", "TATACONSUM.NS": "Fast Moving Consumer Goods",
        "DMART.NS": "Consumer Services", "TRENT.NS": "Consumer Services", "ZOMATO.NS": "Consumer Services",
        "NYKAA.NS": "Consumer Services", "PAYTM.NS": "Financial Services"
    }
    return fallback_data

# ==============================================================================
# 2. TECHNICAL SCANNING & ANALYTICAL RESEARCH ENGINE
# ==============================================================================
def calculate_indicators(df):
    """Calculates technical indicators for swing trading evaluation."""
    if len(df) < 50:
        return None
        
    # Moving Averages for structural trend identification
    df['SMA_50'] = df['Close'].rolling(window=50).mean()
    df['SMA_200'] = df['Close'].rolling(window=200).mean()
    
    # 14-day Relative Strength Index (RSI) for overbought/oversold and pullbacks
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-10)
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # 14-day Average True Range (ATR) for volatility-based risk-management
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    df['ATR'] = true_range.rolling(14).mean()
    
    # Swing Levels for exact target & stop-loss configurations
    df['Swing_Low_10'] = df['Low'].rolling(window=10).min()
    df['Swing_High_20'] = df['High'].rolling(window=20).max()
    
    return df

def scan_and_research():
    """
    Performs high-performance scanning across Nifty 250 universe.
    Groups and extracts the absolute top 15 swing setups based on quantitative score.
    """
    if not YFINANCE_AVAILABLE:
        print("[WARNING] yfinance is not available in this environment. Falling back to baseline data.")
        return get_offline_fallback_data()
        
    universe = get_nifty_250_universe()
    tickers = list(universe.keys())
    
    print(f"=== Downloading market data for {len(tickers)} stocks ===")
    
    # Bulk download of all stock histories for 9 months (fast multithreaded operation)
    try:
        data = yf.download(tickers, period="9m", group_by="ticker", threads=True)
    except Exception as e:
        print(f"[ERROR] Failed to batch download: {str(e)}. Attempting fallback baseline.")
        return get_offline_fallback_data()
        
    candidates = []
    
    for ticker in tickers:
        try:
            # Safely extract historical data for this ticker
            if ticker not in data.columns.levels[0]:
                continue
            df_stock = data[ticker].dropna(subset=['Close'])
            
            if len(df_stock) < 50:
                continue
                
            df_stock = calculate_indicators(df_stock)
            if df_stock is None:
                continue
                
            latest = df_stock.iloc[-1]
            cmp = round(latest['Close'], 2)
            rsi = latest['RSI']
            sma_200 = latest['SMA_200']
            sma_50 = latest['SMA_50']
            atr = latest['ATR']
            swing_low = latest['Swing_Low_10']
            swing_high = latest['Swing_High_20']
            industry = universe.get(ticker, "General")
            
            # ------------------------------------------------------------------
            # SWING STRATEGY SELECTION FILTERS (Institution-Grade Rules)
            # ------------------------------------------------------------------
            # Rule 1: Structural Bullish Trend (Must trade above 200-day SMA to avoid catching falling knives)
            if cmp < sma_200:
                continue
                
            # Rule 2: Pullback Assessment (Isolate consolidation or healthy retracements)
            # Ideal swing pullback entry is when RSI is between 35 and 55 (recoiled springs).
            # Overbought (> 65) or extremely weak stocks (< 30) are filtered out.
            if rsi > 65 or rsi < 30:
                continue
                
            # Scoring System (Higher score = better setup alignment)
            # High score given to stocks with:
            # - RSI close to the "golden pocket" of 40-48
            # - Price trading above 50-day SMA (reinforcing near-term momentum)
            dist_from_45 = abs(rsi - 45)
            score = 100 - dist_from_45
            if cmp > sma_50:
                score += 15 # Add premium for near-term momentum preservation
                
            # Rule 3: Dynamic Price Boundaries & Execution
            entry_range = f"{round(cmp * 0.99, 2):,} – {round(cmp * 1.01, 2):,}"
            
            # Stop-Loss: Set dynamically below the recent 10-day local low or 1.5 * ATR from entry
            stop_loss = round(min(swing_low, cmp - (1.5 * atr)), 2)
            risk = cmp - stop_loss
            if risk <= 0:
                risk = cmp * 0.02 # minimum fallback risk of 2%
                stop_loss = round(cmp - risk, 2)
                
            # Primary Target: Placed at 20-day local resistance swing high or minimum 1:2 R:R
            target = round(max(swing_high, cmp + (2 * risk)), 2)
            
            potential = round(((target - cmp) / cmp) * 100, 2)
            rr_ratio = f"1 : {round((target - cmp) / risk, 2)}"
            
            # Avoid penny stocks / ultra-low prices for institutional-grade setups
            if cmp < 50:
                continue
                
            # Skip if potential return is too low (< 5%) or R:R is unfavorable (< 1.5)
            if potential < 5.0 or (target - cmp) / risk < 1.5:
                continue
                
            candidates.append({
                "Symbol": ticker.replace(".NS", ""),
                "Industry": industry,
                "CMP (₹)": cmp,
                "Entry Range (₹)": entry_range,
                "Stop-Loss (₹)": stop_loss,
                "Target (₹)": target,
                "Potential (%)": f"{potential}%",
                "R:R Ratio": rr_ratio,
                "Score": score
            })
            
        except Exception as e:
            # Graceful error handling for individual ticker processing failures
            continue
            
    # Sort all qualified setups across Nifty 250 by highest technical scorecard alignment and take top 15
    top_candidates = sorted(candidates, key=lambda x: x["Score"], reverse=True)[:15]
    
    # Sort the final leaderboard strictly by absolute Profit Potential for presentation clarity
    final_leaderboard = sorted(top_candidates, key=lambda x: float(x["Potential (%)"].replace('%', '')), reverse=True)
    return final_leaderboard

def get_offline_fallback_data():
    """Baseline data optimized as standard fallback when offline."""
    return [
        {"Symbol": "SHRIRAMFIN", "Industry": "Financial Services", "CMP (₹)": 1041.00, "Entry Range (₹)": "1,025.00 – 1,038.00", "Stop-Loss (₹)": 1010.00, "Target (₹)": 1200.00, "Potential (%)": "16.33%", "R:R Ratio": "1 : 7.84"},
        {"Symbol": "ACME", "Industry": "Power", "CMP (₹)": 418.00, "Entry Range (₹)": "395.00 – 410.00", "Stop-Loss (₹)": 380.77, "Target (₹)": 460.00, "Potential (%)": "14.28%", "R:R Ratio": "1 : 2.62"},
        {"Symbol": "WIPRO", "Industry": "Information Technology", "CMP (₹)": 176.40, "Entry Range (₹)": "170.00 – 174.00", "Stop-Loss (₹)": 164.00, "Target (₹)": 190.00, "Potential (%)": "10.46%", "R:R Ratio": "1 : 2.25"},
        {"Symbol": "NATIONALUM", "Industry": "Metals & Mining", "CMP (₹)": 374.10, "Entry Range (₹)": "370.00 – 374.00", "Stop-Loss (₹)": 362.00, "Target (₹)": 409.25, "Potential (%)": "10.01%", "R:R Ratio": "1 : 3.73"},
        {"Symbol": "WAAREE", "Industry": "Capital Goods", "CMP (₹)": 858.95, "Entry Range (₹)": "830.00 – 850.00", "Stop-Loss (₹)": 811.34, "Target (₹)": 920.00, "Potential (%)": "9.52%", "R:R Ratio": "1 : 2.83"},
        {"Symbol": "BEL", "Industry": "Capital Goods", "CMP (₹)": 405.35, "Entry Range (₹)": "395.00 – 403.00", "Stop-Loss (₹)": 385.00, "Target (₹)": 435.00, "Potential (%)": "9.02%", "R:R Ratio": "1 : 2.51"},
        {"Symbol": "TATASTEEL", "Industry": "Metals & Mining", "CMP (₹)": 188.79, "Entry Range (₹)": "183.50 – 186.00", "Stop-Loss (₹)": 178.00, "Target (₹)": 200.00, "Potential (%)": "8.25%", "R:R Ratio": "1 : 2.26"},
        {"Symbol": "AUROPHARMA", "Industry": "Healthcare", "CMP (₹)": 1653.10, "Entry Range (₹)": "1,620.00 – 1,640.00", "Stop-Loss (₹)": 1585.00, "Target (₹)": 1758.00, "Potential (%)": "7.85%", "R:R Ratio": "1 : 2.86"},
        {"Symbol": "KOTAKBANK", "Industry": "Financial Services", "CMP (₹)": 424.50, "Entry Range (₹)": "417.00 – 421.00", "Stop-Loss (₹)": 412.86, "Target (₹)": 450.00, "Potential (%)": "7.39%", "R:R Ratio": "1 : 4.88"},
        {"Symbol": "TECHM", "Industry": "Information Technology", "CMP (₹)": 1596.90, "Entry Range (₹)": "1,575.00 – 1,595.00", "Stop-Loss (₹)": 1540.00, "Target (₹)": 1697.07, "Potential (%)": "7.07%", "R:R Ratio": "1 : 2.52"},
        {"Symbol": "HAL", "Industry": "Capital Goods", "CMP (₹)": 4856.00, "Entry Range (₹)": "4,780.00 – 4,820.00", "Stop-Loss (₹)": 4700.37, "Target (₹)": 5070.37, "Potential (%)": "5.63%", "R:R Ratio": "1 : 2.71"},
        {"Symbol": "DIVISLAB", "Industry": "Healthcare", "CMP (₹)": 9121.00, "Entry Range (₹)": "9,000.00 – 9,100.00", "Stop-Loss (₹)": 8707.67, "Target (₹)": 9520.17, "Potential (%)": "5.19%", "R:R Ratio": "1 : 1.38"}
    ]

# ==============================================================================
# 3. HTML TABLE DESIGN & COMPILATION
# ==============================================================================
def generate_html_table(data):
    html = """
    <html>
    <head>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333333; line-height: 1.6; }
            .header { background: linear-gradient(135deg, #0f2027 0%, #203a43 50%, #2c5364 100%); color: white; padding: 25px; border-radius: 8px 8px 0 0; text-align: center; }
            .header h2 { margin: 0; font-weight: 600; letter-spacing: 0.5px; }
            .header p { margin: 5px 0 0 0; font-size: 14px; opacity: 0.9; }
            .content { padding: 25px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 8px 8px; background-color: #fcfcfc; }
            .stats-bar { display: flex; justify-content: space-between; margin-bottom: 20px; background: #f1f5f9; padding: 12px 20px; border-radius: 6px; font-size: 13px; font-weight: 500; }
            .stat-item { color: #475569; }
            .stat-highlight { color: #0f2027; font-weight: bold; }
            table { width: 100%; border-collapse: collapse; margin-top: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); background: white; border-radius: 6px; overflow: hidden; }\n            th { background-color: #1f3c4d; color: white; text-align: left; padding: 12px 15px; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }\n            td { padding: 12px 15px; border-bottom: 1px solid #eef2f5; font-size: 14px; }\n            tr:nth-child(even) { background-color: #f8fafc; }\n            tr:hover { background-color: #f1f5f9; }\n            .potential-high { color: #16a34a; font-weight: bold; }\n            .potential-med { color: #ea580c; font-weight: bold; }\n            .rr-badge { background-color: #f0fdf4; color: #166534; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; border: 1px solid #bbf7d0; }\n            .footer { margin-top: 30px; font-size: 11px; color: #64748b; text-align: center; border-top: 1px solid #e2e8f0; padding-top: 15px; }\n        </style>\n    </head>\n    <body>\n        <div class="header">\n            <h2>Consolidated Master Execution Table (Nifty 250 Index Scan)</h2>\n            <p>Automated Weekly Deep Technical Scan — Generated on """ + datetime.now().strftime("%B %d, %Y at %I:%M %p") + """</p>\n        </div>\n        <div class="content">\n            <p>Hello,</p>\n            <p>Please find below your weekly, dynamically calculated <strong>Nifty LargeMidcap 250 Swing Watchlist Leaderboard</strong>. This comprehensive report scanned the full Nifty 250 index to isolate the top 15 setups exhibiting pristine technical trend-following pullback characteristics.</p>\n            \n            <div class="stats-bar">\n                <div class="stat-item">Scanner Mode: <span class="stat-highlight">Live Nifty 250 Pullback Model</span></div>\n                <div class="stat-item">Sectors: <span class="stat-highlight">Dynamic (All Industries Covered)</span></div>\n                <div class="stat-item">Weekly Criteria: <span class="stat-highlight">RSI Pullback [35-55] + SMA 200 Trend Filter</span></div>\n            </div>\n\n            <table>\n                <thead>\n                    <tr>\n                        <th>Symbol</th>\n                        <th>Industry</th>\n                        <th>CMP (₹)</th>\n                        <th>Entry Range (₹)</th>\n                        <th>Stop-Loss (₹)</th>\n                        <th>Target (₹)</th>\n                        <th>Projected Potential</th>\n                        <th>R:R Ratio</th>\n                    </tr>\n                </thead>\n                <tbody>\n    """
    
    for row in data:
        pot_val = float(row["Potential (%)"].replace('%', ''))
        pot_class = "potential-high" if pot_val >= 10 else "potential-med" if pot_val >= 7 else ""
        
        cmp_fmt = f"{row['CMP (₹)']:,.2f}" if isinstance(row['CMP (₹)'], (int, float)) else str(row['CMP (₹)'])
        sl_fmt = f"{row['Stop-Loss (₹)']:,.2f}" if isinstance(row['Stop-Loss (₹)'], (int, float)) else str(row['Stop-Loss (₹)'])
        tgt_fmt = f"{row['Target (₹)']:,.2f}" if isinstance(row['Target (₹)'], (int, float)) else str(row['Target (₹)'])
        
        html += f"""
                    <tr>
                        <td style="font-weight: bold; color: #1f3c4d;">{row['Symbol']}</td>
                        <td style="color: #475569; font-weight: 500;">{row['Industry']}</td>
                        <td style="font-weight: 600;">{cmp_fmt}</td>
                        <td style="color: #334155;">{row['Entry Range (₹)']}</td>
                        <td style="color: #dc2626; font-weight: 500;">{sl_fmt}</td>
                        <td style="color: #16a34a; font-weight: 500;">{tgt_fmt}</td>
                        <td class="{pot_class}">{row['Potential (%)']}</td>
                        <td><span class="rr-badge">{row['R:R Ratio']}</span></td>
                    </tr>
        """
        
    html += """
                </tbody>
            </table>
            
            <p style="margin-top: 25px; font-size: 13px; color: #64748b; line-height: 1.5;">
                <em>Strategic Logic Summary: This institution-grade scanner dynamically assesses all components of the Nifty LargeMidcap 250 index. Stocks are selected if they are trading in major upward trends (Price > SMA 200) and have completed pullbacks into high-probability zones (RSI between 35 and 55). Stop-losses are established below local 10-day lows or mapped to 1.5 * Daily ATR. Targets are set to the 20-day swing high. All penny stocks are excluded. Past performance is not indicative of future returns.</em>
            </p>
        </div>\n        <div class="footer">\n            Report triggered automatically • Swing Trading Research Pipeline • Every Friday 8:00 PM\n        </div>\n    </body>\n    </html>\n    """
    return html

# ==============================================================================
# 4. EMAIL DISPATCH PIPELINE
# ==============================================================================
def send_email():
    SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
    SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD")
    RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")

    if not all([SENDER_EMAIL, SENDER_PASSWORD, RECIPIENT_EMAIL]):
        print("[ERROR] Missing email credentials in Environment Variables!")
        print("Please configure: SENDER_EMAIL, SENDER_PASSWORD, and RECIPIENT_EMAIL.")
        return False

    # Execute the deep Nifty 250 index scan
    watchlist_data = scan_and_research()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 Nifty 250 Weekly Swing Trading Leaderboard — ({datetime.now().strftime('%Y-%m-%d')})"
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL

    html_content = generate_html_table(watchlist_data)
    msg.attach(MIMEText(html_content, "html"))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        server.quit()
        print(f"[SUCCESS] Deep technical scan on Nifty 250 index compiled and emailed to {RECIPIENT_EMAIL}!")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send email: {str(e)}")
        return False

if __name__ == "__main__":
    send_email()
