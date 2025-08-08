import MetaTrader5 as mt5
import pwinput
import pandas as pd
import ta
from datetime import datetime, timezone, timedelta
import time
import sys
import numpy as np
import random
import csv
import os

def print_dynamic(message: str, end='\r'):
    sys.stdout.write(message + end)
    sys.stdout.flush()

print("Script started!")

# Login and connect
try:
    login = int(input("Enter your MT5 Login ID (number): "))
    password = pwinput.pwinput(prompt='Enter your MT5 Password: ', mask='*')
    server = input("Enter your MT5 Server Name: ")
except Exception as e:
    print(f"Error in input: {e}")
    exit(1)

print("Now trying to initialize MT5...")

if not mt5.initialize(login=login, password=password, server=server):
    print("MetaTrader 5 initialization failed, error code:", mt5.last_error())
    exit(1)
else:
    print("Login successful!")

symbol = "XAUUSD"

# Symbol check/activation
symbol_info = mt5.symbol_info(symbol)
if symbol_info is None:
    print(f"{symbol} not found in Market Watch. Attempting to add...")
    if mt5.symbol_select(symbol, True):
        print(f"{symbol} was not present but is now successfully added and activated.")
    else:
        print(f"Failed to add and activate {symbol}.")
elif not symbol_info.visible:
    if mt5.symbol_select(symbol, True):
        print(f"{symbol} was present but hidden. Now activated and visible.")
    else:
        print(f"{symbol} is present but can't be activated/made visible.")
else:
    print(f"{symbol} is already available and visible in Market Watch.")

def add_indicators(df):
    if df is not None and not df.empty:
        df['EMA_10'] = ta.trend.EMAIndicator(close=df['close'], window=10).ema_indicator()
        df['EMA_50'] = ta.trend.EMAIndicator(close=df['close'], window=50).ema_indicator()
        df['RSI_14'] = ta.momentum.RSIIndicator(close=df['close'], window=14).rsi()
    else:
        print("Empty or missing DataFrame. Skipping indicator calculation.")

def get_single_tf_signal(df):
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    if (last['EMA_10'] > last['EMA_50']) and (last['RSI_14'] < 70) and (last['close'] > last['EMA_10']):
        return 'buy'
    if (last['EMA_10'] < last['EMA_50']) and (last['RSI_14'] > 30) and (last['close'] < last['EMA_10']):
        return 'sell'
    return None

def get_multi_tf_signals(df_m1, df_m5, df_m15):
    signals = [
        get_single_tf_signal(df_m1),
        get_single_tf_signal(df_m5),
        get_single_tf_signal(df_m15)
    ]
    clean_signals = [s for s in signals if s is not None]

    buy_count = clean_signals.count('buy')
    sell_count = clean_signals.count('sell')
    if buy_count == 3:
        return 'strong_buy'
    if sell_count == 3:
        return 'strong_sell'
    if buy_count >= 2:
        return 'weak_buy'
    if sell_count >= 2:
        return 'weak_sell'
    return None

def detect_price_action_patterns(df):
    df = df.copy()
    df['bullish_pin'] = (
        (abs(df['close'] - df['open']) <= (df['high'] - df['low']) * 0.3) &
        ((df[['open', 'close']].min(axis=1) - df['low']) >= 2 * abs(df['close'] - df['open'])) &
        ((df['high'] - df[['open', 'close']].max(axis=1)) <= abs(df['close'] - df['open'])) &
        (df['close'] > df['open'])
    )
    df['bearish_pin'] = (
        (abs(df['close'] - df['open']) <= (df['high'] - df['low']) * 0.3) &
        ((df['high'] - df[['open', 'close']].max(axis=1)) >= 2 * abs(df['close'] - df['open'])) &
        ((df[['open', 'close']].min(axis=1) - df['low']) <= abs(df['close'] - df['open'])) &
        (df['close'] < df['open'])
    )
    df['bullish_engulf'] = (
        (df['close'] > df['open']) &
        (df['open'].shift(1) > df['close'].shift(1)) &
        (df['open'] < df['close'].shift(1)) &
        (df['close'] > df['open'].shift(1))
    )
    df['bearish_engulf'] = (
        (df['close'] < df['open']) &
        (df['open'].shift(1) < df['close'].shift(1)) &
        (df['open'] > df['close'].shift(1)) &
        (df['close'] < df['open'].shift(1))
    )
    df['inside_bar'] = (
        (df['high'] < df['high'].shift(1)) &
        (df['low'] > df['low'].shift(1))
    )
    # You can add more patterns here similarly
    return df

