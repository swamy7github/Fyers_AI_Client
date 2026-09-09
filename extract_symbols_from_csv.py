import pandas as pd
import io

def extract_symbols_from_csv(csv_content_or_filepath):
    """
    Reads a CSV file or string and extracts the 'symbol' column as a list.
    Handles the case where the data might be tab-separated.
    """
    try:
        # Check if the input is a string containing the raw data (like the prompt)
        if isinstance(csv_content_or_filepath, str) and "symbol" in csv_content_or_filepath:
            # We use io.StringIO to treat the string as a file-like object for pandas
            # Try comma first, then fallback to tab if it looks like the prompt's raw data
            if "symbol,signal" in csv_content_or_filepath:
                df = pd.read_csv(io.StringIO(csv_content_or_filepath), sep=',')
            else:
                 df = pd.read_csv(io.StringIO(csv_content_or_filepath), sep='\t')
        else:
            # Assume it's a file path. Use comma separator based on the user's provided columns.
            try:
                # The user's output suggests the columns are comma-separated
                df = pd.read_csv(csv_content_or_filepath, sep=',')
            except Exception:
                 # Fallback to tab separator if comma fails
                 df = pd.read_csv(csv_content_or_filepath, sep='\t')

        # Ensure the 'symbol' column exists (case-insensitive check is good practice)
        # We strip whitespace just in case there are spaces in column names
        df.columns = df.columns.str.strip().str.lower()
        
        if 'symbol' in df.columns:
            # Extract the column and convert it to a standard Python list
            symbols_list = df['symbol'].tolist()
            return symbols_list
        else:
            print("Error: Column 'symbol' not found in the provided data.")
            return []

    except FileNotFoundError:
        print(f"Error: File not found at path '{csv_content_or_filepath}'")
        return []
    except Exception as e:
        print(f"An error occurred while parsing the CSV: {e}")
        return []
# Example usage using the raw data provided in the prompt:
raw_data = """symbol	signal	interval	date	strategy_return_%	win_rate_%	number_of_trades	best_profit	settings	close	high
VADILALIND	sell	4h	2025-04-28 09:15:00+05:30	20.16	66.67	14	{'strategy_return_%': np.float64(20.16), 'buy_and_hold_return_%': np.float64(90.09), 'number_of_trades': 14, 'max_drawdown_%': np.float64(-25.28), 'sharpe_ratio': np.float64(24.57), 'win_rate_%': np.float64(66.67)}	{'tenkan_period': 5, 'kijun_period': 10, 'senkou_span_b_period': 30, 'displacement': 26}	6795.7	6881.7
RBZJEWEL	sell	4h	2025-04-25 13:15:00+05:30	15.49	40	9	{'strategy_return_%': np.float64(15.49), 'buy_and_hold_return_%': np.float64(-27.32), 'number_of_trades': 9, 'max_drawdown_%': np.float64(-21.96), 'sharpe_ratio': np.float64(24.13), 'win_rate_%': np.float64(40.0)}	{'tenkan_period': 9, 'kijun_period': 15, 'senkou_span_b_period': 30, 'displacement': 26}	137.36	137.36
AUSOMENT	sell	4h	2025-04-30 09:15:00+05:30	1.54	42.86	27	{'strategy_return_%': np.float64(1.54), 'buy_and_hold_return_%': np.float64(-14.04), 'number_of_trades': 27, 'max_drawdown_%': np.float64(-14.78), 'sharpe_ratio': np.float64(4.6), 'win_rate_%': np.float64(42.86)}	{'tenkan_period': 9, 'kijun_period': 10, 'senkou_span_b_period': 30, 'displacement': 26}	84.24	88.74
ONGC	sell	4h	2025-04-30 13:15:00+05:30	4.86	54.55	10	{'strategy_return_%': np.float64(4.86), 'buy_and_hold_return_%': np.float64(-5.74), 'number_of_trades': 10, 'max_drawdown_%': np.float64(-11.28), 'sharpe_ratio': np.float64(16.47), 'win_rate_%': np.float64(54.55)}	{'tenkan_period': 9, 'kijun_period': 15, 'senkou_span_b_period': 30, 'displacement': 26}	243.42	245.33
OIL	sell	4h	2025-04-30 13:15:00+05:30	20.17	60	9	{'strategy_return_%': np.float64(20.17), 'buy_and_hold_return_%': np.float64(-3.5), 'number_of_trades': 9, 'max_drawdown_%': np.float64(-3.01), 'sharpe_ratio': np.float64(75.26), 'win_rate_%': np.float64(60.0)}	{'tenkan_period': 9, 'kijun_period': 15, 'senkou_span_b_period': 30, 'displacement': 26}	409.4	413.4"""

print("Extracting symbols from raw string data:")
extracted_symbols = extract_symbols_from_csv('summary_watchlist_signals_2025-05-02_04.csv')
print(extracted_symbols)

# Example usage if you have a file named 'data.csv'
# Replace 'data.csv' with the actual path to your file
# print("\nExtracting symbols from 'data.csv':")
# file_extracted_symbols = extract_symbols_from_csv('data.csv')
# print(file_extracted_symbols)