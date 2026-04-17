from datamodel import OrderDepth, TradingState, Order
from typing import List


class Trader:
    """
    Round 1 Strategy — v4

    ASH_COATED_OSMIUM  (fair value ~10000, mean-reverting, spread ~16 pts)
    -----------------------------------------------------------------------
    Core strategy: inventory-skewed market making with hard FV±1 bounds.

    Change from v3 → v4:
      • Revert adj_fv caps back to hard FV-1 / FV+1 bounds.
        When long, adj_fv cap was lowering our ask to ~9999 (below FV),
        cutting per-fill profit from ~5 pts to ~3 pts. Combined with
        the full-capacity posting it cost ~300 pts of ASH PnL.
      • Revert full-capacity posting back to SIZE=30.
        Large orders amplified the cheap-ask problem.
      • KEEP TAKE_EDGE=0: take any ask strictly below FV=10000
        (catches 4 extra mispriced asks at 9999 per day — free profit).

    Surviving fixes from v2 (vs original 182314.py):
      • Push-to-front: bid at max(target, best_bid+1),
                       ask at min(target, best_ask-1)
      • SIZE=30 (was 15)
      • SKEW=0.07 (was 0.10)
      • Position limit 80 (was 20)

    INTARIAN_PEPPER_ROOT  (trends +0.1/100ts = +1000/day)
    -----------------------------------------------------------------
    Unchanged: buy to limit immediately, never sell.
    """

    POSITION_LIMITS = {
        "INTARIAN_PEPPER_ROOT": 80,
        "ASH_COATED_OSMIUM": 80,
    }

    # ── ASH parameters ──────────────────────────────────────────────────
    ASH_FAIR_VALUE  = 10000
    ASH_HALF_SPREAD = 4      # offset from adj_fv for passive quotes
    ASH_TAKE_EDGE   = 0      # take any ask strictly < FV (was 1 in v2, catching < FV-1)
    ASH_SKEW        = 0.07   # adj_fv = FV - SKEW*pos
    ASH_MAKE_SIZE   = 30     # units per passive quote

    # ── PEPPER parameters ────────────────────────────────────────────────
    PEPPER_TREND_PER_STEP = 0.1

    def __init__(self):
        self._pepper_anchor_fv: float | None = None
        self._pepper_anchor_ts: int   | None = None

    # ------------------------------------------------------------------
    def run(self, state: TradingState):
        result = {}
        for product, order_depth in state.order_depths.items():
            pos = state.position.get(product, 0)
            lim = self.POSITION_LIMITS.get(product, 0)
            if product == "ASH_COATED_OSMIUM":
                orders = self._trade_ash(order_depth, pos, lim)
            elif product == "INTARIAN_PEPPER_ROOT":
                orders = self._trade_pepper(order_depth, pos, lim, state)
            else:
                orders = []
            result[product] = orders
        return result, 1, "ROUND1"

    # ------------------------------------------------------------------
    # ASH_COATED_OSMIUM — inventory-skewed market maker (v4)
    # ------------------------------------------------------------------
    def _trade_ash(self, od: OrderDepth, pos: int, lim: int) -> List[Order]:
        orders: List[Order] = []
        FV   = self.ASH_FAIR_VALUE
        HS   = self.ASH_HALF_SPREAD
        EDGE = self.ASH_TAKE_EDGE
        SIZE = self.ASH_MAKE_SIZE

        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        adj_fv  = FV - self.ASH_SKEW * pos
        our_bid = round(adj_fv - HS)
        our_ask = round(adj_fv + HS)

        # ── TAKE: buy asks < FV, sell bids > FV ─────────────────────────
        for ask in sorted(od.sell_orders.keys()):
            if ask >= FV - EDGE:        # EDGE=0 → take anything below 10000
                break
            qty = min(abs(od.sell_orders[ask]), lim - pos)
            if qty <= 0:
                break
            orders.append(Order("ASH_COATED_OSMIUM", ask, qty))
            pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= FV + EDGE:
                break
            qty = min(od.buy_orders[bid], lim + pos)
            if qty <= 0:
                break
            orders.append(Order("ASH_COATED_OSMIUM", bid, -qty))
            pos -= qty

        # ── MAKE: push to front of queue ────────────────────────────────
        if best_bid is not None:
            our_bid = max(our_bid, best_bid + 1)
        if best_ask is not None:
            our_ask = min(our_ask, best_ask - 1)

        # Hard FV±1 bounds — never buy at/above FV, never sell at/below FV.
        # Keeps per-fill profit healthy regardless of inventory position.
        our_bid = min(our_bid, FV - 1)    # reverted from adj_fv-1 (v3 mistake)
        our_ask = max(our_ask, FV + 1)    # reverted from adj_fv+1 (v3 mistake)

        # Safety: don't cross the spread
        if best_ask is not None and our_bid >= best_ask:
            our_bid = best_ask - 1
        if best_bid is not None and our_ask <= best_bid:
            our_ask = best_bid + 1

        buy_cap  = lim - pos
        sell_cap = lim + pos

        if buy_cap > 0:
            orders.append(Order("ASH_COATED_OSMIUM", our_bid, min(SIZE, buy_cap)))

        if sell_cap > 0:
            orders.append(Order("ASH_COATED_OSMIUM", our_ask, -min(SIZE, sell_cap)))

        return orders

    # ------------------------------------------------------------------
    # INTARIAN_PEPPER_ROOT — buy-and-hold trend follower (unchanged)
    # ------------------------------------------------------------------
    def _trade_pepper(self, od: OrderDepth, pos: int, lim: int,
                      state: TradingState) -> List[Order]:
        orders: List[Order] = []

        best_ask = min(od.sell_orders) if od.sell_orders else None
        best_bid = max(od.buy_orders)  if od.buy_orders  else None

        mid = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
        elif best_bid is not None:
            mid = float(best_bid)
        elif best_ask is not None:
            mid = float(best_ask)

        if mid is not None and self._pepper_anchor_fv is None:
            self._pepper_anchor_fv = mid
            self._pepper_anchor_ts = state.timestamp

        # Buy every ask up to position limit — price always trends up
        for ask_price in sorted(od.sell_orders.keys()):
            if pos >= lim:
                break
            qty = min(abs(od.sell_orders[ask_price]), lim - pos)
            if qty > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", ask_price, qty))
                pos += qty

        # Passive bid at best_bid+1 to catch residual supply
        if pos < lim and best_bid is not None and best_ask is not None:
            our_bid = best_bid + 1
            if our_bid >= best_ask:
                our_bid = best_ask - 1
            qty = lim - pos
            if qty > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", our_bid, qty))

        return orders
