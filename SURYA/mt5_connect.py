import MetaTrader5 as mt5
import pwinput
import pandas as pd
import ta
from datetime import datetime, timezone, timedelta
import time

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

def multi_tf_signal(df_m1, df_m5, df_m15):
    def check_single(df):
        if df is None or df.empty:
            return None
        last = df.iloc[-1]
        if (last['EMA_10'] > last['EMA_50']) and (last['RSI_14'] < 70) and (last['close'] > last['EMA_10']):
            return 'buy'
        if (last['EMA_10'] < last['EMA_50']) and (last['RSI_14'] > 30) and (last['close'] < last['EMA_10']):
            return 'sell'
        return None
    signals = [check_single(df_m1), check_single(df_m5), check_single(df_m15)]
    if signals.count('buy') == 3:
        return 'strong_buy'
    if signals.count('sell') == 3:
        return 'strong_sell'
    if signals.count('buy') >= 2:
        return 'buy'
    if signals.count('sell') >= 2:
        return 'sell'
    return None

def detect_price_action_patterns(df):
    df = df.copy()
    df['bullish_engulfing'] = ((df['close'] > df['open']) &
                               (df['open'].shift(1) > df['close'].shift(1)) &
                               (df['open'] < df['close'].shift(1)) &
                               (df['close'] > df['open'].shift(1)))
    df['bearish_engulfing'] = ((df['close'] < df['open']) &
                               (df['open'].shift(1) < df['close'].shift(1)) &
                               (df['open'] > df['close'].shift(1)) &
                               (df['close'] < df['open'].shift(1)))
    body = abs(df['close'] - df['open'])
    lower_wick = df[['open', 'close']].min(axis=1) - df['low']
    upper_wick = df['high'] - df[['open', 'close']].max(axis=1)
    candle_range = df['high'] - df['low']
    df['bullish_pinbar'] = (body <= candle_range * 0.3) & (lower_wick >= 2 * body) & (upper_wick <= body)
    df['bearish_pinbar'] = (body <= candle_range * 0.3) & (upper_wick >= 2 * body) & (lower_wick <= body)
    return df

def is_near_swing(row, df, window=5, how='high'):
    idx = df.index.get_loc(row.name)
    if idx < window or idx > len(df) - window - 1:
        return False
    recent = df.iloc[idx-window:idx+window+1]
    if how == 'high':
        return row['high'] == recent['high'].max()
    elif how == 'low':
        return row['low'] == recent['low'].min()
    return False

def price_action_filter(df_patterns):
    for idx in df_patterns.index[-5:]:
        row = df_patterns.loc[idx]
        near_high = is_near_swing(row, df_patterns, window=5, how='high')
        near_low = is_near_swing(row, df_patterns, window=5, how='low')
        if near_high and (row['bearish_engulfing'] or row['bearish_pinbar']):
            print(f"Bearish PA pattern near swing high at {idx}")
            return 'sell'
        if near_low and (row['bullish_engulfing'] or row['bullish_pinbar']):
            print(f"Bullish PA pattern near swing low at {idx}")
            return 'buy'
    return None

def calculate_trade_with_pips(symbol, signal, direction, market_price):
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("Unable to fetch account info from MT5.")

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        raise RuntimeError(f"{symbol} info not found in MT5.")

    balance = account.balance
    tick_size = getattr(symbol_info, 'point', 0.01)
    tick_value = getattr(symbol_info, 'trade_tick_value', 1.0)
    if tick_value == 0:
        tick_value = 1.0
    lot_step = symbol_info.volume_step
    max_lot = 0.5

    risk_perc = 0.05 if signal in ['strong_buy', 'strong_sell'] else 0.01

    pips = 20
    ticks_per_pip = 10
    sl_ticks = pips * ticks_per_pip
    stop_loss_distance = sl_ticks * tick_size

    risk_amount = balance * risk_perc

    lot_size = risk_amount / (sl_ticks * tick_value)
    lot_size = min(lot_size, max_lot)
    lot_size = max(symbol_info.volume_min, round(lot_size / lot_step) * lot_step)

    if direction == 'buy':
        sl_price = market_price - stop_loss_distance
        tp_price = market_price + 2 * stop_loss_distance
        entry = market_price
    elif direction == 'sell':
        sl_price = market_price + stop_loss_distance
        tp_price = market_price - 2 * stop_loss_distance
        entry = market_price
    else:
        sl_price = tp_price = entry = None
        lot_size = 0

    return entry, lot_size, sl_price, tp_price

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
    if direction.lower() == "buy":
        price = tick.ask
        order_type = mt5.ORDER_TYPE_BUY
    elif direction.lower() == "sell":
        price = tick.bid
        order_type = mt5.ORDER_TYPE_SELL
    else:
        print(f"Unknown direction: {direction}")
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
        "comment": "Multi-timeframe Python EA",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"Trade executed successfully: {direction.upper()} {lot} lots at {price:.2f}")
        print(f"SL: {sl_price:.2f}, TP: {tp_price:.2f}")
        return True
    else:
        print(f"Trade failed: retcode={result.retcode}, comment={result.comment}")
        return False

