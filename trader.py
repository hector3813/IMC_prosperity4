from datamodel import OrderDepth, TradingState, Order
from typing import List


class Trader:
    """
    Round 2 Strategy — v4

    Products: ASH_COATED_OSMIUM + INTARIAN_PEPPER_ROOT (same as Round 1)

    New mechanic: Market Access Fee (MAF)
    ────────────────────────────────────────────────────────────────────
    Setting MARKET_ACCESS_FEE bids for the right to access 25% more
    order book volume. Top 50% of bidders win, pay the fee, and get
    the extra flow. Losers pay nothing and trade normally.

    Incremental value estimate:
      • ASH passive fills: ~25% more fill opportunities ≈ +400–600 PnL
      • PEPPER: fills limit 25% faster → more trend capture ≈ +300–500 PnL
      • Total incremental ≈ 700–1100 PnL/day → ~2100–3300 over round
    Setting MAF = 2000 to be competitive while staying clearly profitable
    if we win. Adjust upward if we learn competitors are bidding higher.

    ASH_COATED_OSMIUM  (FV ~10000, mean-reverting)
    ────────────────────────────────────────────────────────────────────
    Round 2 order book structure:
      Day -1: ask1 often AT 10000 (tight)
      Day 0:  ask1 = 10008–10013 — our passive ask is BEST by 7+ pts ✓
      Day 1:  ask1 = 10016–10020 — our passive ask is BEST by 15+ pts ✓
    Problem in v1/v2: ask side filled MUCH faster than bid side → position
    drifted to -80 short → locked (sell_cap=0), could not trade.
    v3 fix: SKEW raised from 0.07 → 0.15.
    v4 fix: Ask push-to-front flipped from min → max.
      OLD: our_ask = min(our_ask, best_ask-1) → drags ask DOWN to near market
           e.g. pos=0, best_ask=10016: our_ask=min(10004,10015)=10004 → +4 pts/fill
      NEW: our_ask = max(our_ask, best_ask-1) → pushes ask UP to near market
           e.g. pos=0, best_ask=10016: our_ask=max(10004,10015)=10015 → +15 pts/fill
    3.75× more PnL per sell fill on Day 1 (wide spread market).

    INTARIAN_PEPPER_ROOT  (trends +1000/day)
    ────────────────────────────────────────────────────────────────────
    Day -1 → Day 0 → Day 1: 11001 → 12000 → 13000 → continues rising.
    Strategy: buy to limit immediately and hold. Never sell.
    """

    POSITION_LIMITS = {
        "INTARIAN_PEPPER_ROOT": 80,
        "ASH_COATED_OSMIUM": 80,
    }

    # ── Market Access Fee ────────────────────────────────────────────────
    # Bid 2000 XIRECS for 25% more order book volume.
    # Expected net gain if won: ~100–1300 XIRECS over the round.
    MARKET_ACCESS_FEE = 2000

    # ── ASH parameters (unchanged from Round 1 v4) ───────────────────────
    ASH_FAIR_VALUE  = 10000
    ASH_HALF_SPREAD = 4      # passive quotes at adj_fv ± 4
    ASH_TAKE_EDGE   = 0      # take any ask strictly < FV (catches 9999 etc.)
    ASH_SKEW        = 0.15   # adj_fv = FV - SKEW * pos (inventory management)
    ASH_MAKE_SIZE   = 30     # units per passive quote

    # ── PEPPER parameters ────────────────────────────────────────────────
    PEPPER_TREND_PER_STEP = 0.1   # +0.1 per 100-unit timestamp step

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
        return result, self.MARKET_ACCESS_FEE, "ROUND2_v4"

    # ------------------------------------------------------------------
    # ASH_COATED_OSMIUM — inventory-skewed market maker
    # ------------------------------------------------------------------
    def _trade_ash(self, od: OrderDepth, pos: int, lim: int) -> List[Order]:
        orders: List[Order] = []
        FV   = self.ASH_FAIR_VALUE
        HS   = self.ASH_HALF_SPREAD
        EDGE = self.ASH_TAKE_EDGE
        SIZE = self.ASH_MAKE_SIZE

        best_bid = max(od.buy_orders)  if od.buy_orders  else None
        best_ask = min(od.sell_orders) if od.sell_orders else None

        # Inventory skew: push adj fair value lower when long, higher when short
        adj_fv  = FV - self.ASH_SKEW * pos
        our_bid = round(adj_fv - HS)
        our_ask = round(adj_fv + HS)

        # ── TAKE: sweep all mispriced orders ────────────────────────────
        # Buy every ask strictly below FV (EDGE=0 → takes 9999, 9998 …)
        for ask in sorted(od.sell_orders.keys()):
            if ask >= FV - EDGE:
                break
            qty = min(abs(od.sell_orders[ask]), lim - pos)
            if qty <= 0:
                break
            orders.append(Order("ASH_COATED_OSMIUM", ask, qty))
            pos += qty

        # Sell to every bid strictly above FV
        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= FV + EDGE:
                break
            qty = min(od.buy_orders[bid], lim + pos)
            if qty <= 0:
                break
            orders.append(Order("ASH_COATED_OSMIUM", bid, -qty))
            pos -= qty

        # ── MAKE: price-follow market, stay within adj_fv band ─────────────
        # BID: floor at best_bid+1 so we're tied for top of bid queue
        if best_bid is not None:
            our_bid = max(our_bid, best_bid + 1)
        # ASK: floor at best_ask-1 so we don't undercut market by a huge margin
        #   When market ask = 10016 and our natural ask = 10004, old min() logic
        #   dragged us to 10004 (earning only 4 pts/fill).
        #   New max() logic: our_ask = max(10004, 10015) = 10015 → 15 pts/fill.
        #   SKEW naturally raises adj_fv+HS as pos goes short, so ask climbs
        #   toward market price the more short we become.
        if best_ask is not None:
            our_ask = max(our_ask, best_ask - 1)

        # Inventory-aware caps:
        our_bid = min(our_bid, round(adj_fv))
        our_ask = max(our_ask, round(adj_fv))

        # Hard FV guardrail: never bid strictly above FV (avoid adverse
        # selection) or ask strictly below FV.
        our_bid = min(our_bid, FV)
        our_ask = max(our_ask, FV)

        # Safety: don't cross external spread or our own spread
        if best_ask is not None and our_bid >= best_ask:
            our_bid = best_ask - 1
        if best_bid is not None and our_ask <= best_bid:
            our_ask = best_bid + 1
        if our_ask <= our_bid:
            our_ask = our_bid + 1

        buy_cap  = lim - pos
        sell_cap = lim + pos

        if buy_cap > 0:
            orders.append(Order("ASH_COATED_OSMIUM", our_bid, min(SIZE, buy_cap)))
        if sell_cap > 0:
            orders.append(Order("ASH_COATED_OSMIUM", our_ask, -min(SIZE, sell_cap)))

        return orders

    # ------------------------------------------------------------------
    # INTARIAN_PEPPER_ROOT — buy-and-hold trend follower
    # ------------------------------------------------------------------
    def _trade_pepper(self, od: OrderDepth, pos: int, lim: int,
                      state: TradingState) -> List[Order]:
        orders: List[Order] = []

        best_ask = min(od.sell_orders) if od.sell_orders else None
        best_bid = max(od.buy_orders)  if od.buy_orders  else None

        # Anchor fair value on first tick for reference
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

        # ── TAKE: sweep all available asks up to position limit ──────────
        for ask_price in sorted(od.sell_orders.keys()):
            if pos >= lim:
                break
            qty = min(abs(od.sell_orders[ask_price]), lim - pos)
            if qty > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", ask_price, qty))
                pos += qty

        # ── MAKE: passive bid at best_bid+1 to catch residual sellers ───
        if pos < lim and best_bid is not None and best_ask is not None:
            our_bid = best_bid + 1
            if our_bid >= best_ask:
                our_bid = best_ask - 1
            qty = lim - pos
            if qty > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", our_bid, qty))

        return orders
