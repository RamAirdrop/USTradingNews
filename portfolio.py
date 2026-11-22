"""Paper-trading portfolio rules. Pure functions over a JSON-serialisable dict, so any storage backend works.

Because a free host may sleep, stops, targets and the 15:45 ET square-off are *replayed* from five-minute
candles whenever the portfolio is next loaded; the outcome is the same as if a scheduler had run all day.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from market import MARKET_TZ, SESSION_OPEN, SQUARE_OFF, et_parts

START_CAPITAL = 1000.0  # US$


def _round2(x: float) -> float:
    return round(x * 100) / 100


def _tick(x: float) -> float:
    return round(x, 2)


def create_portfolio(starting_capital: float = START_CAPITAL, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"startingCapital": starting_capital, "balance": starting_capital, "positions": [], "trades": [],
            "createdAt": now.isoformat(), "nextId": 1}


def valid_portfolio(p) -> bool:
    return (isinstance(p, dict) and isinstance(p.get("startingCapital"), (int, float)) and isinstance(p.get("balance"), (int, float))
            and isinstance(p.get("positions"), list) and isinstance(p.get("trades"), list) and isinstance(p.get("nextId"), int))


def reserved_capital(p: dict) -> float:
    return sum(x["qty"] * x["entry"] for x in p["positions"])


def available_capital(p: dict) -> float:
    return max(0.0, _round2(p["balance"] - reserved_capital(p)))


def open_position(p: dict, *, symbol: str, name: str, side: str, price: float, risk: float, amount: float | None,
                  now: datetime | None = None, off_hours: bool = False) -> dict:
    """Size from the current balance so realised profits compound into the next trade (and losses shrink it)."""
    now = now or datetime.now(timezone.utc)
    parts = et_parts(now)
    if not off_hours and not (parts["weekday"] not in ("Sat", "Sun") and SESSION_OPEN <= parts["minutes"] < SQUARE_OFF):
        raise ValueError("New paper trades are only allowed 09:30–15:45 ET on weekdays.")
    if side not in ("LONG", "SHORT"):
        raise ValueError("Invalid trade side.")
    if not price or price <= 0 or not risk or risk <= 0:
        raise ValueError("Invalid price or risk distance.")
    if any(x["symbol"] == symbol for x in p["positions"]):
        raise ValueError(f"{symbol} already has an open position.")
    available = available_capital(p)
    budget = min(amount, available) if amount and amount > 0 else available
    qty = math.floor(budget / price * 1000) / 1000  # fractional shares (to 0.001), so $1,000 can still buy pricey stocks
    if qty < 0.001:
        raise ValueError(f"Not enough available capital (${available:,.2f}) to buy any of this stock at ${price:,.2f}.")
    d = 1 if side == "LONG" else -1
    position = {
        "id": p["nextId"], "symbol": symbol, "name": name, "side": side, "qty": qty, "entry": price,
        "stop": _tick(price - d * risk), "target": _tick(price + d * risk * 2),
        "openedAt": now.isoformat(), "day": parts["day"], "lastPrice": price, "lastPriceAt": now.isoformat(),
    }
    if position["stop"] <= 0:
        raise ValueError("Stop level would be non-positive.")
    p["nextId"] += 1
    p["positions"].append(position)
    return position


def close_position(p: dict, position_id: int, exit_price: float, reason: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    idx = next((i for i, x in enumerate(p["positions"]) if x["id"] == position_id), None)
    if idx is None:
        raise ValueError("Position not found.")
    if not exit_price or exit_price <= 0:
        raise ValueError("No valid exit price.")
    position = p["positions"].pop(idx)
    d = 1 if position["side"] == "LONG" else -1
    pnl = _round2(d * (exit_price - position["entry"]) * position["qty"])
    trade = {**position, "exit": exit_price, "reason": reason, "pnl": pnl,
             "pnlPct": _round2(pnl / (position["entry"] * position["qty"]) * 100),
             "closedAt": now.isoformat(), "closeDay": et_parts(now)["day"]}
    p["balance"] = _round2(p["balance"] + pnl)
    p["trades"].append(trade)
    return trade


def replay(position: dict, candles: list[dict], now: datetime, off_hours: bool = False) -> tuple[dict | None, float | None, datetime | None]:
    """Walk candles after entry. Returns (exit or None, latest price, latest price time).

    Within one candle the stop is assumed to trigger before the target (conservative), and a stop that gaps
    through its level fills at the candle open.
    """
    opened = datetime.fromisoformat(position["openedAt"])
    long = position["side"] == "LONG"
    last_price, last_time, session_close = None, None, None
    for c in candles:
        if c["time"] < opened:
            continue
        last_price, last_time = c["close"], c["time"]
        cp = et_parts(c["time"])
        if cp["day"] != position["day"] or cp["minutes"] >= SQUARE_OFF:
            continue
        stop_hit = c["low"] <= position["stop"] if long else c["high"] >= position["stop"]
        target_hit = c["high"] >= position["target"] if long else c["low"] <= position["target"]
        if stop_hit:
            fill = min(c["open"], position["stop"]) if long else max(c["open"], position["stop"])
            return {"price": fill, "reason": "Stop loss hit", "at": c["time"]}, last_price, last_time
        if target_hit:
            return {"price": position["target"], "reason": "Target hit", "at": c["time"]}, last_price, last_time
        session_close = c["close"]
    today = et_parts(now)
    due = position["day"] < today["day"] or (not off_hours and today["minutes"] >= SQUARE_OFF)
    if due:
        square_off_time = datetime.fromisoformat(position["day"] + f"T{SQUARE_OFF // 60:02d}:{SQUARE_OFF % 60:02d}:00").replace(tzinfo=MARKET_TZ)
        price = session_close or last_price or position["lastPrice"]
        return {"price": price, "reason": "End of day square-off", "at": square_off_time}, last_price, last_time
    return None, last_price, last_time


def settle(p: dict, candles_by_symbol: dict[str, list[dict]], now: datetime | None = None, off_hours: bool = False) -> list[dict]:
    """Apply stop / target / square-off rules to every open position. Returns the trades closed."""
    now = now or datetime.now(timezone.utc)
    closed = []
    for position in list(p["positions"]):
        if position["symbol"] not in candles_by_symbol:
            continue  # price data unavailable: leave the position open rather than guess
        exit_, last_price, last_time = replay(position, candles_by_symbol.get(position["symbol"], []), now, off_hours)
        if last_price:
            position["lastPrice"], position["lastPriceAt"] = last_price, last_time.isoformat()
        if exit_:
            closed.append(close_position(p, position["id"], exit_["price"], exit_["reason"], exit_["at"]))
    return closed


def summarize(p: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = et_parts(now)["day"]
    positions = []
    for x in p["positions"]:
        d = 1 if x["side"] == "LONG" else -1
        unreal = _round2(d * (x["lastPrice"] - x["entry"]) * x["qty"])
        positions.append({**x, "unrealized": unreal, "unrealizedPct": _round2(unreal / (x["entry"] * x["qty"]) * 100)})
    days, running = [], p["startingCapital"]
    for t in p["trades"]:
        if not days or days[-1]["day"] != t["closeDay"]:
            days.append({"day": t["closeDay"], "opening": running, "trades": 0, "wins": 0, "pnl": 0.0, "closing": running})
        day = days[-1]
        day["trades"] += 1
        day["wins"] += t["pnl"] > 0
        day["pnl"] = _round2(day["pnl"] + t["pnl"])
        running = _round2(running + t["pnl"])
        day["closing"] = running
    wins = sum(t["pnl"] > 0 for t in p["trades"])
    today_row = next((d for d in days if d["day"] == today), None)
    unreal_total = _round2(sum(x["unrealized"] for x in positions))
    realized = _round2(p["balance"] - p["startingCapital"])
    return {
        "startingCapital": p["startingCapital"], "balance": p["balance"], "realizedPnl": realized,
        "realizedPct": _round2(realized / p["startingCapital"] * 100),
        "reserved": _round2(reserved_capital(p)), "available": available_capital(p),
        "unrealizedPnl": unreal_total, "equity": _round2(p["balance"] + unreal_total),
        "todayPnl": today_row["pnl"] if today_row else 0.0, "todayOpening": today_row["opening"] if today_row else p["balance"],
        "totalTrades": len(p["trades"]), "wins": wins, "losses": sum(t["pnl"] < 0 for t in p["trades"]),
        "winRate": round(wins / len(p["trades"]) * 100) if p["trades"] else None,
        "positions": positions, "trades": list(reversed(p["trades"]))[:100], "days": list(reversed(days))[:60],
    }
