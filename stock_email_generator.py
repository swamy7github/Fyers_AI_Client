import datetime
from jinja2 import Environment, BaseLoader

# Sample data mimicking the LIST of dictionary structure provided by the user.
sample_data_list = [
    {
        "Symbol": "RELIANCE",
        "Sector": "Oil & Gas",
        "market_cap": "19,50,000 Cr",
        "CMP (₹)": "2850.50",
        "Entry Range (₹)": "2830 - 2860",
        "Stop-Loss (₹)": "2750.00",
        "Target (₹)": "3100.00",
        "Potential (%)": "8.75%",
        "R:R Ratio": "1:2.5",
        "Est_Hold_Days": "14 Days",
        "Buy_Signals": "7/9",
        "RSI": "65.4 (Bullish)",
        "Stoch_K": "72.1 (Overbought)",
        "MACD": "12.5 (Buy)",
        "Ultimate_Osc": "55.2 (Neutral)",
        "Aroon_Osc": "80.0 (Strong Uptrend)",
        "BOP": "0.45 (Bullish)",
        "CCI": "110.5 (Buy)",
        "Williams_R": "-25.4 (Overbought)",
        "Momentum": "45.6 (Bullish)",
        "PE": "25.4",
        "ROE": "14.5%",
        "ROCE": "16.2%",
        "DE": "0.35",
        "PB": "2.8",
        "comp_sales_grt": "12.5%",
        "comp_profit_grt": "18.2%",
        "stock_CAGR": "15.4%",
        "return_on_equity": "14.5%",
        "News_Label": "Positive (+0.85)",
        "News_Headline": "Reliance signs new deal in renewable energy sector, boosting investor confidence.",
        "Score": 85.5
    },
    {
        "Symbol": "TCS",
        "Sector": "IT Services",
        "market_cap": "14,20,000 Cr",
        "CMP (₹)": "3950.00",
        "Entry Range (₹)": "3900 - 3960",
        "Stop-Loss (₹)": "3820.00",
        "Target (₹)": "4200.00",
        "Potential (%)": "6.33%",
        "R:R Ratio": "1:2.0",
        "Est_Hold_Days": "10 Days",
        "Buy_Signals": "6/9",
        "RSI": "58.2 (Neutral)",
        "Stoch_K": "65.0 (Neutral)",
        "MACD": "8.2 (Buy)",
        "Ultimate_Osc": "50.1 (Neutral)",
        "Aroon_Osc": "40.0 (Uptrend)",
        "BOP": "0.20 (Neutral)",
        "CCI": "85.0 (Buy)",
        "Williams_R": "-45.0 (Neutral)",
        "Momentum": "30.0 (Bullish)",
        "PE": "32.1",
        "ROE": "45.2%",
        "ROCE": "52.5%",
        "DE": "0.05",
        "PB": "14.5",
        "comp_sales_grt": "15.0%",
        "comp_profit_grt": "14.8%",
        "stock_CAGR": "12.0%",
        "return_on_equity": "45.2%",
        "News_Label": "Neutral (+0.10)",
        "News_Headline": "TCS announces upcoming quarterly results date.",
        "Score": 72.0
    }
]


