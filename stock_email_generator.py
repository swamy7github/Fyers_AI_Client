import os
import numpy as np
from jinja2 import Template

def generate_stock_email_html(stock_data_list):
    """
    Generates a responsive HTML email string from a list of stock dictionaries.
    Handles mixed data types (str, float, int, np.float64) safely and defaults missing keys to '-'.
    Accurately maps all technical, fundamental, and trade setups from the provided candidate dictionary.
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
                                <th style="padding: 10px 5px;">Type</th>
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
                                <td style="padding: 10px 5px; font-weight: bold; color: {% if data.get('Type') == 'LONG' %}#16a34a{% elif data.get('Type') == 'SHORT' %}#dc2626{% else %}#64748b{% endif %};">{{ data.get('Type', 'LONG') }}</td>
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
                            <h3 style="margin: 0; font-size: 18px; color: #0f172a;">
                                {{ data.get('Symbol', '-') }} 
                                <span style="font-size: 14px; color: #64748b; font-weight: normal;">| {{ data.get('Sector', '-') }} | <strong style="color: {% if data.get('Type') == 'LONG' %}#16a34a{% else %}#dc2626{% endif %};">{{ data.get('Type', 'LONG') }}</strong></span>
                            </h3>
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
                                <td><strong>Signals:</strong><br><span style="color: #3b82f6; font-weight: bold;">{{ data.get('Buy_Signals', '-') }}</span></td>
                            </tr>
                        </table>
                    </div>

                    <!-- Fundamentals -->
                    {% if data.get('Fund_Score') %}
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
                    {% endif %}

                    <!-- Technical Indicators -->
                    <div style="padding: 0 15px 15px 15px;">
                        <h4 style="margin: 0 0 10px 0; font-size: 14px; color: #1e3c72; border-bottom: 1px solid #e2e8f0; padding-bottom: 5px;">📊 Technical Indicators</h4>
                        <table style="width: 100%; font-size: 12px; border-collapse: collapse; text-align: left;">
                            <tr style="background-color: #f8fafc;">
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; width: 25%;">EMA 50</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; width: 25%;">{{ data.get('EMA_50', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; width: 25%;">EMA 20</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; width: 25%;">{{ data.get('EMA_20', '-') }}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">Vol SMA 20</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('Vol_SMA_20', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">ATR</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('ATR', '-') }}</td>
                            </tr>
                            <tr style="background-color: #f8fafc;">
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">Swing Low (10)</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;">{{ data.get('Swing_Low_10', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">Swing High (20)</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;">{{ data.get('Swing_High_20', '-') }}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">RSI</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('RSI', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">MACD</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('MACD', '-') }}</td>
                            </tr>
                            <tr style="background-color: #f8fafc;">
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">Stoch %K</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;">{{ data.get('Stoch_K', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">Ultimate Osc</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;">{{ data.get('Ultimate_Osc', '-') }}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">Aroon Osc</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('Aroon_Osc', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">BOP</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0;">{{ data.get('BOP', '-') }}</td>
                            </tr>
                            <tr style="background-color: #f8fafc;">
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">CCI</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;">{{ data.get('CCI', '-') }}</td>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0;">Williams %R</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;">{{ data.get('Williams_R', '-') }}</td>
                            </tr>
                            <tr>
                                <td style="padding: 6px; font-weight: bold; border: 1px solid #e2e8f0; background-color: #f8fafc;">Momentum</td>
                                <td style="padding: 6px; border: 1px solid #e2e8f0; background-color: #ffffff;" colspan="3">{{ data.get('Momentum', '-') }}</td>
                            </tr>
                        </table>
                    </div>

                    <!-- News & Sentiment -->
                    {% if data.get('News_Label') %}
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
                    {% endif %}

                </div>
                {% endfor %}

                <!-- Footer -->
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
# TEST DATA & EXECUTION
# ==========================================
if __name__ == "__main__":
    
    # Matching the exact formatting generated by your Python dictionary output
    sample_data_list = [
        {
            'Type': 'LONG',
            'Symbol': 'AEGISLOG', 
            'Sector': 'Oil Gas & Consumable Fuels', 
            'CMP (₹)': np.float64(1353.6), 
            'Entry Range (₹)': '1,340.06 - 1,367.14', 
            'Stop-Loss (₹)': np.float64(1180.0), 
            'Target (₹)': np.float64(1614.0), 
            'Potential (%)': '19.24%', 
            'Potential_Val': np.float64(19.24), 
            'R:R Ratio': '1 : 1.5', 
            'RR_Val': np.float64(1.5), 
            'Est_Hold_Days': '5 Days', 
            'Buy_Signals': '7/9 BUY', 
            'Fund_Score': '50/100 Pts', 
            'Piotroski_Score': '9/9 (VERY STRONG)', 
            '3Y_Profit_Growth': '24.0%', 
            '3Y_Sales_Growth': '-1.0%', 
            '3Y_ROE_Avg': '16.0%', 
            '3Y_Stock_CAGR': '58.0%', 
            'PE': 38.0, 
            'News_Label': 'BULLISH (+0.50)', 
            'News_Headline': 'Aegis Logistics Share Price - Live NSE: AEGISLOG Stock Price & Chart - Upstox', 
            'Raw_Score': 170, 
            'Opt_Score': np.float64(65.1),
            
            # Exact Technical indicator dictionary values as requested
            'EMA_50': '1290.4',
            'EMA_20': '1110.2',
            'Vol_SMA_20': '1.2M',
            'Swing_Low_10': '1290.0',
            'Swing_High_20': '1410.5',
            'ATR': '25.4',
            'RSI': '62.4',
            'Stoch_K': '75.1 D-(72.3)',
            'MACD': '2.45 (s-1.1, l-0.5)',
            'Ultimate_Osc': '58.2',
            'Aroon_Osc': '100.0',
            'BOP': '0.35',
            'CCI': '125.6',
            'Williams_R': '-15.4',
            'Momentum': '14.5'
        },
        {
            'Type': 'LONG',
            'Symbol': 'BOSCHLTD', 
            'Sector': 'Auto Components', 
            'CMP (₹)': 48385.0, 
            'Entry Range (₹)': '48000 - 48250', 
            'Stop-Loss (₹)': 46500.0, 
            'Target (₹)': 52500.0, 
            'Potential (%)': '8.5%', 
            'Potential_Val': 8.5, 
            'R:R Ratio': '1 : 2.2', 
            'RR_Val': 2.2, 
            'Est_Hold_Days': '15 Days', 
            'Buy_Signals': '6/9', 
            'Fund_Score': '75/100 Pts', 
            'Piotroski_Score': '8/9 (STRONG)', 
            '3Y_Profit_Growth': '14.0%', 
            '3Y_Sales_Growth': '10.0%', 
            '3Y_ROE_Avg': '15.0%', 
            '3Y_Stock_CAGR': '36.0%', 
            'PE': 60.4, 
            'News_Label': 'NEUTRAL (+0.10)', 
            'News_Headline': 'BOSCHLTD maintains consistent growth trajectory in Q2.', 
            'Raw_Score': 140, 
            'Opt_Score': 78.5,
            
            # Expanded Technicals
            'EMA_50': '47200',
            'EMA_20': '41000',
            'Vol_SMA_20': '35.5K',
            'Swing_Low_10': '46800',
            'Swing_High_20': '49500',
            'ATR': '850.5',
            'RSI': '55.2',
            'Stoch_K': '60.5 D-(58.1)',
            'MACD': '450.2 (s-300, l-150.2)',
            'Ultimate_Osc': '52.1',
            'Aroon_Osc': '65',
            'BOP': '0.15',
            'CCI': '95.2',
            'Williams_R': '-45.5',
            'Momentum': '12.0'
        }
    ]

    html_output = generate_stock_email_html(sample_data_list)

    with open("stock_email_preview.html", "w", encoding="utf-8") as f:
        f.write(html_output)

    print("HTML email generated successfully. Open 'stock_email_preview.html' in your browser.")