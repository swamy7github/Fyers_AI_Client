import datetime
from jinja2 import Environment, BaseLoader

# Sample data mimicking the updated LIST of dictionary structure provided by the user.
sample_data_list = [
    {
        "Symbol": "RELIANCE",
        "Sector": "Oil & Gas",
        "CMP (₹)": "2850.50",
        "Entry Range (₹)": "2830 - 2860",
        "Stop-Loss (₹)": "2750.00",
        "Target (₹)": "3100.00",
        "Potential (%)": "8.75%",
        "Potential_Val": 8.75,
        "R:R Ratio": "1:2.5",
        "RR_Val": 2.5,
        "Est_Hold_Days": "14 Days",
        "Buy_Signals": "7/9",
        "Fund_Score": "85/100 Pts",
        "Piotroski_Score": "8/9 (Strong)",
        "3Y_Profit_Growth": "18.2%",
        "3Y_Sales_Growth": "12.5%",
        "3Y_ROE_Avg": "14.5%",
        "3Y_Stock_CAGR": "15.4%",
        "PE": "25.4",
        "News_Label": "Positive (+0.85)",
        "News_Headline": "Reliance signs new deal in renewable energy sector, boosting investor confidence.",
        "Raw_Score": 85.5,
        "Opt_Score": 92.0,
        "RSI": "65.5 (BUY)",
        "Stoch_K": "82.1 (OVERBOUGHT)",
        "MACD": "12.4 (BUY)",
        "Ultimate_Osc": "55.2 (NEUTRAL)",
        "Aroon_Osc": "100.0 (STRONG BUY)",
        "BOP": "0.45 (BUY)",
        "CCI": "125.4 (BUY)",
        "Williams_R": "-25.6 (BUY)",
        "Momentum": "14.2 (BUY)"
    },
    {
        "Symbol": "TCS",
        "Sector": "IT Services",
        "CMP (₹)": "3950.00",
        "Entry Range (₹)": "3900 - 3960",
        "Stop-Loss (₹)": "3820.00",
        "Target (₹)": "4200.00",
        "Potential (%)": "6.33%",
        "Potential_Val": 6.33,
        "R:R Ratio": "1:2.0",
        "RR_Val": 2.0,
        "Est_Hold_Days": "10 Days",
        "Buy_Signals": "6/9",
        "Fund_Score": "90/100 Pts",
        "Piotroski_Score": "9/9 (Exceptional)",
        "3Y_Profit_Growth": "14.8%",
        "3Y_Sales_Growth": "15.0%",
        "3Y_ROE_Avg": "45.2%",
        "3Y_Stock_CAGR": "12.0%",
        "PE": "32.1",
        "News_Label": "Neutral (+0.10)",
        "News_Headline": "TCS announces upcoming quarterly results date.",
        "Raw_Score": 72.0,
        "Opt_Score": 78.5,
        "RSI": "45.2 (NEUTRAL)",
        "Stoch_K": "32.1 (SELL)",
        "MACD": "-2.4 (SELL)",
        "Ultimate_Osc": "41.2 (SELL)",
        "Aroon_Osc": "-50.0 (SELL)",
        "BOP": "-0.25 (SELL)",
        "CCI": "-115.4 (SELL)",
        "Williams_R": "-85.6 (SELL)",
        "Momentum": "-4.2 (SELL)"
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
                            <p style="color: #64748b; margin: 5px 0 0 0; font-size: 14px;">{{ date }} • {{ stocks|length }} Candidates Identified</p>
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
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">CMP</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Entry</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Target</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Potential</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">SL</th>
                                            <th style="padding: 12px 5px; border-bottom: 2px solid #e0e0e0;">Opt Score</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {% for summary_data in stocks %}
                                        <tr style="background-color: {{ loop.cycle('#ffffff', '#f8fafc') }}; border-bottom: 1px solid #e2e8f0;">
                                            <td style="font-weight: bold; color: #1e3c72; padding: 10px 5px;">{{ summary_data['Symbol'] }}</td>
                                            <td style="padding: 10px 5px; font-size: 11px;">{{ summary_data['Sector'] }}</td>
                                            <td style="padding: 10px 5px;">₹{{ summary_data['CMP (₹)'] }}</td>
                                            <td style="padding: 10px 5px; white-space: nowrap;">₹{{ summary_data['Entry Range (₹)'] }}</td>
                                            <td style="color: #166534; font-weight: bold; padding: 10px 5px;">₹{{ summary_data['Target (₹)'] }}</td>
                                            <td style="color: #166534; font-weight: bold; padding: 10px 5px;">{{ summary_data['Potential (%)'] }}</td>
                                            <td style="color: #991b1b; font-weight: bold; padding: 10px 5px;">₹{{ summary_data['Stop-Loss (₹)'] }}</td>
                                            <td style="font-weight: bold; padding: 10px 5px;">
                                                <span style="background-color: #dbeafe; color: #1e40af; padding: 4px 8px; border-radius: 12px; font-size: 11px;">{{ summary_data['Opt_Score'] }}</span>
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
                                                <td style="background-color: rgba(255,255,255,0.2); border-radius: 5px; padding: 10px 20px; margin-right: 10px; display: inline-block;">
                                                    <span style="color: #ffffff; font-size: 12px; text-transform: uppercase;">Raw Score</span><br>
                                                    <span style="color: #ffffff; font-size: 24px; font-weight: bold;">{{ data['Raw_Score'] }}</span>
                                                </td>
                                                <td style="background-color: rgba(255,255,255,0.3); border-radius: 5px; padding: 10px 20px; display: inline-block;">
                                                    <span style="color: #ffffff; font-size: 12px; text-transform: uppercase;">Opt Score</span><br>
                                                    <span style="color: #ffffff; font-size: 24px; font-weight: bold;">{{ data['Opt_Score'] }}</span>
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

                                        <!-- Fundamental Data -->
                                        <h3 style="color: #1e3c72; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; font-size: 18px;">🏢 Fundamental Snapshot</h3>
                                        
                                        <table width="100%" cellpadding="8" cellspacing="0" style="margin-bottom: 15px; font-size: 13px; border-collapse: collapse;">
                                             <tr>
                                                <td width="50%" style="background-color: #f1f5f9; border: 1px solid #e2e8f0; padding: 10px;">
                                                    <strong>Fund Score:</strong> <span style="color: #1e3c72;">{{ data['Fund_Score'] }}</span>
                                                </td>
                                                <td width="50%" style="background-color: #f1f5f9; border: 1px solid #e2e8f0; padding: 10px;">
                                                    <strong>Piotroski Score:</strong> <span style="color: #1e3c72;">{{ data['Piotroski_Score'] }}</span>
                                                </td>
                                            </tr>
                                        </table>

                                        <table width="100%" cellpadding="8" cellspacing="0" style="margin-bottom: 30px; font-size: 13px; border-collapse: collapse;">
                                            <tbody>
                                                <tr>
                                                    <td width="25%" style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>3Y Profit Gr.</strong></td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee; font-weight: bold; color: #166534;">{{ data['3Y_Profit_Growth'] }}</td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>3Y Sales Gr.</strong></td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee; font-weight: bold; color: #166534;">{{ data['3Y_Sales_Growth'] }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>3Y ROE Avg</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['3Y_ROE_Avg'] }}</td>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>3Y Stock CAGR</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['3Y_Stock_CAGR'] }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>P/E Ratio</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;" colspan="3">{{ data['PE'] }}</td>
                                                </tr>
                                            </tbody>
                                        </table>

                                        <!-- Technical Indicators -->
                                        <h3 style="color: #1e3c72; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; font-size: 18px;">📊 Technical Indicators</h3>
                                        <table width="100%" cellpadding="8" cellspacing="0" style="margin-bottom: 30px; font-size: 13px; border-collapse: collapse;">
                                            <tbody>
                                                <tr>
                                                    <td width="25%" style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>RSI</strong></td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee;">{{ data['RSI']|default('-', true) }}</td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Stoch %K</strong></td>
                                                    <td width="25%" style="border-bottom: 1px solid #eee;">{{ data['Stoch_K']|default('-', true) }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>MACD</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['MACD']|default('-', true) }}</td>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Ultimate Osc</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['Ultimate_Osc']|default('-', true) }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Aroon Osc</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['Aroon_Osc']|default('-', true) }}</td>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>BOP</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['BOP']|default('-', true) }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>CCI</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['CCI']|default('-', true) }}</td>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Williams %R</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;">{{ data['Williams_R']|default('-', true) }}</td>
                                                </tr>
                                                <tr>
                                                    <td style="border-bottom: 1px solid #eee; background-color: #fafafa;"><strong>Momentum</strong></td>
                                                    <td style="border-bottom: 1px solid #eee;" colspan="3">{{ data['Momentum']|default('-', true) }}</td>
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