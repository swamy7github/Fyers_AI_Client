import winsound

def play_buy_alert():
    # Frequency (Hz), Duration (ms)
    winsound.Beep(1000, 500) 

def play_sell_alert():
    # Different pitch or pattern for sell
    winsound.Beep(500, 800)

# Example usage in your trading logic:
signal =  "SELL"

if signal == "BUY":
    play_buy_alert()
elif signal == "SELL":
    play_sell_alert()