# Note: For HTML emails, inline styles are strongly recommended for maximum compatibility.
html_template_string = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily Stock Analysis Report</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f4f7f6; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333333; -webkit-font-smoothing: antialiased;">
    
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f4f7f6; padding: 20px 0;">
        <tr>
            <td align="center">
                
                <!-- Main Container -->
                <table width="600" cellpadding="0" cellspacing="0" border="0" style="background-color: transparent;">
                    
                    <!-- Top Email Header -->
                    <tr>
                        <td style="padding-bottom: 20px; text-align: center;">
                            <h1 style="color: #1e3c72; margin: 0; font-size: 24px;">Daily Quantitative Stock Analysis</h1>
                            <p style="color: #64748b; margin: 5px 0 0 0; font-size: 14px;">{{ date }} • {{ stocks|length }} Stocks Analyzed</p>
                        </td>
                    </tr>

                    <!-- Executive Summary Table -->
                    <tr>
                        <td style="padding-bottom: 30px;">
                            <div style="background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05); border: 1px solid #e0e0e0;">
                                <h3 style="margin: 0; padding: 15px 20px; background-color: #f8fafc; color: #1e3c72; border-bottom: 1px solid #e0e0e0; font-size: 16px;">Executive Summary</h3>
                                <table width="100%" cellpadding="8" cellspacing="0" border="0" style="font-size: 12px; text-align: center; border-collapse: collapse;">
                                    <thead>
                                        <tr style="background-color: #1e3c72; color: #ffffff;">
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Symbol</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Sector</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Mkt Cap</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">CMP</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Entry</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Target</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">SL</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Score</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {% for summary_data in stocks %}
                                        <tr style="background-color: {{ loop.cycle('#ffffff', '#f8fafc') }}; border-bottom: 1px solid #e2e8f0;">
                                            <td style="font-weight: bold; color: #1e3c72; padding: 10px 5px;">{{ summary_data['Symbol'] }}</td>
                                            <td style="padding: 10px 5px;">{{ summary_data['Sector'] }}</td>
                                            <td style="padding: 10px 5px;">{{ summary_data['market_cap'] }}</td>
                                            <td style="padding: 10px 5px;">₹{{ summary_data['CMP (₹)'] }}</td>
                                            <td style="padding: 10px 5px; white-space: nowrap;">₹{{ summary_data['Entry Range (₹)'] }}</td>
                                            <td style="color: #166534; font-weight: bold; padding: 10px 5px;">₹{{ summary_data['Target (₹)'] }}</td>
                                            <td style="color: #991b1b; font-weight: bold; padding: 10px 5px;">₹{{ summary_data['Stop-Loss (₹)'] }}</td>
                                            <td style="font-weight: bold; padding: 10px 5px;">
                                                <span style="background-color: #dbeafe; color: #1e40af; padding: 4px 8px; border-radius: 12px; font-size: 11px;">{{ summary_data['Score'] }}</span>
                                            </td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </td>
                    </tr>

                    {% for data in stocks %}
                    <!-- Individual Stock Card -->
                    <tr>
                        <td style="background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05); border: 1px solid #e0e0e0; margin-bottom: 30px; display: block;">
                            
                            <!-- Stock Header Section -->
                            <table width="100%" cellpadding="0" cellspacing="0" border="0">
                                <tr>
                                    <td style="background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); padding: 30px; text-align: center;">
                                        <h2 style="color: #ffffff; margin: 0; font-size: 28px; letter-spacing: 1px;">{{ data['Symbol'] }}</h2>
                                        <p style="color: #c0d6f1; margin: 5px 0 0 0; font-size: 14px; text-transform: uppercase; letter-spacing: 2px;">{{ data['Sector'] }}</p>
                                        
                                        <table align="center" style="margin-top: 20px;">
                                            <tr>
                                                <td style="background-color: rgba(255,255,255,0.2); border-radius: 5px; padding: 10px 20px;">
                                                    <span style="color: #ffffff; font-size: 14px;">Overall Score</span><br>
                                                    <span style="color: #ffffff; font-size: 32px; font-weight: bold;">{{ data['Score'] }}</span>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>

                                <!-- Content Area -->
                                <tr>
                                    <td style="padding: 30px;">
                                        
                                        <!-- Trade Setup Section -->
                                        <h3 style="color: #1e3c72; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; margin-top: 0; font-size: 18px;">📈 Trade Setup</h3>
                                        <table width="100%" cellpadding="10" cellspacing="0" style="margin-bottom: 25px; border-collapse: collapse;">
                                            <tr>
                                                <td width="25%" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px; text-align: center;">
                                                    <span style="font-size: 12px; color: #64748b; display: block; margin-bottom: 5px;">CMP</span>
                                                    <strong style="font-size: 16px; color: #0f172a;">₹{{ data['CMP (₹)'] }}</strong>
                                                </td>
                                                <td width="25%" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px; text-align: center;">
                                                    <span style="font-size: 12px; color: #64748b; display: block; margin-bottom: 5px;">Entry Range</span>
                                                    <strong style="font-size: 16px; color: #0f172a;">₹{{ data['Entry Range (₹)'] }}</strong>
                                                </td>
                                                <td width="25%" style="background-color: #fef2f2; border: 1px solid #fecaca; border-radius: 4px; text-align: center;">
                                                    <span style="font-size: 12px; color: #ef4444; display: block; margin-bottom: 5px;">Stop-Loss</span>
                                                    <strong style="font-size: 16px; color: #991b1b;">₹{{ data['Stop-Loss (₹)'] }}</strong>
                                                </td>
                                                <td width="25%" style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 4px; text-align: center;">
                                                    <span style="font-size: 12px; color: #22c55e; display: block; margin-bottom: 5px;">Target</span>
                                                    <strong style="font-size: 16px; color: #166534;">₹{{ data['Target (₹)'] }}</strong>
                                                </td>
                                            </tr>
                                        </table>

                                        <table width="100%" cellpadding="8" cellspacing="0" style="margin-bottom: 30px; font-size: 14px;">
                                            <tr>
                                                <td width="50%" style="border-bottom: 1px solid #eee;"><strong>Potential Return:</strong> <span style="color: #22c55e; font-weight: bold;">{{ data['Potential (%)'] }}</span></td>
                                                <td width="50%" style="border-bottom: 1px solid #eee;"><strong>Risk/Reward:</strong> {{ data['R:R Ratio'] }}</td>
                                            </tr>
                                            <tr>
                                                <td style="border-bottom: 1px solid #eee;"><strong>Est. Hold Time:</strong> {{ data['Est_Hold_Days'] }}</td>
                                                <td style="border-bottom: 1px solid #eee;"><strong>Buy Signals:</strong> {{ data['Buy_Signals'] }}</td>
                                            </tr>
                                        </table>

                                        <!-- Technical Indicators -->
                                        <h3 style="color: #1e3c72; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; font-size: 18px;">📊 Technical Indicators</h3>
                                        <table width="100%" cellpadding="8" cellspacing="0" style="margin-bottom: 30px; font-size: 13px; border-collapse: collapse;">
                                            <thead>
                                                <tr style="background-color: #f1f5f9;">
                                                    <th align="left" style="border: 1px solid #e2e8f0; width: 25%;">Indicator</th>
                                                    <th align="left" style="border: 1px solid #e2e8f0; width: 25%;">Value & Signal</th>
                                                    <th align="left" style="border: 1px solid #e2e8f0; width: 25%;">Indicator</th>
                                                    <th align="left" style="border: 1px solid #e2e8f0; width: 25%;">Value & Signal</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                <tr>
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">RSI</td>
                                                    <td style="border: 1px solid #e2e8f0;">{{ data['RSI'] }}</td>
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">MACD</td>
                                                    <td style="border: 1px solid #e2e8f0;">{{ data['MACD'] }}</td>
                                                </tr>
                                                <tr style="background-color: #fafafa;">
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">Stochastic K</td>
                                                    <td style="border: 1px solid #e2e8f0;">{{ data['Stoch_K'] }}</td>
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">Ultimate Osc</td>
                                                    <td style="border: 1px solid #e2e8f0;">{{ data['Ultimate_Osc'] }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">Aroon Osc</td>
                                                    <td style="border: 1px solid #e2e8f0;">{{ data['Aroon_Osc'] }}</td>
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">BOP</td>
                                                    <td style="border: 1px solid #e2e8f0;">{{ data['BOP'] }}</td>
                                                </tr>
                                                <tr style="background-color: #fafafa;">
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">CCI</td>
                                                    <td style="border: 1px solid #e2e8f0;">{{ data['CCI'] }}</td>
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">Williams %R</td>
                                                    <td style="border: 1px solid #e2e8f0;">{{ data['Williams_R'] }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border: 1px solid #e2e8f0; font-weight: bold;">Momentum</td>
                                                    <td style="border: 1px solid #e2e8f0;" colspan="3">{{ data['Momentum'] }}</td>
                                                </tr>
                                            </tbody>
                                        </table>

                                        <!-- Fundamental Data -->
                                        <h3 style="color: #1e3c72; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; font-size: 18px;">🏢 Fundamental Snapshot</h3>
                                        <p style="font-size: 13px; color: #666; margin-top: 0;">Market Cap: <strong>{{ data['market_cap'] }}</strong></p>
                                        <table width="100%" cellpadding="8" cellspacing="0" style="margin-bottom: 30px; font-size: 13px; border-collapse: collapse;">
                                            <tbody>
                                                <tr>
                                                    <td width="25%" style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>P/E Ratio</strong></td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee;">{{ data['PE'] }}</td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>P/B Ratio</strong></td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee;">{{ data['PB'] }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>ROE</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['ROE'] }}</td>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>ROCE</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['ROCE'] }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Debt/Equity</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['DE'] }}</td>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Stock CAGR</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['stock_CAGR'] }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Sales Growth</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['comp_sales_grt'] }}</td>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Profit Growth</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['comp_profit_grt'] }}</td>
                                                </tr>
                                            </tbody>
                                        </table>

                                        <!-- News Sentiment -->
                                        <h3 style="color: #1e3c72; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; font-size: 18px;">📰 Latest News & Sentiment</h3>
                                        <div style="background-color: #f8fafc; border-left: 4px solid #3b82f6; padding: 15px; border-radius: 0 4px 4px 0;">
                                            <p style="margin: 0 0 10px 0; font-size: 14px;"><strong>Sentiment:</strong> 
                                                {% if 'Positive' in data['News_Label'] %}
                                                    <span style="color: #16a34a; font-weight: bold;">{{ data['News_Label'] }}</span>
                                                {% elif 'Negative' in data['News_Label'] %}
                                                    <span style="color: #dc2626; font-weight: bold;">{{ data['News_Label'] }}</span>
                                                {% else %}
                                                    <span style="color: #d97706; font-weight: bold;">{{ data['News_Label'] }}</span>
                                                {% endif %}
                                            </p>
                                            <p style="margin: 0; font-size: 14px; font-style: italic; color: #475569;">"{{ data['News_Headline'] }}"</p>
                                        </div>

                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    {% endfor %}
                    
                    <!-- Global Footer -->
                    <tr>
                        <td style="padding: 20px; text-align: center;">
                            <p style="font-size: 11px; color: #94a3b8; margin: 0;">
                                Generated on {{ date }}<br>
                                Disclaimer: This report is for informational purposes only and does not constitute financial advice. Please do your own research before trading.
                            </p>
                        </td>
                    </tr>

                </table>
                <!-- End Main Container -->

            </td>
        </tr>
    </table>

