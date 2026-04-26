from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict


class Trader:
    """
    Round 3 Strategy — v3

    v2 failure: HYDROGEL static FV=10,000 caused two catastrophes:
      1. TAKE-SELL at open: market bids were 10,003–10,009 (above FV=10,000)
         → algo immediately sold 80 units in the first 200 timesteps
      2. As market drifted to 9,935, our bids sat at 9,993 (FV-HS=10,000-7)
         → 48 pts above market → accumulated +80 long into falling market
         → -2,234 unrealized loss at end (80 units × 28 pts below FV)
    Data: HYDROGEL drifted 10,020 → 9,935 (-85 pts) in this simulation.
    Static FV=10,000 assumption is wrong — product does NOT mean-revert in
    a single simulation run.

    v3 fix: DYNAMIC FV = current mid for BOTH products.
    ────────────────────────────────────────────────────────────────────────
    Uses a single shared MM function. No static FV, no TAKE.
    We quote bid at adj_fv-HS and ask at adj_fv+HS where adj_fv = mid - SKEW×pos.
    Push-to-front keeps us at best_bid+1 / best_ask-1.
    Inventory skew automatically widens quotes when long/short to rebalance.

    HYDROGEL_PACK:  HS=7  (earn ~7 pts/fill inside 15-16 pt market spread)
    VELVETFRUIT:    HS=2  (earn ~2 pts/fill inside 4-6 pt market spread)
    """

    POSITION_LIMITS: Dict[str, int] = {
        "HYDROGEL_PACK":       80,
        "VELVETFRUIT_EXTRACT": 80,
    }

    # Shared MM parameters per product
    # (HS, SKEW, SIZE)
    MM_PARAMS: Dict[str, tuple] = {
        "HYDROGEL_PACK":       (7,  0.15, 25),
        "VELVETFRUIT_EXTRACT": (2,  0.05, 15),
    }

    # ──────────────────────────────────────────────────────────────────────────
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}
        for product, od in state.order_depths.items():
            pos = state.position.get(product, 0)
            lim = self.POSITION_LIMITS.get(product, 0)
            if product in self.MM_PARAMS:
                hs, skew, size = self.MM_PARAMS[product]
                orders = self._trade_mm(od, pos, lim, product, hs, skew, size)
            else:
                orders = []   # skip all VEVs
            result[product] = orders
        return result, 0, "ROUND3_v3"

    # ──────────────────────────────────────────────────────────────────────────
    def _trade_mm(self, od: OrderDepth, pos: int, lim: int,
                  product: str, hs: int, skew: float, size: int) -> List[Order]:
        """
        Dynamic-FV market maker.

        FV = current mid-price (no static reference — follows market wherever it goes).
        adj_fv = mid - skew × pos  (inventory skew shades quotes to rebalance position)

        Quote structure:
          bid = adj_fv - hs  →  pushed up to best_bid+1 (queue priority)
                             →  capped at adj_fv (never bid above FV estimate)
          ask = adj_fv + hs  →  pushed down to best_ask-1 (queue priority)
                             →  floored at adj_fv (never ask below FV estimate)

        Effect of skew when LONG (pos > 0):
          adj_fv < mid → our bid is pushed below market → fewer buys
                       → our ask is lower → more aggressive selling → rebalances

        Effect of skew when SHORT (pos < 0):
          adj_fv > mid → our bid is higher → more aggressive buying → rebalances
                       → our ask is pushed up → fewer sells
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

        # ── Push to front of queue ───────────────────────────────────────────
        our_bid = max(our_bid, best_bid + 1)
        our_ask = min(our_ask, best_ask - 1)

        # ── Inventory-aware caps (never bid above adj_fv / ask below adj_fv) ─
        our_bid = min(our_bid, round(adj_fv))
        our_ask = max(our_ask, round(adj_fv))

        # ── Safety: no crossing ──────────────────────────────────────────────
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