def pa_any_bull(row, prev_row):
    patterns = []
    if row.get('bullish_pin', False):
        patterns.append("Pin Bar (Bullish)")
    if row.get('bullish_engulf', False):
        patterns.append("Bullish Engulfing")
    if row.get('inside_bar', False) and row['close'] > prev_row['high']:
        patterns.append("Inside Bar Breakout (Bullish)")
    return patterns

def pa_any_bear(row, prev_row):
    patterns = []
    if row.get('bearish_pin', False):
        patterns.append("Pin Bar (Bearish)")
    if row.get('bearish_engulf', False):
        patterns.append("Bearish Engulfing")
    if row.get('inside_bar', False) and row['close'] < prev_row['low']:
        patterns.append("Inside Bar Breakout (Bearish)")
    return patterns

def classify_signal_mode(signal, pa_1m_bull, pa_1m_bear, pa_5m_bull, pa_5m_bear, pa_15m_bull, pa_15m_bear):
    # Priority: Strong > New Weak > Old Weak > None
    if signal is None:
        return None, None, None, []
    if 'strong' in signal:
        if (signal == 'strong_buy' and (pa_5m_bull or pa_15m_bull)) or (signal == 'strong_sell' and (pa_5m_bear or pa_15m_bear)):
            tf = "5M" if (pa_5m_bull or pa_5m_bear) else "15M"
            patterns = pa_5m_bull + pa_15m_bull if 'buy' in signal else pa_5m_bear + pa_15m_bear
            return 'strong', 'bullish' if 'buy' in signal else 'bearish', tf, patterns
    if 'weak' in signal:
        if (pa_1m_bull and 'buy' in signal):
            return 'new_weak', 'bullish', '1M', pa_1m_bull
        if (pa_1m_bear and 'sell' in signal):
            return 'new_weak', 'bearish', '1M', pa_1m_bear
        # NO PA: classic old weak logic
        if not pa_1m_bull and not pa_1m_bear:
            return 'old_weak', 'bullish' if 'buy' in signal else 'bearish', '1M', []
    return None, None, None, []

# --- Updated Lot size selection logic with old/new weak combined logic ---
def select_lot_advanced(mode, old_weak_present, new_weak_present):
    """Return lot size based on complex weak signal interaction rules."""
    if mode == 'strong':
        return round(random.uniform(0.1, 0.5), 2)
    if mode == 'old_weak' and not new_weak_present:
        # Only old weak signal present
        return round(random.uniform(0.01, 0.05), 2)
    if mode == 'new_weak' and old_weak_present:
        # Both old + new weak present
        return round(random.uniform(0.05, 0.09), 2)
    if mode == 'new_weak' and not old_weak_present:
        # Only new weak present
        return round(random.uniform(0.01, 0.05), 2)
    # default fallback
    return 0.01

def select_sl_tp(mode, direction, entry):
    pip = 0.10
    if mode == 'strong':
        sl_dist, tp_dist = 20 * pip, 40 * pip
    else:
        sl_dist, tp_dist = 10 * pip, 15 * pip
    if direction == 'bullish':
        return entry - sl_dist, entry + tp_dist
    else:
        return entry + sl_dist, entry - tp_dist

def wait_for_candle_close(tf_label, tf_const):
    waiting_animation = ['.', '..', '...']
    idx = 0
    last_candle_time = None
    while True:
        rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, 1)
        if rates is None or len(rates) == 0:
            print_dynamic(f"Failed to fetch {tf_label} candle. Retrying{waiting_animation[idx]}{' ' * 10}")
            idx = (idx + 1) % len(waiting_animation)
            time.sleep(1)
            continue
        current_candle_time = pd.to_datetime(rates[0]['time'], unit='s')
        if last_candle_time is None or current_candle_time > last_candle_time:
            break
        else:
            print_dynamic(f"Waiting for new {tf_label} candle to close{waiting_animation[idx]}{' ' * 10}")
            idx = (idx + 1) % len(waiting_animation)
            time.sleep(1)
    print_dynamic(' ' * 80 + '\r')  # Clear line after done

