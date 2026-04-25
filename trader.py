from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict


class Trader:
    """
    Round 3 Strategy — v1

    Products: HYDROGEL_PACK, VELVETFRUIT_EXTRACT, VEV_4000 … VEV_6500

    ── HYDROGEL_PACK  (mean-reverting ~10,000, spread 15–16) ───────────────
    Perfect market-making target. FV = 10,000, quote inside at ±7.
    Symmetric mean-reverting dynamics → standard min() ask push-to-front.
    Inventory skew (SKEW=0.10) manages position drift.

    ── VELVETFRUIT_EXTRACT  (trending up ~+50 over 3 days) ─────────────────
    Day 0 mean=5246 → Day 2 mean=5255, closing at 5295 by end of Day 2.
    Buy-and-hold to position limit, same framework as INTARIAN_PEPPER_ROOT.

    ── VEV OPTIONS  (calls on VELVETFRUIT, TTE=7 starting Day 1) ───────────
    Deep ITM (VEV_4000, VEV_4500):
      Trade at pure intrinsic (spot - strike), zero time value.
      Buying = extra delta-1 VELVETFRUIT exposure with separate position limits.
      Expected gain per product: 80 units × 50 pt drift = +4,000 PnL.

    ITM (VEV_5000, VEV_5100):
      Small time value (7 and 22 pts) that decays slowly.
      Delta ~0.9–0.95 → strong spot exposure. Buy to limit, accept market TV.
      Net expected: ~40–47 pts gain per unit on 50-pt spot move.

    Near ATM (VEV_5200):
      TV ~47–51 pts (peak for ATM options), decaying ~4 pts/day.
      Delta ~0.55 → moderate spot exposure. Buy to limit conservatively.
      Breakeven on 50-pt spot move if TV decay ≤ delta-gain.

    OTM (VEV_5300) — borderline:
      TV ~44–49 pts, spot barely reaches strike even with trend.
      Skip to avoid paying TV that expires worthless.

    Deep OTM (VEV_5400+): Skip. Likely expires worthless.
    VEV_6000 / VEV_6500: Worthless (bid=0, ask=1).
    """

    POSITION_LIMITS: Dict[str, int] = {
        "HYDROGEL_PACK":       80,
        "VELVETFRUIT_EXTRACT": 80,
        "VEV_4000": 80,
        "VEV_4500": 80,
        "VEV_5000": 80,
        "VEV_5100": 80,
        "VEV_5200": 80,
        "VEV_5300":  0,   # skip — OTM, theta likely kills the trade
        "VEV_5400":  0,
        "VEV_5500":  0,
        "VEV_6000":  0,
        "VEV_6500":  0,
    }

    # ── HYDROGEL parameters ──────────────────────────────────────────────────
    HYDROGEL_FV        = 10000
    HYDROGEL_HS        = 7       # quote inside the 15-16 pt market spread
    HYDROGEL_SKEW      = 0.10    # adj_fv = FV - SKEW × pos
    HYDROGEL_TAKE_EDGE = 0       # take all asks strictly < FV
    HYDROGEL_SIZE      = 25      # units per passive quote

    # ── VEV parameters ───────────────────────────────────────────────────────
    # Max premium above intrinsic we're willing to pay per strike tier
    VEV_MAX_PREMIUM: Dict[str, float] = {
        "VEV_4000":  2.0,   # pure delta-1, zero TV → only buy within 2 pts of intrinsic
        "VEV_4500":  2.0,
        "VEV_5000": 15.0,   # ITM; market TV ≈ 7 → accept up to 15
        "VEV_5100": 35.0,   # ITM; market TV ≈ 22 → accept up to 35
        "VEV_5200": 65.0,   # near ATM; market TV ≈ 47–51 → accept up to 65
    }
    VEV_STRIKES: Dict[str, int] = {
        "VEV_4000": 4000, "VEV_4500": 4500,
        "VEV_5000": 5000, "VEV_5100": 5100,
        "VEV_5200": 5200, "VEV_5300": 5300,
        "VEV_5400": 5400, "VEV_5500": 5500,
        "VEV_6000": 6000, "VEV_6500": 6500,
    }
    VEV_BID_SIZE = 15   # passive bid size per tick

    def __init__(self):
        self._spot: float | None = None   # live VELVETFRUIT mid-price

    # ──────────────────────────────────────────────────────────────────────────
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        # Update spot from VELVETFRUIT order book (needed for VEV FV)
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

            if product == "HYDROGEL_PACK":
                orders = self._trade_hydrogel(od, pos, lim)
            elif product == "VELVETFRUIT_EXTRACT":
                orders = self._trade_velvetfruit(od, pos, lim)
            elif product in self.VEV_STRIKES and lim > 0:
                orders = self._trade_vev(product, od, pos, lim)
            else:
                orders = []

            result[product] = orders

        return result, 0, "ROUND3_v1"

    # ──────────────────────────────────────────────────────────────────────────
    # HYDROGEL_PACK — inventory-skewed market maker
    # ──────────────────────────────────────────────────────────────────────────
    def _trade_hydrogel(self, od: OrderDepth, pos: int, lim: int) -> List[Order]:
        orders: List[Order] = []
        FV   = self.HYDROGEL_FV
        HS   = self.HYDROGEL_HS
        EDGE = self.HYDROGEL_TAKE_EDGE
        SIZE = self.HYDROGEL_SIZE

        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        adj_fv  = FV - self.HYDROGEL_SKEW * pos
        our_bid = round(adj_fv - HS)
        our_ask = round(adj_fv + HS)

        # ── TAKE: sweep mispriced orders ─────────────────────────────────────
        for ask in sorted(od.sell_orders.keys()):
            if ask >= FV - EDGE:
                break
            qty = min(abs(od.sell_orders[ask]), lim - pos)
            if qty <= 0:
                break
            orders.append(Order("HYDROGEL_PACK", ask, qty))
            pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= FV + EDGE:
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
            our_ask = min(our_ask, best_ask - 1)   # min() correct for symmetric spread

        # Inventory-aware caps
        our_bid = min(our_bid, round(adj_fv))
        our_ask = max(our_ask, round(adj_fv))

        # Hard FV guardrail
        our_bid = min(our_bid, FV)
        our_ask = max(our_ask, FV)

        # Safety: don't cross spread or self
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
    # VELVETFRUIT_EXTRACT — buy-and-hold trend follower
    # ──────────────────────────────────────────────────────────────────────────
    def _trade_velvetfruit(self, od: OrderDepth, pos: int, lim: int) -> List[Order]:
        orders: List[Order] = []
        best_ask = min(od.sell_orders) if od.sell_orders else None
        best_bid = max(od.buy_orders)  if od.buy_orders  else None

        # Sweep all available asks up to position limit
        for ask_price in sorted(od.sell_orders.keys()):
            if pos >= lim:
                break
            qty = min(abs(od.sell_orders[ask_price]), lim - pos)
            if qty > 0:
                orders.append(Order("VELVETFRUIT_EXTRACT", ask_price, qty))
                pos += qty

        # Passive bid at best_bid+1 to catch residual sellers
        if pos < lim and best_bid is not None and best_ask is not None:
            our_bid = best_bid + 1
            if our_bid >= best_ask:
                our_bid = best_ask - 1
            qty = lim - pos
            if qty > 0:
                orders.append(Order("VELVETFRUIT_EXTRACT", our_bid, qty))

        return orders

    # ──────────────────────────────────────────────────────────────────────────
    # VEV OPTIONS — directional buy to capture VELVETFRUIT uptrend via options
    # ──────────────────────────────────────────────────────────────────────────
    def _trade_vev(self, product: str, od: OrderDepth, pos: int, lim: int) -> List[Order]:
        """
        Buy VEV calls to gain leveraged delta-1 exposure to the VELVETFRUIT uptrend.
        Each product has its own position limit → stacking multiple ITM calls
        multiplies our spot exposure beyond VELVETFRUIT's limit alone.

        Only buy if ask ≤ intrinsic + max_premium (limits time-value overpayment).
        """
        spot = self._spot
        if spot is None:
            return []

        strike     = self.VEV_STRIKES[product]
        intrinsic  = max(0.0, spot - strike)
        max_prem   = self.VEV_MAX_PREMIUM.get(product, 0.0)
        max_price  = intrinsic + max_prem

        orders: List[Order] = []
        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        # TAKE: sweep asks within our max_price budget
        for ask_price in sorted(od.sell_orders.keys()):
            if pos >= lim:
                break
            if ask_price > max_price:
                break
            qty = min(abs(od.sell_orders[ask_price]), lim - pos)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos += qty

        # MAKE: passive bid to fill over time
        if pos < lim and best_bid is not None and best_ask is not None:
            # Bid at best_bid+1, but cap at intrinsic + max_prem - 1
            our_bid = min(best_bid + 1, round(max_price) - 1)
            our_bid = max(our_bid, round(intrinsic))   # never bid below intrinsic
            if our_bid < best_ask:
                qty = min(self.VEV_BID_SIZE, lim - pos)
                if qty > 0:
                    orders.append(Order(product, our_bid, qty))
        elif pos < lim and best_ask is not None:
            # No bid in book: place passive bid at intrinsic
            bid_price = round(intrinsic)
            if bid_price < best_ask:
                orders.append(Order(product, bid_price, min(self.VEV_BID_SIZE, lim - pos)))

        return orders
