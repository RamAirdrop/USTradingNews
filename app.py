"""Daybreak (Streamlit): US-equity news signals, conditional intraday levels and a compounding paper portfolio."""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

import streamlit as st

import auth
import market
import portfolio as pf
import storage

st.set_page_config(page_title="Daybreak | US paper trading", page_icon="🌅", layout="wide")


# ------------------------------------------------------------------ helpers
def secret(name: str, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:  # noqa: BLE001 - no secrets file configured
        return default


OFF_HOURS = str(secret("ALLOW_OFF_HOURS", os.environ.get("ALLOW_OFF_HOURS", "0"))).strip() == "1"


def usd(value) -> str:
    if value is None:
        return "—"
    return f"{'−' if value < 0 else ''}${abs(value):,.2f}"


def signed(value) -> str:
    return "—" if value is None else (f"+{usd(value)}" if value > 0 else usd(value))


def et_time(iso: str) -> str:
    """Shown in DISPLAY_TZ (default CET/CEST) with the zone label, e.g. '23 Nov 15:30 CET'."""
    return market.local_time(datetime.fromisoformat(iso))


# ------------------------------------------------------------ market snapshot
@st.cache_resource
def market_state() -> dict:
    return {"lock": threading.Lock(), "snapshot": storage.kv_get("market_us"), "last_attempt": 0.0, "last_error": None}


def ensure_snapshot(force: bool = False) -> dict:
    state = market_state()
    min_gap = 60 if force else 900  # failures are retried every 15 minutes, manual refresh once a minute
    if (force or market.snapshot_due(state["snapshot"])) and time.time() - state["last_attempt"] >= min_gap:
        with state["lock"]:
            if time.time() - state["last_attempt"] >= min_gap:
                state["last_attempt"] = time.time()
                try:
                    result = market.collect_market()
                    if not any(s["status"] == "ok" for s in result["sources"]):
                        raise RuntimeError("All news sources failed: " + "; ".join(result["errors"]))
                    storage.kv_set("market_us", result)
                    state["snapshot"], state["last_error"] = result, None
                except Exception as error:  # noqa: BLE001
                    state["last_error"] = str(error)
    return state


@st.cache_data(ttl=120, show_spinner=False)
def intraday(symbol: str):
    return market.fetch_intraday(symbol)


# ----------------------------------------------------------------- portfolio
def load_portfolio(key: str) -> dict:
    data = storage.kv_get(f"portfolio_us:{key}")
    if data is None:
        return pf.create_portfolio()
    if not pf.valid_portfolio(data):
        raise RuntimeError("Saved portfolio is corrupt; refusing to overwrite it.")
    return data


def settle_user(key: str) -> dict:
    symbols = {x["symbol"] for x in load_portfolio(key)["positions"]}
    candles = {}
    for symbol in symbols:
        try:
            candles[symbol] = intraday(symbol)
        except Exception as error:  # noqa: BLE001
            st.warning(f"Could not fetch prices for {symbol}: {error}")
    with storage.LOCK:
        p = load_portfolio(key)
        closed = pf.settle(p, candles, datetime.now(timezone.utc), OFF_HOURS)
        if p["positions"] or closed:
            storage.kv_set(f"portfolio_us:{key}", p)
    return p


def open_trade(key: str, stock: dict, amount: float) -> str:
    try:
        quote = market.fetch_quote(stock["symbol"])
    except Exception:  # noqa: BLE001
        quote = stock.get("quote")
        if not quote:
            raise
    scenario = market.build_scenario(quote, stock["score"])
    if not scenario:
        raise ValueError("No directional scenario is available for this stock.")
    if not quote["fresh"] and not OFF_HOURS:
        raise ValueError("Quote is not fresh (today and under 20 minutes old). Try again during market hours.")
    with storage.LOCK:
        p = load_portfolio(key)
        position = pf.open_position(p, symbol=stock["symbol"], name=stock["name"], side=scenario["side"], price=quote["price"],
                                    risk=abs(scenario["entry"] - scenario["stop"]), amount=amount, off_hours=OFF_HOURS)
        storage.kv_set(f"portfolio_us:{key}", p)
    return f"Opened {position['side']} {position['qty']} × {position['symbol']} at {usd(position['entry'])}."


def close_trade(key: str, position_id: int) -> str:
    with storage.LOCK:
        p = load_portfolio(key)
        position = next((x for x in p["positions"] if x["id"] == position_id), None)
        if not position:
            raise ValueError("Position not found.")
        try:
            price = market.fetch_quote(position["symbol"])["price"]
        except Exception:  # noqa: BLE001
            price = position["lastPrice"]
        trade = pf.close_position(p, position_id, price, "Closed manually")
        storage.kv_set(f"portfolio_us:{key}", p)
    return f"Closed {trade['symbol']}: {signed(trade['pnl'])}"


# ------------------------------------------------------------------- sign-in
def google_enabled() -> bool:
    try:
        return hasattr(st, "login") and "auth" in st.secrets
    except Exception:  # noqa: BLE001
        return False


def current_user() -> dict | None:
    if st.session_state.get("user"):
        return st.session_state["user"]
    if google_enabled() and getattr(st.user, "is_logged_in", False):
        email = str(st.user.get("email") or "").lower()
        allowed = [str(e).lower() for e in secret("allowed_emails", [])]
        if allowed and email not in allowed:
            st.sidebar.error("This Google account is not on the allow-list.")
            return None
        return {"key": f"google:{st.user.get('sub') or email}", "label": email}
    return None


def sidebar(user: dict | None) -> None:
    st.sidebar.title("🌅 Daybreak")
    if user:
        st.sidebar.success(f"Signed in as **{user['label']}**")
        if st.sidebar.button("Sign out"):
            st.session_state.pop("user", None)
            if google_enabled() and getattr(st.user, "is_logged_in", False):
                st.logout()
            st.rerun()
        return
    st.sidebar.subheader("Sign in")
    if google_enabled():
        st.sidebar.button("Continue with Google", on_click=st.login, args=["google"] if "google" in dict(st.secrets.get("auth", {})) else [], use_container_width=True)
        st.sidebar.caption("or use a username and password")
    tab_in, tab_up = st.sidebar.tabs(["Log in", "Register"])
    with tab_in, st.form("login"):
        u, p = st.text_input("Username"), st.text_input("Password", type="password")
        if st.form_submit_button("Log in"):
            try:
                key = auth.login(u, p)
                st.session_state["user"] = {"key": key, "label": u.strip().lower()}
                st.rerun()
            except ValueError as error:
                st.error(str(error))
    with tab_up:
        if secret("allow_registration", True):
            with st.form("register"):
                u, p, p2 = st.text_input("Choose a username"), st.text_input("Choose a password", type="password"), st.text_input("Repeat password", type="password")
                if st.form_submit_button("Create account"):
                    try:
                        if p != p2:
                            raise ValueError("Passwords do not match.")
                        key = auth.register(u, p)
                        st.session_state["user"] = {"key": key, "label": u.strip().lower()}
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))
        else:
            st.info("Registration is closed. Ask the administrator for an account.")


