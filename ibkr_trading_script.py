import time
import datetime
import threading
import pandas as pd
from ibapi.wrapper import EWrapper
from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.scanner import ScannerSubscription
from ibapi.tag_value import TagValue
import traceback

class TradingApp(EClient, EWrapper):
    def __init__(self, trailing_stop_percent: int = 15, stall_time_sec: int = 5, price_stall_delta: int = 5, max_loss_per_trade: int = 200, volume_spike: int = None, spread: int = None, condition_str: str = None, filter_options: list = [], scan_options: list = [], number_of_rows: int = 50, asset_type: str = "STK", asset_loc: str = "STK.US.MAJOR", scan_code: str = "TOP_VOLUME_RATE"):
        EClient.__init__(self, wrapper=self)
        self.nextValidOrderId = None
        self.itercount = 0
        self.req_id_counter = 0
        self.historical_data = {}
        self.current_volumes = {}
        self.reqId_to_symbol = {}
        self.symbol_to_reqId = {}
        self.stock_data = {}
        self.rank_id_tracker = {}
        self.historical_data_reqId_mapper = {}
        self.is_calc_RVOL: bool = False
        self.current_minute = None
        self.previous_minute = None
        self.same_minute: bool = True
        self.stockOpen = {}
        self.currentOpenOrders = {}
        self.max_loss_per_trade = max_loss_per_trade
        self.trailing_stop_percent = trailing_stop_percent
        self.stall_time_sec = stall_time_sec
        self.price_stall_delta = price_stall_delta
        self.positions = {}
        self.open_orders = {}

        self.minuteCheck()
        self.orderCheck()

        self.condition_str = (
            f"self.stock_data[reqId]['macd'][0]['macd_open'] and "
            f"self.stock_data[reqId]['macd'][0]['ema_9'] >= self.stock_data[reqId]['macd'][0]['ema_20'] and "
            f"self.stock_data[reqId]['spread'] <= {spread if spread else 0.5} and "
            f"float(self.stock_data[reqId]['mark']) > float(self.stock_data[reqId]['vwap']) and "
            f"self.stock_data[reqId]['volume_spike'] > {volume_spike if volume_spike else 0.75}"
        ) if not condition_str else condition_str

        self.scanSub = ScannerSubscription()
        self.scanSub.numberOfRows = number_of_rows
        self.scanSub.instrument = asset_type
        self.scanSub.locationCode = asset_loc
        self.scanSub.scanCode = scan_code

        self.scan_options = [] if len(scan_options) == 0 else scan_options
        self.filter_options = [
            TagValue("volumeAbove","50000"),
            TagValue("marketCapBelow1e6", "1000"),
            TagValue("changePercAbove", "0.1"),
            TagValue('priceAbove', "0.99"),
            TagValue('priceBelow', "20.01"),
        ] if len(filter_options) == 0 else filter_options

        print('self.filter_options', self.filter_options, '\n')

    def error(self, reqId, errorCode, errorString):
        if 'Warning' not in errorString:
            print("Error3", f"{reqId}, {errorCode}, {errorString}\n")

    ################################################################################################################################################
    ## APP
    ################################################################################################################################################

    def connect_app(self, app, host_address: str = "127.0.0.1", port: int = 7497, client_id: int = 0, run_app: bool = False, run_scanner: bool = False):
        self.app = app
        self.app.connect(host_address, port, client_id)
        if run_app:
            self.run_app()
        if run_scanner:
            self.run_scanner()

    def run_app(self):
        t1 = threading.Thread(target=self.app.run)
        t1.start()

        while self.app.nextValidOrderId is None:
            print("Waiting for TWS connection acknowledgement ...\n")
            time.sleep(1)

        print('Connection established!\n')

        self.app.reqPositions()

    def disconnect_app(self):
        self.app.disconnect()

    ################################################################################################################################################
    ## CALCULATIONS
    ################################################################################################################################################

    def calculate_trailing_stop_percent(self, current_mark, purchase_price):
        price_movement_percent = ((current_mark - purchase_price) / purchase_price) * 100

        if price_movement_percent < 2:
            return 25
        elif 2 <= price_movement_percent < 4:
            return 20
        elif 4 <= price_movement_percent < 6:
            return 15
        elif 6 <= price_movement_percent < 10:
            return 10
        else:
            return 5

    def calculate_volume_spike(self, reqId):
        open = self.stockOpen[reqId]
        current_mark = self.stock_data[reqId]['mark']
        vol_change_pct_sign = -1 if current_mark - open < 0 else 1
        historical_data_length = len(self.historical_data[reqId]['calc_volume_spike'])
        if reqId in self.historical_data:
            avg_volume = sum(self.historical_data[reqId]['calc_volume_spike']) / historical_data_length
            current_volume = self.stock_data[reqId]['minute_volume']
            relative_volume = current_volume / avg_volume
            vol_change_pct = abs(relative_volume - 1.0)
            self.stock_data[reqId]['volume_spike'] = vol_change_pct_sign * vol_change_pct

    def run_calc_macd(self, reqId):
        now_datetime = datetime.datetime.now()

        if 'macd_creation_time' not in self.stock_data[reqId].keys() or (now_datetime - self.stock_data[reqId]['macd_creation_time']).total_seconds() > 10:
            macd_data = self.calculate_macd(self.historical_data[reqId]['macd'])[-1:]
            self.stock_data[reqId]['macd'] = macd_data
            self.stock_data[reqId]['macd_creation_time'] = datetime.datetime.now()

    @staticmethod
    def calculate_ema(current_price, previous_ema, window):
        alpha = 2 / (window + 1)
        return current_price * alpha + previous_ema * (1 - alpha)

    def calculate_macd(self, data, short_window=12, long_window=26, signal_window=9):
        ema_short = []
        ema_long = []
        ema_9 = []
        ema_20 = []
        macd_line = []
        signal_line = []
        macd_histogram = []

        for i in range(len(data)):
            close_price = data[i]['close']
            if i == 0:
                ema_short.append(close_price)
                ema_long.append(close_price)
                ema_9.append(close_price)
                ema_20.append(close_price)
            else:
                ema_short.append(self.calculate_ema(close_price, ema_short[-1], short_window))
                ema_long.append(self.calculate_ema(close_price, ema_long[-1], long_window))
                ema_9.append(self.calculate_ema(close_price, ema_9[-1], 9))
                ema_20.append(self.calculate_ema(close_price, ema_20[-1], 20))

            macd_line.append(ema_short[-1] - ema_long[-1])

            if i < signal_window:
                signal_line.append(None)
            else:
                if signal_line[-1] is None:
                    signal_line.append(sum(macd_line[-signal_window:]) / signal_window)
                else:
                    signal_line.append(self.calculate_ema(macd_line[-1], signal_line[-1], signal_window))

            if signal_line[-1] is not None:
                macd_histogram.append(macd_line[-1] - signal_line[-1])
            else:
                macd_histogram.append(None)

        for i in range(len(data)):
            data[i]['ema_9'] = ema_9[i]
            data[i]['ema_20'] = ema_20[i]
            data[i]['macd'] = macd_line[i]
            data[i]['signal'] = signal_line[i]
            if data[i]['macd'] and data[i]['signal']:
                data[i]['macd_open'] = True if data[i]['macd'] > data[i]['signal'] else False
            else:
                data[i]['macd_open'] = None
            data[i]['macd_histogram'] = macd_histogram[i]

        return data

    @staticmethod
    def parse_execution_time(execution_time_str):
        return datetime.datetime.strptime(execution_time_str, "%Y%m%d %H:%M:%S")

    ################################################################################################################################################
    ## CHECKS
    ################################################################################################################################################

    def minuteCheck(self):
        current_time = datetime.datetime.now()

        seconds_until_next_minute = 60 - current_time.second

        print('seconds_until_next_minute', seconds_until_next_minute, '\n')

        print('60 sec check | STOCK TRACKER DATA | :\n\n', self.rank_id_tracker, '\n\n', 'LENGTH', len(self.rank_id_tracker.keys()), '\n')

        reqIds = self.reqId_to_symbol.keys()

        for reqId in reqIds:
            self.stock_data[reqId]['minute_volume'] = 0

        threading.Timer(seconds_until_next_minute, self.minuteCheck).start()

    def orderCheck(self):
        try:
            print('2 sec check | ORDER DATA | :\n\n', self.currentOpenOrders, '\n')

            for symbol in self.currentOpenOrders.keys():
                symbol_values = self.currentOpenOrders[symbol]
                sold_values = symbol_values['sell']

                if symbol_values['buy']['active'] and sold_values['active']:
                    shares_purchased = symbol_values['buy']['shares_purchased']
                    sold_order_ids = sold_values['order_ids']
                    sold_executed_shares = sold_values['executed_shares']

                    if shares_purchased and sold_executed_shares and shares_purchased - sold_executed_shares <= 0:
                        self.resetCurrentOpenOrder(symbol)
                    else:
                        current_time = datetime.datetime.now()
                        time_delta = (current_time - sold_values['execution_time']).total_seconds()

                        if time_delta > 5:
                            for sold_order_id in sold_order_ids:
                                self.cancelOrder(sold_order_id)

                                order_ids = self.currentOpenOrders[symbol]['sell']['order_ids']

                                if sold_order_id in order_ids:
                                    index = order_ids.index(sold_order_id)
                                    
                                    order_ids.pop(index)

                            self.sell_place_order(reqId=self.symbol_to_reqId[symbol]['reqId'], trigger_by_check=True)

        except Exception as e:
            print(f'orderCheck error: {e}\n')
            traceback.print_exc()

        threading.Timer(2, self.orderCheck).start()

    ################################################################################################################################################
    ## HISTORICAL DATA
    ################################################################################################################################################

    def retrieve_historical_data(self, reqId, contract):
        historical_reqId = reqId
        reqId = self.historical_data_reqId_mapper[reqId]

        end_date_time = datetime.datetime.now().strftime("%Y%m%d %H:%M:%S")
        duration_str = "900 S"
        bar_size_setting = "1 min"
        what_to_show = "TRADES"
        use_rth = 0
        format_date = 1
        keep_up_to_date = False
        chartOptions = []

        self.reqHistoricalData(historical_reqId, contract, end_date_time, duration_str, bar_size_setting, what_to_show, use_rth, format_date, keep_up_to_date, chartOptions)
        self.historical_data[reqId] = []

    def request_historical_data(self, symbol, reqId):
        exchange = self.stock_data[reqId]['exchange']
        contract = self.create_contract(symbol, exchange, "USD")

        self.stock_data[reqId]['contract'] = contract

        historical_req_id = self.get_next_req_id()
        self.historical_data_reqId_mapper[historical_req_id] = reqId
        volume_spike_thread = threading.Thread(target=self.retrieve_historical_data, args=(historical_req_id, contract))

        volume_spike_thread.start()
        volume_spike_thread.join()

    def historicalData(self, reqId, bar):
        reqId = self.historical_data_reqId_mapper[reqId]

        if reqId not in self.historical_data or not isinstance(self.historical_data[reqId], dict):
            self.historical_data[reqId] = {}

        # VOLUME SPIKE
        if 'calc_volume_spike' not in self.historical_data[reqId]:
            self.historical_data[reqId]['calc_volume_spike'] = []
            self.historical_data[reqId]['macd'] = []
        self.historical_data[reqId]['calc_volume_spike'].append(bar.volume)
        self.stockOpen[reqId] = bar.open
        self.historical_data[reqId]['macd'].append({
            'date': bar.date,
            'open': bar.open,
            'high': bar.high,
            'low': bar.low,
            'close': bar.close,
            'volume': bar.volume
        })

    def historicalDataEnd(self, reqId, start, end):
        reqId = self.historical_data_reqId_mapper[reqId]

        try:
            # VOLUME SPIKE
            self.calculate_volume_spike(reqId)

            # MACD
            self.run_calc_macd(reqId)

        except Exception as e:
            print(f'historicalDataEnd error: {e}\n')
            traceback.print_exc()

    def create_contract(self, symbol, exchange, currency):
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = exchange
        contract.currency = currency
        return contract

    ################################################################################################################################################
    ## IDs
    ################################################################################################################################################

    def nextId(self):
        self.orderId += 1
        return self.orderId

    def nextValidId(self, orderId):
        print("Setting nextValidOrderId:", orderId, '\n')
        self.app.nextValidOrderId = orderId
        self.orderId = orderId

    def get_next_req_id(self):
        req_id = self.req_id_counter
        self.req_id_counter += 1
        return req_id

    ################################################################################################################################################
    ## ORDERS
    ################################################################################################################################################

    def run_buy_order(self, reqId):
        try:
            symbol = self.stock_data[reqId]['symbol'] # NOQA
            result = eval(self.condition_str)

            if result and not self.currentOpenOrders[symbol]['buy']['active']:
                self.currentOpenOrders[symbol]['buy']['active'] = True
                self.buy_place_order(reqId=reqId)

        except Exception as e:
            print(f"Buy order exception | {self.stock_data[reqId]['symbol']} |: {e}\n")

    def buy_place_order(self, reqId):
        symbol = self.stock_data[reqId]['symbol']

        contract = self.stock_data[reqId]['contract']
        orderId = self.nextId()

        purchase_price = self.stock_data[reqId]['ask']
        shares_purchased = 500

        entryOrder = Order()
        entryOrder.orderId = orderId
        entryOrder.transmit = True
        entryOrder.eTradeOnly = ''
        entryOrder.firmQuoteOnly = ''
        entryOrder.outsideRth = True
        entryOrder.totalQuantity = shares_purchased
        entryOrder.tif = 'DAY'
        entryOrder.action = 'BUY'
        entryOrder.orderType = 'MKT'
        entryOrder.orderType = 'LMT'
        entryOrder.lmtPrice = purchase_price

        self.currentOpenOrders[symbol]['buy']['order_ids'].append(orderId)

        print(f"Placing BUY order | {self.stock_data[reqId]['symbol']} | with ID: {orderId}\n")
        self.placeOrder(orderId, contract, entryOrder)

    def run_sell_order(self, reqId, price, symbol):
        try:
            try:
                max_mark = self.currentOpenOrders[symbol]['buy']['max_mark']
                if max_mark == 0:
                    max_mark = price
                    self.currentOpenOrders[symbol]['buy']['max_mark'] = max_mark
                else:
                    max_mark = self.currentOpenOrders[symbol]['buy']['max_mark']

            except Exception:
                max_mark = price
                self.currentOpenOrders[symbol]['buy']['max_mark'] = max_mark

            purchase_price = self.currentOpenOrders[symbol]['buy']['purchase_price']

            # MAX MARK

            print('max_mark', price > max_mark, '\n')
            print('MAX TRADE LOSS', None if not purchase_price else price < purchase_price, '\n')
            print('PRICE IN BETWEEN', None if not max_mark or not purchase_price else max_mark > price >= purchase_price, '\n')

            if max_mark and price > max_mark:
                self.currentOpenOrders[symbol]['buy']['max_mark'] = price

            # MAX TRADE LOSS

            elif purchase_price and price < purchase_price:
                print('MAX TRADE LOSS')
                shares_purchased = self.currentOpenOrders[symbol]['buy']['shares_purchased']
                current_loss_amount = (purchase_price - price) * shares_purchased

                if current_loss_amount >= self.max_loss_per_trade:
                    self.currentOpenOrders[symbol]['sell']['active'] = True
                    self.sell_place_order(reqId=reqId, max_trade_loss=True)

            elif max_mark and purchase_price and max_mark > price >= purchase_price:
                # PRICE STALL

                print('PRICE STALL')

                purchase_price = self.currentOpenOrders[symbol]['buy']['purchase_price']
                price_delta = abs(price - purchase_price)


                if price_delta <= self.price_stall_delta:
                    current_time = datetime.datetime.now()
                    execution_time_str = self.currentOpenOrders[symbol]['buy']['execution_time']
                    execution_time = self.parse_execution_time(execution_time_str)
                    time_delta = (current_time - execution_time).total_seconds()

                    if time_delta >= self.stall_time_sec:
                        self.currentOpenOrders[symbol]['sell']['active'] = True
                        self.sell_place_order(reqId=reqId, price_stalled=True)

                else:
                    # TRAILING STOP

                    print('TRAILING STOP')

                    trailing_stop_percent = self.calculate_trailing_stop_percent(current_mark=price, purchase_price=purchase_price)

                    print('trailing_stop_percent', trailing_stop_percent, '\n')

                    trailing_stop_price = 0 if trailing_stop_percent == 0 else max_mark - (max_mark - purchase_price) * trailing_stop_percent / 100

                    if price <= trailing_stop_price:
                        self.currentOpenOrders[symbol]['sell']['active'] = True
                        self.sell_place_order(reqId=reqId, trailing_stop=True)

        except Exception as e:
            print(f'sell exception: {e}')
            print('\n')
            traceback.print_exc()

    def sell_place_order(self, reqId, shareAmount: int = None, max_trade_loss: bool = False, trailing_stop: bool = False, price_stalled: bool = False, trigger_by_check: bool = False):
        symbol = self.stock_data[reqId]['symbol']

        contract = self.stock_data[reqId]['contract']
        orderId = self.nextId()

        sellOrder = Order()
        sellOrder.orderId = orderId
        sellOrder.transmit = True
        sellOrder.eTradeOnly = ''
        sellOrder.firmQuoteOnly = ''
        sellOrder.outsideRth = True
        sellOrder.tif = 'DAY'
        sellOrder.action = 'SELL'
        # sellOrder.orderType = 'MKT'
        sellOrder.orderType = 'LMT'
        sellOrder.totalQuantity = shareAmount if shareAmount else self.currentOpenOrders.get(symbol, {}).get('shares_purchased', 0)
        sellOrder.totalQuantity = self.currentOpenOrders[symbol]['buy']['shares_purchased']
        sellOrder.lmtPrice = self.stock_data[reqId].get('bid', 0)

        self.currentOpenOrders[symbol]['sell']['order_ids'].append(orderId)
        self.currentOpenOrders[symbol]['sell']['execution_time'] = datetime.datetime.now()

        print(f"Placing SELL order | {'trigger_by_check' if trigger_by_check else 'max_trade_loss' if max_trade_loss else 'trailing_stop' if trailing_stop else 'price_stalled' if price_stalled else None} | {symbol} | with ID: {orderId}\n")
        self.placeOrder(orderId, contract, sellOrder)

    def execDetails(self, reqId, contract, execution):
        try:
            symbol = contract.symbol

            if execution.side == 'BOT':
                self.currentOpenOrders[symbol]['buy']['order_ids'].append(execution.orderId)
                self.currentOpenOrders[symbol]['buy']['purchase_price'] = execution.price
                self.currentOpenOrders[symbol]['buy']['shares_purchased'] += execution.shares
                self.currentOpenOrders[symbol]['buy']['execution_time'] = execution.time
            else:
                self.currentOpenOrders[symbol]['sell']['order_ids'].append(execution.orderId)
                self.currentOpenOrders[symbol]['sell']['executed_shares'] += execution.shares

            print(f"Execution Details: ID:{execution.orderId} || {execution.side} {execution.shares} {contract.symbol} @ {execution.time}\n")

        except Exception as e:
            print(f'execDetails error: {e}\n')
            traceback.print_exc()

    def openOrder(self, orderId, contract, order, orderState):
        if orderState.status not in ['Cancelled', 'Filled']:
            symbol = contract.symbol

            self.open_orders[orderId] = {
                'symbol': symbol,
                'action': order.action,
                'orderType': order.orderType,
                'totalQuantity': order.totalQuantity,
                'lmtPrice': order.lmtPrice,
                'status': orderState.status
            }

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        if orderId in self.open_orders:
            self.open_orders[orderId]['status'] = status
            self.open_orders[orderId]['filled'] = filled
            self.open_orders[orderId]['remaining'] = remaining
            self.open_orders[orderId]['avgFillPrice'] = avgFillPrice

    def openOrderEnd(self):
        print("All open orders have been received")
        # print('self.open_orders', self.open_orders)

    def position(self, account: str, contract: Contract, position, avgCost: float):
        print('position', position)
        symbol = contract.symbol
        if symbol not in self.positions:
            self.positions[symbol] = {'position': 0, 'avgCost': 0, 'count': 0}
        self.positions[symbol]['position'] += position
        self.positions[symbol]['avgCost'] += avgCost * position
        self.positions[symbol]['count'] += 1

    def positionEnd(self):
        print("All positions have been received")
        for symbol, data in self.positions.items():
            total_position = data['position']
            avg_cost = data['avgCost'] / data['position'] if total_position != 0 else 0
            print(f"Symbol: {symbol}, Total Position: {total_position}, Average Cost: {avg_cost}")
            print(f"Symbol: {symbol}, Total Position: {total_position}")

    ################################################################################################################################################
    ## RESETS
    ################################################################################################################################################

    def resetCurrentOpenOrder(self, symbol):
        self.currentOpenOrders[symbol] = {
            'buy': {
                'active': False,
                'order_ids': [],
                'max_mark': 0,
                'purchase_price': None,
                'shares_purchased': 0,
                'execution_time': None
            },
            'sell': {
                'active': False,
                'order_ids': [],
                'executed_shares': 0,
                'execution_time': None
            }
        }

    ################################################################################################################################################
    ## SCANNER
    ################################################################################################################################################

    def scannerData(self, reqId, rank, contractDetails, distance, benchmark, projection, legsStr):
        symbol = contractDetails.contract.symbol

        print('scannerData symbol:', symbol, '\n')

        rankId = self.itercount + reqId
        self.reqId_to_symbol[rankId] = symbol
        self.symbol_to_reqId[symbol] = {'reqId': rankId}

        if symbol not in self.rank_id_tracker.keys():
            self.rank_id_tracker[symbol] = rankId
            self.stock_data[rankId] = {
                'symbol': symbol,
                'exchange': contractDetails.contract.exchange
            }

            self.resetCurrentOpenOrder(symbol)

            self.app.reqMktData(rankId, contractDetails.contract, "232,233,165,375,595", False, False, [])
            self.itercount += 1

    def scannerDataEnd(self, reqId):
        self.cancelScannerSubscription(reqId)

    def run_scanner(self):
        self.app.reqScannerSubscription(self.app.nextId(), self.scanSub, self.scan_options, self.filter_options)

    ################################################################################################################################################
    ## TICKS
    ################################################################################################################################################

    def tickPrice(self, reqId, tickType, price, attrib):
        if tickType == 37:
            self.stock_data[reqId]['mark'] = price

            symbol = self.stock_data[reqId]['symbol']

            if self.currentOpenOrders[symbol]['buy']['active'] and not self.currentOpenOrders[symbol]['sell']['active']:
                self.run_sell_order(reqId=reqId, price=price, symbol=symbol)

        elif tickType == 1:
                self.stock_data[reqId]['bid'] = price
        elif tickType == 2:
                self.stock_data[reqId]['ask'] = price

        if 'bid' in self.stock_data[reqId].keys() and 'ask' in self.stock_data[reqId].keys():
            self.stock_data[reqId]['spread'] = self.stock_data[reqId]['ask'] - self.stock_data[reqId]['bid']

    def tickString(self, reqId, tickType, size):
        if tickType == 48:
            vwap = size.split(';')[-2]
            self.stock_data[reqId]['vwap'] = vwap
        elif tickType == 77:
            if reqId in self.reqId_to_symbol:
                symbol = self.reqId_to_symbol[reqId]
                self.request_historical_data(symbol, reqId)

            volume_parsed = size.split(';')[1]

            try:
                self.stock_data[reqId]['minute_volume'] += float(volume_parsed)
            except Exception:
                self.stock_data[reqId]['minute_volume'] = 0.0
                self.stock_data[reqId]['minute_volume'] += float(volume_parsed)

            self.run_buy_order(reqId)


################################################################################################################################################
## START SCRIPT
################################################################################################################################################

filter_options = [
            TagValue("volumeAbove","10000"),
            TagValue("marketCapBelow1e6", "1000"),
            TagValue("socialSentimentScoreChangeAbove", "0.1"),
            TagValue("curMACDAbove", "0.01"),
            TagValue("curMACDSignalAbove", "0.01"),
            TagValue("curMACDDistAbove", "0.01"),
            TagValue("changePercAbove", "0.1"),
            TagValue('priceAbove', "0.99"),
            TagValue('priceBelow', "20.01")
        ]

scans = {
    '0': 'MOST_ACTIVE',
    '1': 'TOP_PERC_GAIN',
    '2': 'HOT_BY_PRICE_RANGE',
    '3': 'HOT_BY_VOLUME',
    '4': 'TOP_TRADE_RATE',
    '5': 'TOP_VOLUME_RATE'
}

trading_app = TradingApp(trailing_stop_percent=15, stall_time_sec=305, price_stall_delta=0.10, max_loss_per_trade=1, volume_spike=3, spread=0.05, number_of_rows=10, scan_code=scans['1'], filter_options=filter_options)
trading_app.connect_app(app=trading_app, run_app=True, run_scanner=True)