class TradeTracker:
    def __init__(self, symbol):
        self.symbol = symbol
        self.trades = {}

    def update_pnl(self):
        total_pnl = 0.0
        info_lines = []

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=30)

        closed_positions = mt5.history_deals_get(start_time, end_time)
        if closed_positions is None:
            print("Loaded 0 closed deals.")
            return

        for deal in closed_positions:
            if hasattr(deal, 'symbol') and deal.symbol != self.symbol:
                continue
            order = deal.order
            profit = deal.profit
            self.trades[order] = profit

        for order, profit in self.trades.items():
            status = "Profit" if profit > 0 else ("Loss" if profit < 0 else "Breakeven")
            info_lines.append(f"Trade order {order}: {status} {profit:.2f}")
            total_pnl += profit

        print("\nTrade Summary (Closed XAUUSD Trades):")
        for line in info_lines:
            print(line)
        print(f"Net P&L (closed trades, last 30 days): {total_pnl:.2f}\n")

        return total_pnl

tracker = TradeTracker(symbol)

dfs = {}
timeframe_names = {
    mt5.TIMEFRAME_M1: "1 Minute",
    mt5.TIMEFRAME_M5: "5 Minute",
    mt5.TIMEFRAME_M15: "15 Minute"
}

print("\n--- Starting trading loop (5 min candle frequency) ---")

last_candle_time = None

while True:
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 1)
    if rates is None or len(rates) == 0:
        print("Failed to fetch 5-minute candle.")
        time.sleep(15)
        continue
    current_candle_time = pd.to_datetime(rates[0]['time'], unit='s')

    if last_candle_time is None or current_candle_time > last_candle_time:
        print(f"\n--- New trading cycle started at {datetime.now()} ---")
        last_candle_time = current_candle_time

        # Update all dataframes & indicators
        for tf, count in [(mt5.TIMEFRAME_M1, 100), (mt5.TIMEFRAME_M5, 50), (mt5.TIMEFRAME_M15, 50)]:
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df['time'] = pd.to_datetime(df['time'], unit='s')
                df.set_index('time', inplace=True)
                dfs[tf] = df
                add_indicators(dfs[tf])
                print(f"{symbol} {timeframe_names[tf]} updated, last close: {dfs[tf]['close'].iloc[-1]:.2f}")
            else:
                print(f"No data for {symbol} timeframe {tf}")

        df_m1 = dfs.get(mt5.TIMEFRAME_M1)
        df_m5 = dfs.get(mt5.TIMEFRAME_M5)
        df_m15 = dfs.get(mt5.TIMEFRAME_M15)

        # Detect price action patterns on 5-minute timeframe
        df_m5_patterns = detect_price_action_patterns(df_m5)
        pa_signal = price_action_filter(df_m5_patterns)

        signal = multi_tf_signal(df_m1, df_m5, df_m15)

        print(f"Multi-timeframe Signal: {signal}")
        print(f"Price Action Pattern Signal on 5m: {pa_signal}")

        if signal and df_m1 is not None and not df_m1.empty:
            # Check PA confirmation with multi-timeframe signal
            if pa_signal is None:
                print("No valid price action pattern near swing on 5m - skipping trade")
                tracker.update_pnl()
                time.sleep(15)
                continue
            if (('buy' in signal and pa_signal != 'bullish') or
                ('sell' in signal and pa_signal != 'bearish')):
                print("Price action pattern does not confirm signal - skipping trade")
                tracker.update_pnl()
                time.sleep(15)
                continue

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                print("Failed to get tick data. Skipping this cycle.")
                time.sleep(15)
                continue

            direction = 'buy' if 'buy' in signal else 'sell'
            market_price = tick.ask if direction == 'buy' else tick.bid

            entry, lot, sl, tp = calculate_trade_with_pips(symbol, signal, direction, market_price)

            positions = mt5.positions_get(symbol=symbol) or []
            print(f"Open positions: {len(positions)}")

            if len(positions) == 0:
                waiting = False
                if direction == 'buy' and not (dfs[mt5.TIMEFRAME_M1]['close'].iloc[-1] > dfs[mt5.TIMEFRAME_M1]['EMA_10'].iloc[-1]):
                    waiting = True
                    print("Waiting: Price not above EMA_10 for BUY entry.")
                if direction == 'sell' and not (dfs[mt5.TIMEFRAME_M1]['close'].iloc[-1] < dfs[mt5.TIMEFRAME_M1]['EMA_10'].iloc[-1]):
                    waiting = True
                    print("Waiting: Price not below EMA_10 for SELL entry.")

                if waiting:
                    print("Price not in favourable zone, waiting for price...")
                elif lot > 0:
                    print(f"\nPLACING TRADE: {signal.upper()} {lot} lots at {entry:.2f}")
                    print(f"SL: {sl:.2f}, TP: {tp:.2f}")
                    place_market_order(symbol, direction, lot, sl, tp)
                else:
                    print("Invalid lot size, no trade executed.")
            else:
                print("Open position exists, skipping trade.")
        else:
            print("No valid multi-timeframe trade signal or insufficient data.")

        tracker.update_pnl()
    else:
        print("Waiting for new 5-minute candle to close...")

    time.sleep(15)  # poll every 15 seconds