# --------------------------------------------------------------------- views
def render_stock(stock: dict, user: dict | None, idx: int) -> None:
    with st.container(border=True):
        left, mid, right = st.columns([3, 2, 2])
        left.markdown(f"**{stock['name']}**  \n`{stock['symbol']}` · {stock['sector']}")
        quote = stock.get("quote")
        mid.metric("Delayed price", usd(quote["price"]) if quote else "—",
                   f"{quote['changePct']:+.2f}%" if quote and quote.get("changePct") is not None else None)
        icon = {"Positive": "🟢", "Negative": "🔴", "Neutral": "⚪"}[stock["sentiment"]]
        right.markdown(f"{icon} **{stock['sentiment']}**  \nscore {stock['score']:+d} · {len(stock['news'])} headline(s)")
        headline = next((a for a in stock["news"] if a["score"] * stock["score"] > 0), stock["news"][0])
        st.caption(f"▤ {headline['title']}")
        conf = stock["confidence"]
        if conf.get("percent") is None:
            st.caption("News confidence: N/A · no directional evidence")
        else:
            st.progress(conf["percent"] / 100, text=f"News confidence {conf['percent']}% · {conf['label']} · {conf['publishers']} publisher(s) · not a profit probability")
        sc = stock["scenario"]
        if sc:
            a, b, c = st.columns(3)
            a.metric(f"{sc['entryAction']} at", usd(sc["entry"]))
            b.metric(f"{sc['exitAction']} at (target)", usd(sc["target"]))
            c.metric("Stop loss", usd(sc["stop"]))
            st.caption(f"{'BUY → SELL' if sc['side'] == 'LONG' else 'SELL → BUY BACK'} · ~2:1 reward/risk · {sc['status']} · illustrative only")
        else:
            st.caption("Mixed/neutral news or no price history: no directional setup.")
        with st.expander("Source headlines"):
            for a in stock["news"]:
                st.markdown(f"- [{a['title']}]({a['url']}) — {a['source']}, {et_time(a['publishedAt'])} ({a['label']})")
        if sc:
            with st.expander("📈 Paper trade this signal"):
                if not user:
                    st.info("Sign in (sidebar) to place paper trades.")
                else:
                    with st.form(f"trade-{stock['symbol']}-{idx}"):
                        available = pf.available_capital(load_portfolio(user["key"]))
                        amount = st.number_input("Amount to deploy ($)", min_value=1.0, max_value=max(1.0, float(available)), value=max(1.0, float(int(available))), step=50.0)
                        if st.form_submit_button("Open paper trade"):
                            try:
                                st.success(open_trade(user["key"], stock, amount))
                            except Exception as error:  # noqa: BLE001
                                st.error(str(error))


