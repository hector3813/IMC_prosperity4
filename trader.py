from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict


class Trader:
    """
    Round 4 Strategy — v2

    Same 12 products as Round 3. New mechanic: counterparty IDs (Mark bots).

    ── COUNTERPARTY ANALYSIS ───────────────────────────────────────────────────
    Mark 38  → loses ~8 pts/fill on HYDROGEL, ~10 pts/fill on VEV_4000
               (buys above mid, sells below mid consistently)
               → BE the market maker against Mark 38: quote ±7 / ±9 to fill first

    Mark 55  → loses ~2.5 pts/fill on VELVETFRUIT (always pays spread)
               → quote ±2 on VELVETFRUIT, let Mark 55 fill us

    Mark 67  → unconditional VELVETFRUIT buyer, never sells
               → we sell VELVETFRUIT to Mark 67 at ask repeatedly

    Mark 22  → profitably writes OTM VEV calls (VEV_5400/5500) to Mark 01
               → copy: sell VEV_5400/VEV_5500, collect time value
               → spot max in Round 4 data was 5300; both strikes require 5400/5500

    ── PRODUCTS ────────────────────────────────────────────────────────────────
    HYDROGEL_PACK       Dynamic FV MM  HS=7   SKEW=0.15  (Mark 38 = reliable flow)
    VELVETFRUIT_EXTRACT Dynamic FV MM  HS=2   SKEW=0.05  (Mark 55+67 = fill sources)
    VEV_4000            Dynamic FV MM  HS=9   SKEW=0.20  limit=40 (Mark 38 loses ±10)
    VEV_5300            Sell-only OTM        limit=15   (spot 5253, strike 5300 = 47 OTM)
    VEV_5400            Sell-only OTM        limit=30   (spot needs +147 to go ITM)
    VEV_5500            Sell-only OTM        limit=30   (spot needs +247, very safe)

    v1 results: +820 total. v2 improvements:
      VELVETFRUIT SKEW 0.05→0.10: v1 accumulated 31 units long into -42pt downtrend
        = -382 XIRECS inventory loss. Stronger skew prevents drift.
      HYDROGEL   SKEW 0.15→0.20: v1 ended +18 units long, ~-280 unrealized PnL.
      VEV_4000   HS 9→8, SIZE 10→15: only 9 fills in v1 despite good ±16pt edge.
      VEV_5300   added: sold at ~46pts in Round 4 data; 15 units × 46 = +690 if OTM.
      VEV_5400/5500 limits 20→30: sold out in first 300 timestamps at peak prices.
    """

    POSITION_LIMITS: Dict[str, int] = {
        "HYDROGEL_PACK":       80,
        "VELVETFRUIT_EXTRACT": 80,
        "VEV_4000":            40,   # cap delta exposure relative to VELVETFRUIT
        "VEV_5300":            15,   # sell-only OTM; spot at 5253, strike 5300
        "VEV_5400":            30,   # increased from 20 (sold out instantly in v1)
        "VEV_5500":            30,   # increased from 20
    }

    # (half_spread, inventory_skew, order_size) per MM product
    MM_PARAMS: Dict[str, tuple] = {
        "HYDROGEL_PACK":       (7,  0.20, 25),   # SKEW 0.15→0.20: tighter inventory
        "VELVETFRUIT_EXTRACT": (2,  0.10, 15),   # SKEW 0.05→0.10: prevent long drift
        "VEV_4000":            (8,  0.20, 15),   # HS 9→8, SIZE 10→15: more fills
    }

    OTM_SELL_STRIKES: Dict[str, int] = {
        "VEV_5300": 5300,   # new: spot at 5253, OTM by 47 pts, all time-value
        "VEV_5400": 5400,
        "VEV_5500": 5500,
    }
    OTM_SELL_SIZE = 5

    def __init__(self):
        self._spot: float | None = None

    # ──────────────────────────────────────────────────────────────────────────
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        # Track live VELVETFRUIT mid-price (needed for OTM VEV filtering)
        vf_od = state.order_depths.get("VELVETFRUIT_EXTRACT")
        if vf_od:
            bb = max(vf_od.buy_orders)  if vf_od.buy_orders  else None
            ba = min(vf_od.sell_orders) if vf_od.sell_orders else None
            if   bb is not None and ba is not None:
                self._spot = (bb + ba) / 2.0
            elif bb is not None:
                self._spot = float(bb)
            elif ba is not None:
                self._spot = float(ba)

        for product, od in state.order_depths.items():
            pos = state.position.get(product, 0)
            lim = self.POSITION_LIMITS.get(product, 0)

            if product in self.MM_PARAMS:
                hs, skew, size = self.MM_PARAMS[product]
                orders = self._trade_mm(od, pos, lim, product, hs, skew, size)
            elif product in self.OTM_SELL_STRIKES and lim > 0:
                strike = self.OTM_SELL_STRIKES[product]
                orders = self._sell_otm_vev(od, pos, lim, product, strike)
            else:
                orders = []

            result[product] = orders

        return result, 0, "ROUND4_v2"

    # ──────────────────────────────────────────────────────────────────────────
    def _trade_mm(self, od: OrderDepth, pos: int, lim: int,
                  product: str, hs: int, skew: float, size: int) -> List[Order]:
        """
        Dynamic-FV market maker.
        FV = current mid — follows market wherever it goes, no directional bet.
        Inventory skew (adj_fv = mid - skew×pos) shades quotes to rebalance.
        """
        orders: List[Order] = []
        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        if best_bid is None or best_ask is None:
            return orders

        mid    = (best_bid + best_ask) / 2.0
        adj_fv = mid - skew * pos
        our_bid = round(adj_fv - hs)
        our_ask = round(adj_fv + hs)

        # Push to front of queue
        our_bid = max(our_bid, best_bid + 1)
        our_ask = min(our_ask, best_ask - 1)

        # Inventory-aware caps
        our_bid = min(our_bid, round(adj_fv))
        our_ask = max(our_ask, round(adj_fv))

        # Safety checks
        if our_bid >= best_ask:
            our_bid = best_ask - 1
        if our_ask <= best_bid:
            our_ask = best_bid + 1
        if our_ask <= our_bid:
            our_ask = our_bid + 1

        buy_cap  = lim - pos
        sell_cap = lim + pos
        if buy_cap > 0:
            orders.append(Order(product, our_bid,  min(size, buy_cap)))
        if sell_cap > 0:
            orders.append(Order(product, our_ask, -min(size, sell_cap)))

        return orders

    # ──────────────────────────────────────────────────────────────────────────
    def _sell_otm_vev(self, od: OrderDepth, pos: int, lim: int,
                      product: str, strike: int) -> List[Order]:
        """
        Sell OTM call options at market to collect time value.
        Mark 22 does this profitably vs Mark 01.

        Only sell when spot < strike (genuinely OTM).
        Post ask at best_bid+1 to be at front of ask queue.
        Never buy (hold short position until expiry or close cheaply).
        """
        if self._spot is None or self._spot >= strike:
            return []   # don't sell if we're ITM — risk too high

        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        sell_cap = lim + pos   # = lim when pos=0, = 0 when pos=-lim
        if sell_cap <= 0 or best_bid is None or best_bid <= 0:
            return []

        # Post at best_bid+1 (front of ask queue, capture the spread)
        sell_price = best_bid + 1
        if best_ask is not None and sell_price >= best_ask:
            sell_price = best_ask - 1
        if sell_price <= 0:
            return []

        qty = min(self.OTM_SELL_SIZE, sell_cap)
        return [Order(product, sell_price, -qty)] if qty > 0 else []
