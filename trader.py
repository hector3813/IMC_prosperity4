from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict


class Trader:
    """
    Round 3 Strategy — v2

    v1 FAILURE: Bought VELVETFRUIT + 5 VEV products to limit (long 80 each).
    When spot dropped ~35 pts mid-simulation, all 6 positions lost simultaneously:
      6 products × 80 units × 35 pts = ~16,800 loss → observed -13,500 drawdown.

    v2 FIX: Eliminate every directional bet. Market-make both liquid products.
    ────────────────────────────────────────────────────────────────────────────
    HYDROGEL_PACK (mean-reverting ~10,000, spread 15–16)
      FV = 10,000 (static, confirmed mean-reversion in all 3 training days)
      HS = 7 → quote inside the 15-16 pt spread, earn 7 pts per fill
      TAKE = sweep any ask strictly below 10,000 (mispriced order capture)

    VELVETFRUIT_EXTRACT (tight 5-pt spread, no clear FV anchor)
      FV = current midpoint (dynamic — no strong prior on level)
      HS = 2 → quote bid+1 / ask-1, earn 2 pts per fill (inside the 5 pt spread)
      SKEW = 0.05 → mild inventory management to stay balanced

    ALL VEVs: Skip.
      Complex options with unknown future dynamics; v1 proved directional
      exposure kills us. Return in v3 only if MM-able without position risk.
    """

    POSITION_LIMITS: Dict[str, int] = {
        "HYDROGEL_PACK":       80,
        "VELVETFRUIT_EXTRACT": 80,
    }

    # ── HYDROGEL parameters ──────────────────────────────────────────────────
    HYDROGEL_FV        = 10_000
    HYDROGEL_HS        = 7       # earn 7 pts/fill inside 15-16 pt spread
    HYDROGEL_SKEW      = 0.10    # adj_fv = FV − SKEW × pos
    HYDROGEL_TAKE_EDGE = 0       # take all asks strictly below FV
    HYDROGEL_SIZE      = 25

    # ── VELVETFRUIT parameters ───────────────────────────────────────────────
    VELVETFRUIT_HS     = 2       # earn 2 pts/fill inside 5 pt spread
    VELVETFRUIT_SKEW   = 0.05
    VELVETFRUIT_SIZE   = 15

    # ──────────────────────────────────────────────────────────────────────────
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}
        for product, od in state.order_depths.items():
            pos = state.position.get(product, 0)
            lim = self.POSITION_LIMITS.get(product, 0)
            if product == "HYDROGEL_PACK":
                orders = self._trade_hydrogel(od, pos, lim)
            elif product == "VELVETFRUIT_EXTRACT":
                orders = self._trade_velvetfruit(od, pos, lim)
            else:
                orders = []   # skip all VEVs
            result[product] = orders
        return result, 0, "ROUND3_v2"

    # ──────────────────────────────────────────────────────────────────────────
    # HYDROGEL_PACK — inventory-skewed market maker around FV = 10,000
    # ──────────────────────────────────────────────────────────────────────────
    def _trade_hydrogel(self, od: OrderDepth, pos: int, lim: int) -> List[Order]:
        orders: List[Order] = []
        FV   = self.HYDROGEL_FV
        HS   = self.HYDROGEL_HS
        SIZE = self.HYDROGEL_SIZE

        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        adj_fv  = FV - self.HYDROGEL_SKEW * pos
        our_bid = round(adj_fv - HS)
        our_ask = round(adj_fv + HS)

        # ── TAKE: sweep mispriced orders ─────────────────────────────────────
        for ask in sorted(od.sell_orders.keys()):
            if ask >= FV - self.HYDROGEL_TAKE_EDGE:
                break
            qty = min(abs(od.sell_orders[ask]), lim - pos)
            if qty <= 0:
                break
            orders.append(Order("HYDROGEL_PACK", ask, qty))
            pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= FV + self.HYDROGEL_TAKE_EDGE:
                break
            qty = min(od.buy_orders[bid], lim + pos)
            if qty <= 0:
                break
            orders.append(Order("HYDROGEL_PACK", bid, -qty))
            pos -= qty

        # ── MAKE: push to front of queue ─────────────────────────────────────
        if best_bid is not None:
            our_bid = max(our_bid, best_bid + 1)
        if best_ask is not None:
            our_ask = min(our_ask, best_ask - 1)

        our_bid = min(our_bid, round(adj_fv))
        our_ask = max(our_ask, round(adj_fv))
        our_bid = min(our_bid, FV)
        our_ask = max(our_ask, FV)

        if best_ask is not None and our_bid >= best_ask:
            our_bid = best_ask - 1
        if best_bid is not None and our_ask <= best_bid:
            our_ask = best_bid + 1
        if our_ask <= our_bid:
            our_ask = our_bid + 1

        buy_cap  = lim - pos
        sell_cap = lim + pos
        if buy_cap > 0:
            orders.append(Order("HYDROGEL_PACK", our_bid,  min(SIZE, buy_cap)))
        if sell_cap > 0:
            orders.append(Order("HYDROGEL_PACK", our_ask, -min(SIZE, sell_cap)))

        return orders

    # ──────────────────────────────────────────────────────────────────────────
    # VELVETFRUIT_EXTRACT — market-make using live mid-price as FV
    # ──────────────────────────────────────────────────────────────────────────
    def _trade_velvetfruit(self, od: OrderDepth, pos: int, lim: int) -> List[Order]:
        """
        Market-make around current mid (no directional bet).
        With 5-pt market spread: bid at best_bid+1, ask at best_ask-1.
        Earn 2 pts per fill, inventory skew keeps position balanced.
        """
        orders: List[Order] = []
        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        if best_bid is None or best_ask is None:
            return orders

        mid    = (best_bid + best_ask) / 2.0
        HS     = self.VELVETFRUIT_HS
        SIZE   = self.VELVETFRUIT_SIZE

        adj_fv  = mid - self.VELVETFRUIT_SKEW * pos
        our_bid = round(adj_fv - HS)
        our_ask = round(adj_fv + HS)

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
            orders.append(Order("VELVETFRUIT_EXTRACT", our_bid,  min(SIZE, buy_cap)))
        if sell_cap > 0:
            orders.append(Order("VELVETFRUIT_EXTRACT", our_ask, -min(SIZE, sell_cap)))

        return orders