def render_radar(snapshot: dict | None, user: dict | None) -> None:
    if not snapshot or snapshot["day"] != market.et_parts()["day"]:
        st.warning("No current-day snapshot yet. Previous-day ideas are never shown. Use **Refresh data** (first load takes about a minute).")
        return
    stocks = market.present_stocks(snapshot)
    pos, neg = sum(s["score"] > 0 for s in stocks), sum(s["score"] < 0 for s in stocks)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Stocks in focus", len(stocks))
    c2.metric("Positive (buy → sell)", pos)
    c3.metric("Negative (sell → buy back)", neg)
    c4.metric("Headlines analysed", len(snapshot["articles"]))
    index = snapshot.get("index")
    if index:
        st.metric("S&P 500 (delayed)", f"{index['price']:,.2f}", f"{index['changePct']:+.2f}% vs previous close" if index.get("changePct") is not None else None)
        st.line_chart({c["time"][:10]: c["close"] for c in index["candles"]}, height=160)
    f1, f2, f3 = st.columns([2, 2, 3])
    flt = f1.selectbox("Signal", ["All", "Positive", "Negative", "Neutral"])
    sort = f2.selectbox("Sort by", ["Signal strength", "News confidence", "News coverage", "Name"])
    query = f3.text_input("Search", placeholder="Search stocks…").lower()
    shown = [s for s in stocks if (flt == "All" or s["sentiment"] == flt) and query in f"{s['name']} {s['symbol']} {s['sector']}".lower()]
    keys = {"Signal strength": lambda s: (-abs(s["score"]), -len(s["news"])), "News confidence": lambda s: -(s["confidence"].get("percent") or -1),
            "News coverage": lambda s: -len(s["news"]), "Name": lambda s: s["name"]}
    for i, stock in enumerate(sorted(shown, key=keys[sort])):
        render_stock(stock, user, i)
    if not shown:
        st.info("No covered stocks match.")


@st.fragment(run_every="30s")
def render_portfolio(user: dict | None) -> None:
    if not user:
        st.info("Sign in from the sidebar to use your paper portfolio (starts at $1,000).")
        return
    try:
        s = pf.summarize(settle_user(user["key"]))
    except Exception as error:  # noqa: BLE001
        st.error(str(error))
        return
    a, b, c = st.columns(3)
    a.metric("Balance", usd(s["balance"]), f"started {usd(s['startingCapital'])}", delta_color="off")
    b.metric("Total realised P/L", signed(s["realizedPnl"]), f"{s['realizedPct']:+.2f}%")
    c.metric("Today's realised P/L", signed(s["todayPnl"]), f"day opened at {usd(s['todayOpening'])}", delta_color="off")
    d, e, f = st.columns(3)
    d.metric("Open P/L", signed(s["unrealizedPnl"]), f"equity {usd(s['equity'])}", delta_color="off")
    e.metric("Available to trade", usd(s["available"]), f"{usd(s['reserved'])} in open positions", delta_color="off")
    f.metric("Win rate", "—" if s["winRate"] is None else f"{s['winRate']}%", f"{s['wins']}W · {s['losses']}L · {s['totalTrades']} trades", delta_color="off")
    st.subheader("Open positions")
    if not s["positions"]:
        st.caption("No open positions. Open one from a stock card on the Market radar tab.")
    for x in s["positions"]:
        cols = st.columns([2, 1, 2, 2, 2, 2, 1.5])
        cols[0].markdown(f"**{x['symbol']}**  \n{'BUY → SELL' if x['side'] == 'LONG' else 'SELL → BUY BACK'}")
        cols[1].write(f"Qty {x['qty']:g}")
        cols[2].write(f"Entry {usd(x['entry'])}")
        cols[3].write(f"Last {usd(x['lastPrice'])}")
        cols[4].write(f"SL {usd(x['stop'])} · T {usd(x['target'])}")
        cols[5].write(f"{signed(x['unrealized'])} ({x['unrealizedPct']:+.2f}%)")
        if cols[6].button("Close now", key=f"close-{x['id']}"):
            try:
                st.toast(close_trade(user["key"], x["id"]))
            except Exception as error:  # noqa: BLE001
                st.error(str(error))
            st.rerun(scope="fragment")
    st.subheader("Daily ledger")
    if s["days"]:
        st.dataframe([{"Day": d["day"], "Opening": usd(d["opening"]), "Trades": d["trades"], "Wins": d["wins"], "P/L": signed(d["pnl"]), "Closing": usd(d["closing"])} for d in s["days"]], hide_index=True, use_container_width=True)
    else:
        st.caption("No completed days yet.")
    st.subheader("Closed trades")
    if s["trades"]:
        st.dataframe([{"Closed": et_time(t["closedAt"]), "Stock": t["symbol"], "Side": t["side"], "Qty": t["qty"], "Entry": usd(t["entry"]),
                       "Exit": usd(t["exit"]), "Reason": t["reason"], "P/L": f"{signed(t['pnl'])} ({t['pnlPct']:+.2f}%)"} for t in s["trades"]], hide_index=True, use_container_width=True)
    else:
        st.caption("No closed trades yet.")
    with st.expander("Reset portfolio"):
        if st.checkbox("I understand this erases all trades and restores $1,000", key="reset-ok") and st.button("Reset portfolio"):
            with storage.LOCK:
                storage.kv_set(f"portfolio_us:{user['key']}", pf.create_portfolio())
            st.rerun(scope="fragment")
    st.caption("Simulation only. Fees, taxes, slippage and short-sale margin are ignored; each position reserves its full notional value. "
               "Stops, targets and the 15:45 ET square-off are replayed from 5-minute candles whenever this page loads.")


