
from typing import Dict


class Position:
    def __init__(self, symbol: str, holding: float = 0, total_cost: float = 0):
        self.symbol = symbol
        self.available_holding = holding # 当前可用仓位
        self.total_cost = total_cost # 当前持仓的成本
        self.pending_buy_holding = 0  # 等待成交的买入仓位
        self.pending_buy_cash = 0 # 未成交的买单占用的资金
        self.pending_sell_holding = 0 # 等待成交的卖出仓位
        self.pending_sell_cash = 0 # 未成交的卖单占用的资金

class Account:
    def __init__(self, cash: float):
        # 账户资金
        self.available_cash = cash  # 可用资金
        self.pending_cash = 0 # 已占用资金
        self.init_cash = cash # 初始资金
        self.min_cash_remaining = cash # 最少时剩余资金
        
        # 持仓
        self.positions: Dict[str, Position] = {}
        
        # 统计数据
        self.cross_profit = 0.0 # 账户浮盈
        self.min_cross_profit = 0.0
        self.max_cross_profit = 0.0
        self.total_profit = 0.0
        self.total_calc_count = 0.0
        self.NoEnoughCashCount = 0.0
        
    def Summy(self, symbol: str, curr_market_price: float):
        if symbol not in self.positions.keys():
            return 
          
        position = self.positions[symbol]
        # 总持仓位 = 可用持仓 + 未成交的卖单仓位
        total_holding = position.available_holding + position.pending_sell_holding
        total_cash = round(position.pending_buy_cash + self.available_cash, 2)
        total_cost = round(position.total_cost, 2)
        profit = round(total_holding * curr_market_price + total_cash - self.init_cash, 2)
        self.total_profit += profit
        self.total_calc_count += 1
        # print(f" {position.available_holding} {position.total_cost}, {position.pending_buy_holding} {position.pending_buy_cash}, {position.pending_sell_holding} {position.pending_sell_cash}")
        print(f"Account Summy: \n    InitCash: {self.init_cash}, RemainCash: {total_cash}, MaxCashUsed: {round(self.init_cash - self.min_cash_remaining, 2)}, \n    RemainHolding: {total_holding}, TotalCost: {total_cost} CurrPrice: {curr_market_price} \n    CrossProfit: {profit}, MaxProfit: {self.max_cross_profit}, MaxLoss: {self.min_cross_profit}, AvgProfit: {round(self.total_profit/self.total_calc_count, 2)}, \n    NoEnoughCash: {self.NoEnoughCashCount}\n")
        
    def calc(self, symbol: str, curr_market_price: float):
        if symbol not in self.positions.keys():
            return 
          
        position = self.positions[symbol]
        # 总持仓位 = 可用持仓 + 未成交的卖单仓位
        total_holding = position.available_holding + position.pending_sell_holding
        total_cash = round(position.pending_buy_cash + self.available_cash, 2)
        profit = round(total_holding * curr_market_price + total_cash - self.init_cash, 2)
        if self.min_cash_remaining > self.available_cash:
            self.min_cash_remaining = self.available_cash
        if profit < self.min_cross_profit:
            self.min_cross_profit = profit
            # print(f" max loss: {self.min_cross_profit} {round(self.available_cash)} {total_holding} {position.total_cost}")
        if profit > self.max_cross_profit:
            self.max_cross_profit = profit
        self.total_profit += profit
        self.total_calc_count += 1
        self.cross_profit = profit
        
    def reflash(self, cash: float):
        self.available_cash = cash  # 可用资金
        self.pending_cash = 0 # 已占用资金
        self.init_cash = cash # 初始资金
        self.min_cash_remaining = cash # 最少时剩余资金
        
        self.cross_profit = 0.0 # 账户浮盈
        self.min_cross_profit = 0.0
        self.max_cross_profit = 0.0
        self.total_profit = 0.0
        self.total_calc_count = 0.0
        self.NoEnoughCashCount = 0.0
    
    def cash_increase(self, ammount: float) -> bool:
        if self.available_cash + ammount < 0:
            return False
        self.available_cash += ammount
        if self.available_cash < self.min_cash_remaining:
            self.min_cash_remaining = self.available_cash
        return True
        
    # 提交买单会占用资金
    def place_buy(self, symbol: str, share: float, price: float) -> bool:
        # print(f"Place Buy Order: {symbol} {share} @{price}, cash: {round(self.available_cash)}")
        # 提交买单会占用一部分可用资金，转入不可用资金
        cost = round(share * price, 4)
        if not self.cash_increase(-1 * cost):
            # print("Account Error: do not have enough cash.")
            self.NoEnoughCashCount += 1
            return False
        
        if symbol not in self.positions.keys():
            # 开仓
            self.positions[symbol] = Position(symbol, 0, 0)
            # self.positions[symbol] = Position(symbol, 100, 6900)
        
        # 将资金转入已占用资金中
        position = self.positions[symbol]
        position.pending_buy_cash += cost
        position.pending_buy_holding += share
        return True
            
    # 买单成交不影响资金，会增加持仓
    def buy_done(self, symbol: str, share: float, price: float) -> bool:
        # 必须是已经开仓的标的
        if symbol not in self.positions.keys():
            print(f" {symbol} not in {self.positions.keys()}")
            return False
        
        cost = round(share * price, 4)
        position = self.positions[symbol]
        # 仓位转入可用仓位
        position.available_holding += round(share, 4)
        position.pending_buy_holding -= round(share, 4)
        # 资金占用转入持仓成本
        position.total_cost += cost
        position.pending_buy_cash -= cost
        return True
    
    # 提交卖单会占用仓位，不影响资金
    def place_sell(self, symbol: str, share: float, price: float) -> bool:
        if symbol not in self.positions.keys():
            # self.positions[symbol] = Position(symbol, 100, 6900)
            self.positions[symbol] = Position(symbol, 0, 0)

        position = self.positions[symbol]
        # 允许裸卖空
        if position.available_holding < share:
            print("Account Error: do not have enough holding.")
            return False

        cost = round(share * price, 4)
        # 可用持仓减少，不可用仓位增加，待收入增加
        position.available_holding -= round(share, 4)
        # position.total_cost -= cost
        position.pending_sell_holding += round(share, 4)
        position.pending_sell_cash += cost
        return True
    
    # 卖单成交会增加资金
    def sell_done(self, symbol: str, share: float, price: float) -> bool:
        if symbol not in self.positions.keys():
            return False
        
        cost = round(share * price, 4)
        position = self.positions[symbol]
        # 占用仓位减少，可用现金额增加
        position.pending_sell_cash -= cost
        position.pending_sell_holding -= share
        
        position.total_cost -= cost
        return self.cash_increase(cost)

    # 取消买单，将资金恢复
    def cancel_buy(self, symbol: str, share: float, price: float) -> bool:
        if symbol not in self.positions.keys():
            return False
          
        cost = round(share*price, 4)
        position = self.positions[symbol]
        position.pending_buy_cash -= cost
        position.pending_buy_holding -= share
        return self.cash_increase(cost)
    
    # 取消卖单，将仓位恢复
    def cancel_sell(self, symbol: str, share: float, price: float) -> bool:
        if symbol not in self.positions.keys():
            return False
        
        cost = round(share*price, 4)
        position = self.positions[symbol]
        position.available_holding += share
        position.pending_sell_cash -= cost
        position.pending_sell_holding -= share
        return True