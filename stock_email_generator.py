import os
from jinja2 import Template

def generate_stock_email_html(stock_data_list):
    """
    Generates a responsive HTML email string from a list of stock dictionaries.
    Handles mixed data types (str, float, int) safely and defaults missing keys to '-'.
    """
    
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 20px; font-family: Arial, sans-serif; background-color: #f4f7fa; color: #333333;">
        
        <!-- Main Email Container -->
        <div style="max-width: 700px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
            
            <!-- Header -->
            <div style="background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); padding: 20px; text-align: center; color: white;">
                <h1 style="margin: 0; font-size: 24px; letter-spacing: 1px;">DAILY MARKET REPORT</h1>
                <p style="margin: 5px 0 0 0; font-size: 14px; opacity: 0.9;">Analyzing {{ stocks|length }} Potential Setups</p>
            </div>

            <div style="padding: 20px;">
                
                <!-- EXECUTIVE SUMMARY TABLE -->
                <h2 style="font-size: 16px; color: #1e3c72; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; margin-top: 0;">Executive Summary</h2>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 12px; text-align: center;">
                        <thead>
                            <tr style="background-color: #f8fafc; border-bottom: 2px solid #cbd5e1; color: #475569;">
                                <th style="padding: 10px 5px;">Symbol</th>
                                <th style="padding: 10px 5px;">Sector</th>
                                <th style="padding: 10px 5px;">CMP</th>
                                <th style="padding: 10px 5px;">Entry</th>
                                <th style="padding: 10px 5px;">Target</th>
                                <th style="padding: 10px 5px;">SL</th>
                                <th style="padding: 10px 5px;">Potential</th>
                                <th style="padding: 10px 5px;">Score</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for data in stocks %}
                            <tr style="border-bottom: 1px solid #e2e8f0; background-color: {{ loop.cycle('#ffffff', '#f8fafc') }};">
                                <td style="padding: 10px 5px; font-weight: bold; color: #1e3c72;">{{ data.get('Symbol', '-') }}</td>
                                <td style="padding: 10px 5px; color: #64748b;">{{ data.get('Sector', '-') }}</td>
                                <td style="padding: 10px 5px;">₹{{ data.get('CMP (₹)', '-') }}</td>
                                <td style="padding: 10px 5px;">₹{{ data.get('Entry Range (₹)', '-') }}</td>
                                <td style="padding: 10px 5px;">₹{{ data.get('Target (₹)', '-') }}</td>
                                <td style="padding: 10px 5px; color: #dc2626;">₹{{ data.get('Stop-Loss (₹)', '-') }}</td>
                                
                                {% set pot = data.get('Potential_Val', 0) | float(default=0) %}
                                <td style="padding: 10px 5px; font-weight: bold; color: {% if pot > 0 %}#16a34a{% else %}#dc2626{% endif %};">
                                    {{ data.get('Potential (%)', '-') }}
                                </td>
                                
                                <td style="padding: 10px 5px; font-weight: bold;">
                                    <span style="background-color: #1e3c72; color: white; padding: 3px 8px; border-radius: 12px; font-size: 11px;">
                                        {{ data.get('Opt_Score', '-') }}
                                    </span>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>

                <!-- INDIVIDUAL STOCK CARDS -->
                {% for data in stocks %}
                <div style="border: 1px solid #e2e8f0; border-radius: 6px; margin-bottom: 25px; overflow: hidden;">
                    
                    <!-- Card Header -->
                    <div style="background-color: #f1f5f9; padding: 12px 15px; border-bottom: 1px solid #e2e8f0; display: table; width: 100%; box-sizing: border-box;">
                        <div style="display: table-cell; vertical-align: middle;">
                            <h3 style="margin: 0; font-size: 18px; color: #0f172a;">{{ data.get('Symbol', '-') }} <span style="font-size: 14px; color: #64748b; font-weight: normal;">| {{ data.get('Sector', '-') }}</span></h3>
                        </div>
                        <div style="display: table-cell; vertical-align: middle; text-align: right;">
                            <span style="background-color: #3b82f6; color: white; padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-left: 5px;">Opt: {{ data.get('Opt_Score', '-') }}</span>
                            <span style="background-color: #64748b; color: white; padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-left: 5px;">Raw: {{ data.get('Raw_Score', '-') }}</span>
                        </div>
                    </div>

                    <!-- Trade Setup -->
                    <div style="padding: 15px; background-color: #ffffff; border-bottom: 1px solid #e2e8f0;">
                        <table style="width: 100%; font-size: 13px; text-align: center;">
                            <tr>
                                <td><strong>CMP:</strong><br>₹{{ data.get('CMP (₹)', '-') }}</td>
                                <td><strong>Entry:</strong><br>₹{{ data.get('Entry Range (₹)', '-') }}</td>
                                <td><strong>Target:</strong><br><span style="color: #16a34a; font-weight: bold;">₹{{ data.get('Target (₹)', '-') }}</span></td>
                                <td><strong>Stop-Loss:</strong><br><span style="color: #dc2626; font-weight: bold;">₹{{ data.get('Stop-Loss (₹)', '-') }}</span></td>
                                <td><strong>Potential:</strong><br><span style="color: #16a34a; font-weight: bold;">{{ data.get('Potential (%)', '-') }}</span></td>
                                <td><strong>R:R:</strong><br>{{ data.get('R:R Ratio', '-') }}</td>
                                <td><strong>Hold:</strong><br>{{ data.get('Est_Hold_Days', '-') }}</td>
                            </tr>
                        </table>
                    </div>

                    <!-- Fundamentals -->
                    <div style="padding: 15px;">
                        <h4 style="margin: 0 0 10px 0; font-size: 14px; color: #1e3c72; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">🏢 Fundamental Snapshot</h4>
                        
                        <div style="background-color: #f8fafc; padding: 10px; border-radius: 4px; margin-bottom: 10px; font-size: 13px; text-align: center; border: 1px solid #e2e8f0;">
                            <strong>Fund Score:</strong> {{ data.get('Fund_Score', '-') }} &nbsp;|&nbsp; 
                            <strong>Piotroski Score:</strong> {{ data.get('Piotroski_Score', '-') }}
                        </div>

                        <table style="width: 100%; font-size: 13px; border-collapse: collapse;">
                            <tr style="border-bottom: 1px solid #f1f5f9;">
                                <td style="padding: 8px 5px; font-weight: bold; color: #475569; width: 25%;">3Y Profit Gr.</td>
                                <td style="padding: 8px 5px; color: #16a34a; font-weight: bold; width: 25%;">{{ data.get('3Y_Profit_Growth', '-') }}</td>
                                <td style="padding: 8px 5px; font-weight: bold; color: #475569; width: 25%;">3Y Sales Gr.</td>
                                <td style="padding: 8px 5px; color: #16a34a; font-weight: bold; width: 25%;">{{ data.get('3Y_Sales_Growth', '-') }}</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #f1f5f9;">
                                <td style="padding: 8px 5px; font-weight: bold; color: #475569;">3Y ROE Avg</td>
                                <td style="padding: 8px 5px;">{{ data.get('3Y_ROE_Avg', '-') }}</td>
                                <td style="padding: 8px 5px; font-weight: bold; color: #475569;">3Y Stock CAGR</td>
                                <td style="padding: 8px 5px;">{{ data.get('3Y_Stock_CAGR', '-') }}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px 5px; font-weight: bold; color: #475569;">P/E Ratio</td>
                                <td style="padding: 8px 5px;" colspan="3">{{ data.get('PE', '-') }}</td>
                            </tr>
                        </table>
                    </div>

                    <!-- Technical Indicators -->
                    <div style="padding: 0 15px 15px 15px;">
                        <h4 style="margin: 0 0 10px 0; font-size: 14px; color: #1e3c72; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">📊 Technical Indicators</h4>
                        <table style="width: 100%; font-size: 12px; border-collapse: collapse; text-align: left;">
                            <tr style="background-color: #f8fafc;">
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; width: 25%;">RSI</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; width: 25%;">{{ data.get('RSI', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; width: 25%;">Stoch %K</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; width: 25%;">{{ data.get('Stoch_K', '-') }}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">MACD</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('MACD', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">Ultimate Osc</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('Ultimate_Osc', '-') }}</td>
                            </tr>
                            <tr style="background-color: #f8fafc;">
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">Aroon Osc</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;">{{ data.get('Aroon_Osc', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">BOP</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;">{{ data.get('BOP', '-') }}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">CCI</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('CCI', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">Williams %R</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('Williams_R', '-') }}</td>
                            </tr>
                            <tr style="background-color: #f8fafc;">
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">Momentum</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;" colspan="3">{{ data.get('Momentum', '-') }}</td>
                            </tr>
                        </table>
                    </div>

                    <!-- News & Sentiment -->
                    <div style="padding: 0 15px 15px 15px;">
                        <h4 style="margin: 0 0 10px 0; font-size: 14px; color: #1e3c72; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">📰 Latest News & Sentiment</h4>
                        <div style="background-color: #f8fafc; border-left: 4px solid #3b82f6; padding: 12px; font-size: 13px;">
                            <div style="margin-bottom: 5px;">
                                <strong>Sentiment:</strong> 
                                <span style="color: #ea580c; font-weight: bold;">{{ data.get('News_Label', '-') }}</span>
                            </div>
                            <i style="color: #475569;">"{{ data.get('News_Headline', '-') }}"</i>
                        </div>
                    </div>

                </div>
                {% endfor %}

                <div style="text-align: center; margin-top: 30px; font-size: 11px; color: #94a3b8;">
                    <p>Generated by Automated Analysis Engine</p>
                    <p>Disclaimer: This data is for informational purposes only and does not constitute financial advice.</p>
                </div>

            </div>
        </div>
    </body>
    </html>
    """

    template = Template(html_template)
    return template.render(stocks=stock_data_list)

# ==========================================
# TEST DATA & EXECUTION (Handles mixed types)
# ==========================================
if __name__ == "__main__":
    
    # Sample list showcasing full data vs missing technical data 
    # to demonstrate that the template won't crash and will fallback to '-'
    sample_data_list = [
        {
            "Symbol": "ACMESOLAR",
            "Sector": "Renewables",
            "CMP (₹)": 1580,
            "Entry Range (₹)": "1550-1570",
            "Stop-Loss (₹)": 1490.5,
            "Target (₹)": 1800,
            "Potential (%)": "14.5%",
            "Potential_Val": 14.5,
            "R:R Ratio": "1:2.1",
            "RR_Val": 2.1,
            "Est_Hold_Days": "30 Days",
            "Buy_Signals": "4/6",
            "Fund_Score": "55/100 Pts",
            "Piotroski_Score": "9/9 (VERY STRONG)",
            "3Y_Profit_Growth": "149.0%",
            "3Y_Sales_Growth": "16.0%",
            "3Y_ROE_Avg": "8.0%",
            "3Y_Stock_CAGR": "0.0%",
            "PE": 47.8,
            "News_Label": "NEUTRAL (+0.15)",
            "News_Headline": "ACMESOLAR maintains steady operational performance with positive sector outlook",
            "Raw_Score": "72",
            "Opt_Score": 85,
            "RSI": "65.2 (BUY)",
            "MACD": 1.25,
            "Stoch_K": "N/A",
            "Ultimate_Osc": 55.4,
            "Aroon_Osc": "70 (STRONG)",
            "BOP": 0.45,
            "CCI": "Wait",
            "Williams_R": -45.2,
            "Momentum": "Bullish"
        },
        {
            "Symbol": "RELIANCE",
            "Sector": "Energy",
            "CMP (₹)": 2950,
            "Entry Range (₹)": 2900,
            "Stop-Loss (₹)": 2850,
            "Target (₹)": 3200,
            "Potential (%)": "8.4%",
            "Potential_Val": "8.4", 
            "R:R Ratio": 2.5, 
            "RR_Val": "2.5", 
            "Est_Hold_Days": 15,
            "Buy_Signals": 5,
            "Fund_Score": "82/100 Pts",
            "Piotroski_Score": "7/9",
            "3Y_Profit_Growth": "12.5%",
            "3Y_Sales_Growth": "18.2%",
            "3Y_ROE_Avg": "14.5%",
            "3Y_Stock_CAGR": "22.1%",
            "PE": 28.4,
            "News_Label": "BULLISH (+0.75)",
            "News_Headline": "Reliance signs major telecom agreement.",
            "Raw_Score": 88,
            "Opt_Score": 92
            # Missing technical indicators intentionally to test the default '-' fallback
        }
    ]

    html_output = generate_stock_email_html(sample_data_list)

    with open("stock_email_preview.html", "w", encoding="utf-8") as f:
        f.write(html_output)

    print("HTML email generated successfully. Open 'stock_email_preview.html' in your browser.")