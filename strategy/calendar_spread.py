# calendar_spread_with_combo_and_recover.py
import asyncio
import datetime
import logging
from typing import Dict, Any, Optional, Tuple, List

from ib_insync import IB, Stock, Option, LimitOrder, Trade, Ticker, Contract, ComboLeg, Order

from strategy.common import DailyProfitSummary

logger = logging.getLogger("CalendarSpreadCombo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class CalendarSpreadStrategy:
    def __init__(self, ib: IB, symbol: str = "TQQQ"):
        self.ib = ib
        self.symbol = symbol

        self.cfg: Dict[str, Any] = {
            "iv_diff_min": 8.0,
            "iv_diff_max": 13.0,
            "net_theta_min": 2.0,
            "expected_return_min": 0.0025,
            "min_days_between": 30,
            "near_max_days": 14,
            "near_delta_target": -0.30,
            "contract_size": 100,
            "check_interval": 60,
            "max_positions": 3,
            "loss_close_pct": 0.5,
            "order_price_slippage": 0.05,
            "recover_on_start": True,  # 新增：启动时是否恢复已存在持仓
        }

        self.active_spreads: Dict[int, Dict[str, Any]] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._lock = asyncio.Lock()

    def InitStrategy(self, **kwargs):
        self.cfg.update(kwargs or {})
        logger.info("InitStrategy params: %s", self.cfg)

    def DoStop(self, **kwargs):
        logger.info("Stopping strategy...")
        self._running = False
        for t in self._tasks:
            t.cancel()

    def Reconnect(self, **kwargs):
        try:
            if not self.ib.isConnected():
                host = kwargs.get("host", "127.0.0.1")
                port = kwargs.get("port", 7497)
                clientId = kwargs.get("clientId", 1)
                logger.info("Reconnecting to IB %s:%s clientId=%s", host, port, clientId)
                self.ib.connect(host, port, clientId=clientId)
        except Exception:
            logger.exception("Reconnect failed")

    def DailySummary(self, date_str: str) -> DailyProfitSummary:
        profits = 0.0
        position = len(self.active_spreads)
        cash = 0.0
        return DailyProfitSummary(strategy=self.__class__.__name__, profits=profits, position=position, cash=cash, date=date_str, params=self.cfg)

    async def update_order_status(self, order: Trade):
        logger.debug("Order update: %s", order)

    # ---------------- lifecycle ----------------
    async def run(self):
        if not self.ib.isConnected():
            raise RuntimeError("IB not connected")
        logger.info("Starting CalendarSpreadStrategy (combo) for %s", self.symbol)
        self._running = True

        # recover positions if configured
        if self.cfg.get("recover_on_start", True):
            try:
                await self._recover_positions()
            except Exception:
                logger.exception("Recover positions failed at startup")

        loop = asyncio.get_event_loop()
        scan_task = loop.create_task(self._periodic_scan())
        monitor_task = loop.create_task(self._monitor_positions())
        self._tasks += [scan_task, monitor_task]

        try:
            await asyncio.gather(scan_task, monitor_task)
        except asyncio.CancelledError:
            logger.info("Tasks cancelled")
        finally:
            self._running = False
            logger.info("Strategy stopped")

    # ---------------- periodic tasks ----------------
    async def _periodic_scan(self):
        while self._running:
            try:
                await self._scan_once()
            except Exception:
                logger.exception("Error in scan")
            await asyncio.sleep(self.cfg["check_interval"])

    async def _monitor_positions(self):
        while self._running:
            try:
                to_close = []
                async with self._lock:
                    for tid, meta in list(self.active_spreads.items()):
                        near_contract = meta["near_contract"]
                        far_contract = meta["far_contract"]
                        entry_credit = meta["entry_credit"]

                        near_expiry_date = datetime.datetime.strptime(near_contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
                        days_to_expiry = (near_expiry_date - datetime.date.today()).days

                        near_bid, near_ask = await self._get_bid_ask(near_contract)
                        far_bid, far_ask = await self._get_bid_ask(far_contract)
                        if near_ask is None or far_bid is None:
                            continue
                        current_net = (near_ask - far_bid) * self.cfg["contract_size"]
                        pnl = entry_credit - current_net
                        loss_frac = (-pnl / entry_credit) if (entry_credit > 0 and pnl < 0) else 0.0

                        if days_to_expiry <= 0:
                            logger.info("Spread %s expired -> close", tid)
                            to_close.append(tid)
                        elif days_to_expiry <= 2 and loss_frac >= self.cfg["loss_close_pct"]:
                            logger.info("Spread %s near expiry and loss %.2f >= %.2f -> close", tid, loss_frac, self.cfg["loss_close_pct"])
                            to_close.append(tid)

                for tid in to_close:
                    await self._close_spread(tid)
            except Exception:
                logger.exception("Error in monitor_positions")
            await asyncio.sleep(max(5.0, self.cfg["check_interval"] / 3.0))

    # ---------------- core scan ----------------
    async def _scan_once(self):
        spot = await self._get_spot_price()
        if spot is None:
            logger.warning("Spot price unavailable")
            return

        expiries = await self._get_option_expirations()
        if not expiries:
            logger.warning("No expiries")
            return

        today = datetime.date.today()
        near_expiry = self._select_near_expiry(expiries, today)
        if not near_expiry:
            logger.info("No near expiry")
            return

        far_expiry = None
        for e in sorted(expiries):
            if (e - near_expiry).days >= self.cfg["min_days_between"]:
                far_expiry = e
                break
        if not far_expiry:
            logger.info("No far expiry with spacing")
            return

        near_contract, near_greeks = await self._select_put_contract(near_expiry, spot)
        far_contract, far_greeks = await self._select_put_contract(far_expiry, spot, prefer_strike=(near_contract.strike if near_contract else None))

        if not near_contract or not far_contract:
            logger.info("No candidate contracts")
            return

        if not (near_greeks.get("impliedVol") and far_greeks.get("impliedVol") and near_greeks.get("theta") is not None and far_greeks.get("theta") is not None):
            logger.info("Missing greeks/IV")
            return

        near_iv = near_greeks["impliedVol"] * 100.0
        far_iv = far_greeks["impliedVol"] * 100.0
        iv_diff = far_iv - near_iv
        net_theta = -near_greeks["theta"] + far_greeks["theta"]

        near_bid, near_ask = await self._get_bid_ask(near_contract)
        far_bid, far_ask = await self._get_bid_ask(far_contract)
        if None in (near_bid, near_ask, far_bid, far_ask):
            logger.info("Missing bid/ask")
            return

        net_credit_per_share = near_bid - far_ask
        net_credit_per_contract = net_credit_per_share * self.cfg["contract_size"]
        collateral = max(near_contract.strike * self.cfg["contract_size"], 1.0)
        expected_return = net_credit_per_contract / collateral

        logger.info("cand: near=%s far=%s spot=%.2f iv_diff=%.2f net_theta=%.2f exp_ret=%.4f credit=%.2f",
                    near_contract, far_contract, spot, iv_diff, net_theta, expected_return, net_credit_per_contract)

        async with self._lock:
            if len(self.active_spreads) >= self.cfg["max_positions"]:
                logger.info("Max positions reached")
                return

            if (self.cfg["iv_diff_min"] <= iv_diff <= self.cfg["iv_diff_max"]
                and net_theta >= self.cfg["net_theta_min"]
                and expected_return >= self.cfg["expected_return_min"]
                and net_credit_per_share > 0):
                qty = 1
                await self._enter_calendar_combo(near_contract, far_contract, qty, net_credit_per_contract)
            else:
                logger.debug("Conditions not met")

    # ---------------- helpers: market data ----------------
    async def _get_spot_price(self) -> Optional[float]:
        try:
            contract = Stock(self.symbol, "SMART", "USD")
            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(0.5)
            return ticker.last or ticker.close or getattr(ticker, "marketPrice", None)()
        except Exception:
            logger.exception("Failed to get spot")
            return None

    async def _get_option_expirations(self) -> List[datetime.date]:
        try:
            params = self.ib.reqSecDefOptParams(self.symbol, "", "STK", 0)
            expiries = []
            for p in params:
                for e in p.expirations:
                    try:
                        expiries.append(datetime.datetime.strptime(e, "%Y%m%d").date())
                    except Exception:
                        continue
                if expiries:
                    break
            return sorted(set(expiries))
        except Exception:
            logger.exception("Failed to get expirations")
            return []

    def _select_near_expiry(self, expiries: List[datetime.date], today: datetime.date) -> Optional[datetime.date]:
        candidates = [e for e in expiries if 0 <= (e - today).days <= self.cfg["near_max_days"]]
        fridays = [e for e in candidates if e.weekday() == 4]
        if fridays:
            return min(fridays)
        if candidates:
            return min(candidates)
        return None

    async def _select_put_contract(self, expiry: datetime.date, spot: float, prefer_strike: Optional[float] = None) -> Tuple[Optional[Option], Dict[str, Any]]:
        try:
            params = self.ib.reqSecDefOptParams(self.symbol, "", "STK", 0)
            strikes = None
            for p in params:
                strikes = sorted(set(p.strikes))
                if strikes:
                    break
            if not strikes:
                return None, {}

            strike = None
            if prefer_strike is not None and prefer_strike in strikes:
                strike = prefer_strike
            else:
                lesser = [s for s in strikes if s <= spot]
                strike = max(lesser) if lesser else min(strikes)

            contract = Option(self.symbol, expiry.strftime("%Y%m%d"), strike, "PUT", "SMART")
            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(0.8)
            greeks = {}
            mg = getattr(ticker, "modelGreeks", None)
            if mg:
                greeks["impliedVol"] = mg.impliedVol
                greeks["theta"] = mg.theta
                greeks["delta"] = mg.delta
            else:
                greeks["impliedVol"] = getattr(ticker, "impliedVol", None)
                greeks["theta"] = getattr(ticker, "theta", None)
                greeks["delta"] = getattr(ticker, "delta", None)
            return contract, greeks
        except Exception:
            logger.exception("select_put_contract failed")
            return None, {}

    async def _get_bid_ask(self, contract) -> Tuple[Optional[float], Optional[float]]:
        try:
            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(0.4)
            bid = getattr(ticker, "bid", None)
            ask = getattr(ticker, "ask", None)
            if bid is None and ask is not None:
                bid = ask
            if ask is None and bid is not None:
                ask = bid
            return bid, ask
        except Exception:
            logger.exception("get_bid_ask failed")
            return None, None

    # ---------------- new: enter as combo ----------------
    async def _enter_calendar_combo(self, near_contract, far_contract, qty: int, entry_credit: float):
        """
        Use BAG combo order with ComboLegs to place combo: SELL near PUT + BUY far PUT
        This improves atomicity compared to separate single-leg orders.
        """
        try:
            # build combo contract (BAG)
            combo = Contract()
            combo.symbol = self.symbol
            combo.secType = "BAG"
            combo.currency = "USD"
            combo.exchange = "SMART"

            # legs: use conId to reference actual option contracts
            # ensure we have conId (reqContractDetails might populate)
            if not getattr(near_contract, "conId", None):
                self.ib.qualifyContracts(near_contract)
            if not getattr(far_contract, "conId", None):
                self.ib.qualifyContracts(far_contract)

            leg_near = ComboLeg()
            leg_near.conId = near_contract.conId
            leg_near.ratio = qty
            leg_near.action = "SELL"
            leg_near.exchange = "SMART"

            leg_far = ComboLeg()
            leg_far.conId = far_contract.conId
            leg_far.ratio = qty
            leg_far.action = "BUY"
            leg_far.exchange = "SMART"

            combo.comboLegs = [leg_near, leg_far]

            # price: net credit per share. For combo order, price unit typically per contract.
            # set limit price to net_credit_per_share or net_credit_per_contract? IB expects price per contract*multiplier often.
            net_credit_per_share = round((await self._get_bid_ask(near_contract))[0] - (await self._get_bid_ask(far_contract))[1], 4)
            if net_credit_per_share is None:
                logger.warning("Cannot compute net credit for combo")
                return

            # combination limit price is typically per contract; we set as net_credit_per_share * contract_size
            limit_price = round(net_credit_per_share * self.cfg["contract_size"], 2)

            order = Order()
            order.action = "SELL"  # overall action for combo (ignored if legs have actions)
            order.orderType = "LMT"
            order.lmtPrice = limit_price
            order.totalQuantity = qty
            order.transmit = True

            trade = self.ib.placeOrder(combo, order)
            tid = id(trade)
            async with self._lock:
                self.active_spreads[tid] = {
                    "near_contract": near_contract,
                    "far_contract": far_contract,
                    "combo_contract": combo,
                    "combo_trade": trade,
                    "entry_credit": entry_credit,
                    "entry_time": datetime.datetime.utcnow()
                }
            logger.info("Placed combo spread id=%s limit=%.2f entry_credit=%.2f", tid, limit_price, entry_credit)

            # attach update handlers
            trade.updateEvent += lambda tr: asyncio.create_task(self.update_order_status(tr))

        except Exception:
            logger.exception("enter_calendar_combo failed")

    # ---------------- close ----------------
    async def _close_spread(self, tid: int):
        meta = self.active_spreads.get(tid)
        if not meta:
            return
        near_contract = meta["near_contract"]
        far_contract = meta["far_contract"]
        try:
            # close via combo opposite (buy near, sell far) -> create BAG with legs reversed and submit
            combo = Contract()
            combo.symbol = self.symbol
            combo.secType = "BAG"
            combo.currency = "USD"
            combo.exchange = "SMART"

            if not getattr(near_contract, "conId", None):
                self.ib.qualifyContracts(near_contract)
            if not getattr(far_contract, "conId", None):
                self.ib.qualifyContracts(far_contract)

            leg_near = ComboLeg()
            leg_near.conId = near_contract.conId
            leg_near.ratio = meta.get("combo_trade").order.totalQuantity if meta.get("combo_trade") else 1
            leg_near.action = "BUY"
            leg_near.exchange = "SMART"

            leg_far = ComboLeg()
            leg_far.conId = far_contract.conId
            leg_far.ratio = leg_near.ratio
            leg_far.action = "SELL"
            leg_far.exchange = "SMART"

            combo.comboLegs = [leg_near, leg_far]

            # pricing: use current mid of near ask and far bid
            near_bid, near_ask = await self._get_bid_ask(near_contract)
            far_bid, far_ask = await self._get_bid_ask(far_contract)
            if near_ask is None or far_bid is None:
                logger.warning("Cannot price combo close for %s", tid)
                return

            limit_price = round((near_ask - far_bid) * self.cfg["contract_size"], 2)
            order = Order()
            order.action = "BUY"
            order.orderType = "LMT"
            order.lmtPrice = limit_price
            order.totalQuantity = leg_near.ratio
            order.transmit = True

            trade = self.ib.placeOrder(combo, order)
            logger.info("Placed combo close for %s limit=%.2f", tid, limit_price)

            async with self._lock:
                if tid in self.active_spreads:
                    del self.active_spreads[tid]
        except Exception:
            logger.exception("close_spread failed")

    # ---------------- recover existing positions ----------------
    async def _recover_positions(self):
        """
        从 IB positions() 恢复现有期权组合仓位到 active_spreads
        策略：找到同一 strike 的近月 short put 与远月 long put 的数量对，
        并把配对的组合恢复为一个 active_spread 条目。
        注意：此方法做启发式配对，复杂持仓（多手/多价/部分成交）需更精细逻辑。
        """
        logger.info("Recovering positions from IB...")
        # 查询所有持仓
        try:
            positions = self.ib.positions()
            # filter option positions on symbol
            opt_positions = [p for p in positions if getattr(p.contract, "secType", "") == "OPT" and getattr(p.contract, "symbol", "") == self.symbol]
            # group by (strike, right)
            grouped: Dict[Tuple[float, str], List] = {}
            for p in opt_positions:
                key = (p.contract.strike, p.contract.right)
                grouped.setdefault(key, []).append(p)

            # We look for pairs: same strike, right='P', one negative qty near expiry, one positive qty far expiry
            candidates = []
            # create list of option entries with expiry date for matching
            opts = []
            for p in opt_positions:
                try:
                    expiry = datetime.datetime.strptime(p.contract.lastTradeDateOrContractMonth[:8], "%Y%m%d").date()
                except Exception:
                    # try contractMonth format
                    try:
                        expiry = datetime.datetime.strptime(p.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
                    except Exception:
                        continue
                opts.append((p, expiry))

            # now attempt pair matching: for each short put, find long put same strike with later expiry (30-40 days)
            for p_short, exp_short in [(o, ex) for o, ex in opts if o.position < 0 and o.contract.right == "P"]:
                for p_long, exp_long in [(o, ex) for o, ex in opts if o.position > 0 and o.contract.right == "P"]:
                    if p_short.contract.strike == p_long.contract.strike and (exp_long - exp_short).days >= self.cfg["min_days_between"]:
                        # found candidate pair: construct active_spread entry
                        tid = hash((p_short.account, p_short.contract.conId, p_long.contract.conId))
                        # approximate entry_credit unknown; set to 0 (could be improved by historical trade lookup)
                        entry_credit = 0.0
                        self.active_spreads[tid] = {
                            "near_contract": p_short.contract,
                            "far_contract": p_long.contract,
                            "near_pos": p_short.position,
                            "far_pos": p_long.position,
                            "entry_credit": entry_credit,
                            "entry_time": datetime.datetime.utcnow(),
                            "recovered": True,
                        }
                        logger.info("Recovered spread tid=%s near_exp=%s far_exp=%s strike=%.2f qty_near=%s qty_far=%s",
                                    tid, exp_short, exp_long, p_short.contract.strike, p_short.position, p_long.position)
                        # avoid duplicate pairing: break after first match
                        break

            logger.info("Recovery complete, found %s spreads", len(self.active_spreads))
        except Exception:
            logger.exception("recover_positions failed")

    # ---------------- misc ----------------
    # (reuse earlier helper implementations: _get_bid_ask, _select_put_contract, _get_option_expirations, _get_spot_price)
    async def _get_bid_ask(self, contract) -> Tuple[Optional[float], Optional[float]]:
        try:
            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(0.4)
            bid = getattr(ticker, "bid", None)
            ask = getattr(ticker, "ask", None)
            if bid is None and ask is not None:
                bid = ask
            if ask is None and bid is not None:
                ask = bid
            return bid, ask
        except Exception:
            logger.exception("get_bid_ask failed")
            return None, None

    async def _get_spot_price(self) -> Optional[float]:
        try:
            contract = Stock(self.symbol, "SMART", "USD")
            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(0.5)
            return ticker.last or ticker.close or getattr(ticker, "marketPrice", lambda: None)()
        except Exception:
            logger.exception("get_spot failed")
            return None

    async def _get_option_expirations(self) -> List[datetime.date]:
        try:
            params = self.ib.reqSecDefOptParams(self.symbol, "", "STK", 0)
            expiries = []
            for p in params:
                for e in p.expirations:
                    try:
                        expiries.append(datetime.datetime.strptime(e, "%Y%m%d").date())
                    except Exception:
                        continue
                if expiries:
                    break
            return sorted(set(expiries))
        except Exception:
            logger.exception("_get_option_expirations failed")
            return []