</body>
</html>
"""


def generate_stock_email_html(stock_data_list: list) -> str:
    """
    Takes a list of dictionaries containing stock analysis data and renders it into a 
    beautiful, email-ready HTML string, looping through each stock.

    Args:
        stock_data_list (list): A list where each element is a dictionary containing 
                                the stock parameters.

    Returns:
        str: The fully rendered HTML string ready to be sent via email.
    """
    
    # 1. Set up the Jinja2 environment
    env = Environment(loader=BaseLoader())
    
    # 2. Load the template from the string
    template = env.from_string(html_template_string)
    
    # 3. Get the current date for the header and footer
    current_date = datetime.datetime.now().strftime("%B %d, %Y")
    
    # 4. Render the template by passing the list of dictionaries and the date
    # Notice we pass `stocks=stock_data_list` which matches `{% for data in stocks %}` in the HTML
    rendered_html = template.render(
        stocks=stock_data_list,
        date=current_date
    )
    
    return rendered_html

if __name__ == "__main__":
    # Generate the HTML using the sample list data
    final_html_output = generate_stock_email_html(sample_data_list)
    
    # Optional: Save it to an HTML file to preview
    output_filename = "stock_email_preview.html"
    try:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(final_html_output)
        print(f"✅ Success! HTML email template generated for {len(sample_data_list)} stocks and saved to '{output_filename}'.")
    except Exception as e:
        print(f"❌ Error writing to file: {e}")