def render_news(snapshot: dict | None) -> None:
    if not snapshot:
        st.info("Headlines appear after the first refresh.")
        return
    for src in snapshot["sources"]:
        st.caption(f"{src['name']}: {src['count']} today" if src["status"] == "ok" else f"{src['name']}: unavailable ({src.get('error')})")
    for a in snapshot["articles"]:
        st.markdown(f"**{a['label']}** · [{a['title']}]({a['url']}) — {a['source']}, {et_time(a['publishedAt'])}")


ABOUT = """
**A research assistant, not a crystal ball.**

1. **News.** Public RSS feeds (CNBC, MarketWatch, Investing.com, Google News). Only headlines since the previous 16:00 ET close are used.
2. **Signal.** Keyword rules score each headline −3…+3 and sum per company across a fixed {n}-stock US large-cap universe. Heuristic, not AI forecasting.
3. **Levels.** Positive: buy = previous close + 0.15·ATR, stop = entry − 0.6·ATR, sell target = entry + 2·risk. Negative mirrors this for sell → buy back. ATR averages up to 14 completed daily true ranges (min 10).
4. **News confidence.** 60% publisher-balanced agreement + 25% publisher breadth + 15% freshness, capped at 49/74/90% for 1/2/3+ publishers. It is **not** a win probability.
5. **Paper portfolio.** Starts at $1,000; fractional shares are allowed; trades are sized from the current balance so profits compound and losses shrink the next trade. Positions close at stop, 2R target or 15:45 ET (15 minutes before the close).

Prices come from Yahoo Finance's unofficial endpoint (delayed, no guarantees). Educational use only — not investment advice.
"""


# ----------------------------------------------------------------------- main
user = current_user()
sidebar(user)
state = market_state()
with st.spinner("Collecting public headlines and delayed prices (first run takes about a minute)…"):
    ensure_snapshot()
if st.sidebar.button("↻ Refresh data"):
    with st.spinner("Refreshing…"):
        ensure_snapshot(force=True)
if state["last_error"]:
    st.sidebar.warning(f"Refresh issue: {state['last_error']}")
if state["snapshot"]:
    st.sidebar.caption(f"Updated {et_time(state['snapshot']['updatedAt'])} · auto-refresh after 09:45 ET")
    _day = market.et_parts()["day"]
    st.sidebar.caption(f"US session today: {market.session_hours_local()}" if market.is_trading_day(_day)
                       else "US market closed today (weekend/holiday). Next session: " + market.session_hours_local(
                           next(str(date.fromisoformat(_day) + timedelta(days=i)) for i in range(1, 8)
                                if market.is_trading_day(str(date.fromisoformat(_day) + timedelta(days=i))))))
    for err in state["snapshot"].get("errors", [])[:5]:
        st.sidebar.caption(f"⚠ {err}")

st.title("Daybreak — US intraday paper trading")
tab_radar, tab_port, tab_news, tab_about = st.tabs(["Market radar", "Paper portfolio", "News feed", "How it works"])
with tab_radar:
    render_radar(state["snapshot"], user)
with tab_port:
    render_portfolio(user)
with tab_news:
    render_news(state["snapshot"])
with tab_about:
    st.markdown(ABOUT.replace("{n}", str(len(market.UNIVERSE))))
st.caption("Educational only. Not investment advice or a recommendation from a registered adviser. Markets involve risk.")