class TradeTracker:
    def __init__(self, symbol):
        self.symbol = symbol
        self.last_printed_pnl = None
        self.csv_file = "trades_log.csv"
        # Prepare CSV file with headers if not exists
        if not os.path.exists(self.csv_file):
            with open(self.csv_file, mode='w', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(["Trading Logic", "Entry Price", "Take Profit (TP)", "Stop Loss (SL)", "Lot Size", "Timestamp"])

    def update_pnl(self):
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=30)
        closed_deals = mt5.history_deals_get(start_time, end_time)
        if closed_deals is None:
            return
        total_pnl = 0
        for deal in closed_deals:
            if hasattr(deal, 'symbol') and deal.symbol != self.symbol:
                continue
            if hasattr(deal, 'entry') and deal.entry != mt5.DEAL_ENTRY_OUT:
                continue
            if abs(deal.profit) < 0.0001:
                continue
            total_pnl += deal.profit

        if self.last_printed_pnl is None or round(total_pnl, 2) != round(self.last_printed_pnl, 2):
            print(f"Net P&L (closed trades last 30 days): {total_pnl:.2f}\n")
            self.last_printed_pnl = total_pnl

    def log_trade(self, trading_logic, entry, tp, sl, lot):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.csv_file, mode='a', newline='') as file:
            writer = csv.writer(file)
            writer.writerow([trading_logic, f"{entry:.5f}", f"{tp:.5f}", f"{sl:.5f}", f"{lot:.2f}", timestamp])


def place_market_order(symbol, direction, lot, sl_price, tp_price, deviation=20):
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        print(f"Symbol {symbol} not found.")
        return False
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            print(f"Symbol {symbol} not visible and failed to select.")
            return False

    tick = mt5.symbol_info_tick(symbol)
    if direction == 'bullish' or direction == 'buy':
        price = tick.ask
        order_type = mt5.ORDER_TYPE_BUY
    elif direction == 'bearish' or direction == 'sell':
        price = tick.bid
        order_type = mt5.ORDER_TYPE_SELL
    else:
        print(f"Invalid direction: {direction}")
        return False

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": deviation,
        "magic": 10032024,
        "comment": "SmartMT5Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[ORDER EXECUTED] {direction.upper()} {lot} lots at {price:.2f} | SL: {sl_price:.2f}, TP: {tp_price:.2f}")
        return True
    else:
        print(f"[ORDER FAILED] Retcode={result.retcode}, Comment: {result.comment}")
        return False


print("\n--- Starting SmartMT5Bot trading loop ---\n")
tracker = TradeTracker(symbol)

