from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict


class Trader:
    """
    Round 4 Strategy — v3

    ── COUNTERPARTY ANALYSIS ───────────────────────────────────────────────────
    Mark 38  → loses ±8pts/fill on HYDROGEL, ±10pts on VEV_4000
    Mark 55  → loses ±2.5pts/fill on VELVETFRUIT
    Mark 67  → unconditional VELVETFRUIT buyer, never sells
    Mark 22  → writes OTM/near-ATM VEV calls profitably to Mark 01

    ── v2 results: +1,223. v3 improvements ─────────────────────────────────────
    VEV_5200 ADDED (biggest miss):
      Spot started 5295.5, ended 5253.5. VEV_5200 (strike 5200) started at mid ~120,
      expired at intrinsic 53.5. Selling 20 units at ~120 = +66.5/unit × 20 = +1,330
      left on table. Only sell when spot < strike+100 (= 5300) to limit delta risk.
      Time value at start ~24.5pts. Profitable if spot doesn't rise +45pts from strike.

    HYDROGEL SKEW 0.20→0.30 (second biggest fix):
      v2 accumulated +31 units long at peak mid (~10,061), then mid reverted -44pts
      → -872 HYDROGEL drawdown (inventory loss). SKEW=0.30 makes ask aggressive when
      long (at pos=+31: ask at mid-2.3, cheapest seller by 10pts → sells fast).

    VEV_5200 limit=20, VEV_5300 limit 15→20 (more premium collected).

    ── PRODUCTS ────────────────────────────────────────────────────────────────
    HYDROGEL_PACK       Dynamic FV MM  HS=7  SKEW=0.30  SIZE=25
    VELVETFRUIT_EXTRACT Dynamic FV MM  HS=2  SKEW=0.10  SIZE=15
    VEV_4000            Dynamic FV MM  HS=8  SKEW=0.20  SIZE=15  limit=40
    VEV_5200            Sell time-value  limit=20  (spot<5300 guard)
    VEV_5300            Sell OTM         limit=20
    VEV_5400            Sell OTM         limit=30
    VEV_5500            Sell OTM         limit=30
    """

    POSITION_LIMITS: Dict[str, int] = {
        "HYDROGEL_PACK":       80,
        "VELVETFRUIT_EXTRACT": 80,
        "VEV_4000":            40,
        "VEV_5200":            20,   # new: sell TV; spot 5253 < 5300 guard
        "VEV_5300":            20,   # limit 15→20
        "VEV_5400":            30,
        "VEV_5500":            30,
    }

    MM_PARAMS: Dict[str, tuple] = {
        "HYDROGEL_PACK":       (7,  0.30, 25),   # SKEW 0.20→0.30: prevent +31 overhang
        "VELVETFRUIT_EXTRACT": (2,  0.10, 15),
        "VEV_4000":            (8,  0.20, 15),
    }

    # Sell these products (go short) to collect time value.
    # Key: strike. Only sell when spot < strike + OTM_SELL_MAX_ITM
    OTM_SELL_STRIKES: Dict[str, int] = {
        "VEV_5200": 5200,   # ITM but with significant TV (~24pts); sell when spot<5300
        "VEV_5300": 5300,
        "VEV_5400": 5400,
        "VEV_5500": 5500,
    }
    OTM_SELL_MAX_ITM = 100   # don't sell if spot >= strike + 100 (delta risk too high)
    OTM_SELL_SIZE    = 5

    def __init__(self):
        self._spot: float | None = None

    # ──────────────────────────────────────────────────────────────────────────
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

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
                orders = self._sell_vev(od, pos, lim, product, strike)
            else:
                orders = []

            result[product] = orders

        return result, 0, "ROUND4_v3"

    # ──────────────────────────────────────────────────────────────────────────
    def _trade_mm(self, od: OrderDepth, pos: int, lim: int,
                  product: str, hs: int, skew: float, size: int) -> List[Order]:
        """Dynamic-FV market maker. FV = current mid. Inventory skew rebalances."""
        orders: List[Order] = []
        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        if best_bid is None or best_ask is None:
            return orders

        mid    = (best_bid + best_ask) / 2.0
        adj_fv = mid - skew * pos
        our_bid = round(adj_fv - hs)
        our_ask = round(adj_fv + hs)

        our_bid = max(our_bid, best_bid + 1)
        our_ask = min(our_ask, best_ask - 1)

        our_bid = min(our_bid, round(adj_fv))
        our_ask = max(our_ask, round(adj_fv))

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
    def _sell_vev(self, od: OrderDepth, pos: int, lim: int,
                  product: str, strike: int) -> List[Order]:
        """
        Sell VEV calls to collect time value. Works for OTM and near-ITM options.

        Guard: only sell when spot < strike + OTM_SELL_MAX_ITM (=100).
          - VEV_5200 (strike 5200): sell when spot < 5300  → spot=5253 ✓
          - VEV_5300 (strike 5300): sell when spot < 5400  → spot=5253 ✓
          - VEV_5400/5500: always OTM at current spot levels

        Post ask at best_bid+1 to be at front of queue.
        Positions go short (negative) — held to expiry for full premium.
        """
        if self._spot is None:
            return []
        if self._spot >= strike + self.OTM_SELL_MAX_ITM:
            return []   # delta risk too high if deeply ITM

        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        sell_cap = lim + pos   # = lim when pos=0, = 0 when pos=-lim
        if sell_cap <= 0 or best_bid is None or best_bid <= 0:
            return []

        sell_price = best_bid + 1
        if best_ask is not None and sell_price >= best_ask:
            sell_price = best_ask - 1
        if sell_price <= 0:
            return []

        qty = min(self.OTM_SELL_SIZE, sell_cap)
        return [Order(product, sell_price, -qty)] if qty > 0 else []