while True:
    # --- Wait 1-min candle close for weak signals ---
    wait_for_candle_close('1M', mt5.TIMEFRAME_M1)

    # Fetch and prepare data and indicators for all timeframes
    dfs = {}
    for tf_const, tf_label, bars in zip([mt5.TIMEFRAME_M1, mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15], ['1M', '5M', '15M'], [100, 50, 50]):
        rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, bars)
        if rates is None or len(rates) == 0:
            print(f"[Warning] Failed to fetch {tf_label} data. Skipping cycle.")
            time.sleep(5)
            continue
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        add_indicators(df)
        dfs[tf_label] = detect_price_action_patterns(df)

    # If data incomplete, skip iteration
    if any(tf not in dfs for tf in ['1M', '5M', '15M']):
        time.sleep(5)
        continue

    df_1, df_5, df_15 = dfs['1M'], dfs['5M'], dfs['15M']

    # Multi-timeframe indicator signal
    multi_signal = get_multi_tf_signals(df_1, df_5, df_15)

    # Extract latest and previous rows for PA functions
    def get_rows(df):
        if len(df) < 2:
            return None, None
        return df.iloc[-1], df.iloc[-2]

    row_1, prev_1 = get_rows(df_1)
    row_5, prev_5 = get_rows(df_5)
    row_15, prev_15 = get_rows(df_15)

    if (row_1 is None or prev_1 is None or
        row_5 is None or prev_5 is None or
        row_15 is None or prev_15 is None):
        print("Insufficient candle data for price action analysis. Skipping iteration.")
        time.sleep(5)
        continue

    # PA detections per timeframe
    pa_1m_bull = pa_any_bull(row_1, prev_1)
    pa_1m_bear = pa_any_bear(row_1, prev_1)
    pa_5m_bull = pa_any_bull(row_5, prev_5)
    pa_5m_bear = pa_any_bear(row_5, prev_5)
    pa_15m_bull = pa_any_bull(row_15, prev_15)
    pa_15m_bear = pa_any_bear(row_15, prev_15)

    if multi_signal is not None:
        mode, direction, tf, patterns = classify_signal_mode(
            multi_signal, pa_1m_bull, pa_1m_bear,
            pa_5m_bull, pa_5m_bear, pa_15m_bull, pa_15m_bear)
    else:
        mode, direction, tf, patterns = None, None, None, []

    # For strong signals wait additionally for 5-min candle close
    if mode == 'strong':
        wait_for_candle_close('5M', mt5.TIMEFRAME_M5)

    # Calculate lot with advanced logic for weak signals
    old_weak_present = mode == 'old_weak'
    new_weak_present = mode == 'new_weak'
    lot = select_lot_advanced(mode, old_weak_present, new_weak_present)

    # Set entry price source
    if tf in ['1M', '5M', '15M']:
        df_entry = {'1M': df_1, '5M': df_5, '15M': df_15}[tf]
        entry_price = df_entry['close'].iloc[-1]
    else:
        print_dynamic("No valid confluence of price action and indicator signals. No trade executed.            \r")
        time.sleep(5)
        continue

    if mode is None or direction is None:
        print_dynamic("No valid confluence of price action and indicator signals. No trade executed.            \r")
        time.sleep(5)
        continue

    sl, tp = select_sl_tp(mode, direction, entry_price)

    # Determine string for logging trading logic
    if mode == 'strong':
        trading_logic_str = f"Strong {'Buy' if direction=='bullish' else 'Sell'}"
    elif mode == 'new_weak' and old_weak_present:
        trading_logic_str = f"New Weak {'Buy' if direction=='bullish' else 'Sell'} + Old Confirmed"
    elif mode == 'new_weak':
        trading_logic_str = f"New Weak {'Buy' if direction=='bullish' else 'Sell'} only"
    elif mode == 'old_weak':
        trading_logic_str = f"Old Weak {'Buy' if direction=='bullish' else 'Sell'} only"
    else:
        trading_logic_str = "Unknown"

    print("\n--- Price Action Signal Detected ---")
    print(f"Timeframe: {tf}")
    print(f"Pattern(s): {', '.join(patterns) if patterns else '(none for classic weak logic)'}")
    print(f"Signal: {'Buy' if direction == 'bullish' else 'Sell'} ({mode.replace('_', ' ').title()})")
    print(f"Entry price: {entry_price:.2f}")
    print(f"Stop Loss: {sl:.2f}")
    print(f"Take Profit: {tp:.2f}")
    print(f"Lot Size: {lot:.2f}")
    print(f"Trading Logic: {trading_logic_str}")
    print("--------------------------------------\n")

    # Check open positions before trading
    positions = mt5.positions_get(symbol=symbol)
    open_count = len(positions) if positions else 0
    print(f"[Info] Current open positions for {symbol}: {open_count}")

    if open_count == 0:
        # EMA10 check on 1m timeframe for price zone confirmation before entry
        if direction == 'bullish' and not (df_1['close'].iloc[-1] > df_1['EMA_10'].iloc[-1]):
            print("[Info] Waiting for 1m close above EMA10 before BUY entry.")
        elif direction == 'bearish' and not (df_1['close'].iloc[-1] < df_1['EMA_10'].iloc[-1]):
            print("[Info] Waiting for 1m close below EMA10 before SELL entry.")
        else:
            # Place the order and log on success
            placed = place_market_order(symbol, direction, lot, sl, tp)
            if placed:
                tracker.log_trade(trading_logic_str, entry_price, tp, sl, lot)
            else:
                print("[Error] Order placement failed.")
    else:
        print("[Info] Existing position detected, skipping new entry.")

    tracker.update_pnl()
    time.sleep(5)
