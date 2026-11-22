"""Market data, news sentiment and conditional intraday scenarios (US equities edition)."""
from __future__ import annotations

import html
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote as urlquote, urlparse

import requests

MARKET_TZ = ZoneInfo("America/New_York")  # US Eastern; follows EST/EDT automatically
SESSION_OPEN = 9 * 60 + 30     # NYSE/Nasdaq regular session opens 09:30 ET
MARKET_CLOSE = 16 * 60         # ...and closes 16:00 ET
SQUARE_OFF = 15 * 60 + 45      # paper positions are flattened 15 minutes before the close
MORNING_REFRESH = 9 * 60 + 45  # daily snapshot refresh, 15 minutes after the open

CACHE_DIR = Path("/tmp/daybreak_quote_cache")

SOURCES = [
    {"name": "CNBC Markets", "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html"},
    {"name": "CNBC Market Insider", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20409666"},
    {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"name": "Investing.com", "url": "https://www.investing.com/rss/news_25.rss"},
    {"name": "Google News (tech & media)", "url": "https://news.google.com/rss/search?q=(Apple+OR+Microsoft+OR+Nvidia+OR+AMD+OR+Intel+OR+Broadcom+OR+Oracle+OR+Salesforce+OR+Alphabet+OR+Google+OR+Meta+OR+Netflix+OR+Disney+OR+Amazon+OR+Tesla)+stock+when:1d&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Google News (other sectors)", "url": "https://news.google.com/rss/search?q=(Walmart+OR+Costco+OR+%22Home+Depot%22+OR+Coca-Cola+OR+PepsiCo+OR+McDonald%27s+OR+Nike+OR+JPMorgan+OR+%22Bank+of+America%22+OR+%22Goldman+Sachs%22+OR+%22Wells+Fargo%22+OR+%22Eli+Lilly%22+OR+UnitedHealth+OR+%22Johnson+%26+Johnson%22+OR+Pfizer+OR+Exxon+OR+Chevron+OR+Boeing+OR+Caterpillar)+stock+when:1d&hl=en-US&gl=US&ceid=US:en"},
]

_UNIVERSE = [
    ("AAPL", "Apple", "Technology", ["apple"]),
    ("MSFT", "Microsoft", "Technology", ["microsoft"]),
    ("NVDA", "NVIDIA", "Technology", ["nvidia"]),
    ("AMD", "Advanced Micro Devices", "Technology", ["amd", "advanced micro devices"]),
    ("INTC", "Intel", "Technology", ["intel"]),
    ("AVGO", "Broadcom", "Technology", ["broadcom"]),
    ("ORCL", "Oracle", "Technology", ["oracle"]),
    ("CRM", "Salesforce", "Technology", ["salesforce"]),
    ("GOOGL", "Alphabet (Google)", "Communication", ["alphabet", "google"]),
    ("META", "Meta Platforms", "Communication", ["meta platforms", "facebook"]),
    ("NFLX", "Netflix", "Communication", ["netflix"]),
    ("DIS", "Walt Disney", "Communication", ["disney"]),
    ("AMZN", "Amazon", "Consumer", ["amazon"]),
    ("TSLA", "Tesla", "Automotive", ["tesla"]),
    ("WMT", "Walmart", "Consumer", ["walmart"]),
    ("COST", "Costco", "Consumer", ["costco"]),
    ("HD", "Home Depot", "Consumer", ["home depot"]),
    ("KO", "Coca-Cola", "Consumer", ["coca-cola", "coca cola"]),
    ("PEP", "PepsiCo", "Consumer", ["pepsico", "pepsi"]),
    ("MCD", "McDonald's", "Consumer", ["mcdonald's", "mcdonald\u2019s", "mcdonalds"]),
    ("NKE", "Nike", "Consumer", ["nike"]),
    ("JPM", "JPMorgan Chase", "Banking", ["jpmorgan", "jp morgan"]),
    ("BAC", "Bank of America", "Banking", ["bank of america", "bofa"]),
    ("GS", "Goldman Sachs", "Banking", ["goldman sachs", "goldman"]),
    ("WFC", "Wells Fargo", "Banking", ["wells fargo"]),
    ("LLY", "Eli Lilly", "Healthcare", ["eli lilly", "lilly"]),
    ("UNH", "UnitedHealth", "Healthcare", ["unitedhealth", "united health"]),
    ("JNJ", "Johnson & Johnson", "Healthcare", ["johnson & johnson", "j&j"]),
    ("PFE", "Pfizer", "Healthcare", ["pfizer"]),
    ("XOM", "Exxon Mobil", "Energy", ["exxon"]),
    ("CVX", "Chevron", "Energy", ["chevron"]),
    ("BA", "Boeing", "Industrials", ["boeing"]),
    ("CAT", "Caterpillar", "Industrials", ["caterpillar"]),
]
UNIVERSE = [{"symbol": s, "name": n, "sector": sec, "aliases": a} for s, n, sec, a in _UNIVERSE]


# ---------------------------------------------------------------- time helpers
def et_parts(moment: datetime | None = None) -> dict:
    moment = (moment or datetime.now(timezone.utc)).astimezone(MARKET_TZ)
    return {"day": moment.strftime("%Y-%m-%d"), "minutes": moment.hour * 60 + moment.minute, "weekday": moment.strftime("%a")}


def in_session(moment: datetime | None = None) -> bool:
    p = et_parts(moment)
    return p["weekday"] not in ("Sat", "Sun") and SESSION_OPEN <= p["minutes"] < SQUARE_OFF


def news_window_start(now: datetime | None = None) -> datetime:
    """The overnight window opens at the 16:00 ET close of the previous session."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(MARKET_TZ).replace(hour=16, minute=0, second=0, microsecond=0)
    return day - timedelta(days=1) if now < day else day


def snapshot_due(snapshot: dict | None, now: datetime | None = None) -> bool:
    """A snapshot is due when missing, from an earlier ET day, or taken before today's 09:45 refresh."""
    now = now or datetime.now(timezone.utc)
    if not snapshot:
        return True
    p = et_parts(now)
    if snapshot.get("day") != p["day"]:
        return True
    updated = et_parts(datetime.fromisoformat(snapshot["updatedAt"]))
    return p["minutes"] >= MORNING_REFRESH and updated["minutes"] < MORNING_REFRESH


# ------------------------------------------------------------------- sentiment
POSITIVE = re.compile(r"\b(beat(?:s)?|surge[sd]?|gain[sd]?|rall(?:y|ies|ied)|rise[sn]?|rose|jump(?:s|ed)?|growth|upgrade[sd]?|record high|wins?|approval|approved|profit(?:s)?|dividend|buyback|strong|soar(?:s|ed)?|tops|outperform(?:s|ed)?)\b", re.I)
NEGATIVE = re.compile(r"\b(miss(?:es|ed)?|fall(?:s|en)?|fell|drop(?:s|ped)?|slump[sd]?|loss(?:es)?|downgrade[sd]?|probe|fraud|penalty|penalties|weak|decline[sd]?|crash(?:es|ed)?|selloff|plunge[sd]?|cuts?|concerns?|reject(?:s|ed)?|lawsuit|tumbl(?:e|es|ed)|sink(?:s)?|sank|layoffs?|recall(?:s|ed)?|warn(?:s|ed|ing)?)\b", re.I)
NEGATION = re.compile(r"\b(?:no|not|fails? to)\s+(?:\w+\s+)?(growth|approval|approved|profit|gain|beat|strong)\b")
PROFIT_DOWN = re.compile(r"\bprofit(?:s)?\s+(?:fall|fell|drop|declin|slump|plung|down|cut)|\b(?:fall|drop|decline|cut)\w*\s+in\s+profit")
LOSS_NARROW = re.compile(r"\bloss(?:es)?\s+(?:narrow|shrink|reduc)|\bnarrow\w*\s+loss")


def sentiment(title: str) -> dict:
    text = title.lower()
    text = re.sub(r"\bstop[-\s]+loss\b", "", text)
    text = re.sub(r"\bprofit[-\s]+booking\b", "selling", text)
    positive = len(POSITIVE.findall(text))
    negative = len(NEGATIVE.findall(text))
    if re.search(r"\bstocks? to buy\b|\bbuy (?:rating|call|recommendation)\b", text):
        positive += 1
    if re.search(r"\bsell (?:rating|call|recommendation)\b|\bselling\b", text):
        negative += 1
    for _ in NEGATION.finditer(text):
        positive = max(0, positive - 1)
        negative += 1
    if PROFIT_DOWN.search(text):
        positive = max(0, positive - 1)
        if negative == 0:
            negative += 1
    if LOSS_NARROW.search(text):
        negative = max(0, negative - 1)
        positive += 1
    score = max(-3, min(3, positive - negative))
    return {"score": score, "label": "Positive" if score > 0 else "Negative" if score < 0 else "Neutral"}


def matches_stock(title: str, stock: dict) -> bool:
    # Headlines usually say just "Meta"; match it only when capitalised so "meta-analysis" etc. don't count.
    if stock["symbol"] == "META" and re.search(r"(^|[^A-Za-z0-9])Meta(?![A-Za-z0-9-])", title):
        return True
    return any(re.search(rf"(^|[^a-z0-9]){re.escape(alias)}($|[^a-z0-9])", title, re.I) for alias in stock["aliases"])


def safe_url(value) -> str | None:
    try:
        parsed = urlparse(str(value).strip())
    except ValueError:
        return None
    return parsed.geturl() if parsed.scheme in ("http", "https") and parsed.netloc else None


def publisher_key(url: str | None, name: str) -> str:
    link = safe_url(url) if url else None
    if not link:
        return re.sub(r"[^a-z0-9]", "", re.sub(r"^the\s+", "", name.lower()))
    host = (urlparse(link).hostname or "").removeprefix("www.")
    for domain, key in (("cnbc.com", "cnbc"), ("marketwatch.com", "marketwatch"), ("dowjones.io", "marketwatch"),
                        ("investing.com", "investing-com"), ("yahoo.com", "yahoo-finance")):
        if host == domain or host.endswith("." + domain):
            return key
    return host


def news_confidence(news: list[dict], score: int, now: datetime | None = None) -> dict:
    """Heuristic headline-evidence percentage (not a probability of profit)."""
    now = now or datetime.now(timezone.utc)
    window_start = news_window_start(now)
    publishers: dict[str, dict] = {}
    for article in news:
        published = datetime.fromisoformat(article["publishedAt"])
        age = (now - published).total_seconds() * 1000
        if not article["score"] or age < 0 or published < window_start:
            continue
        key = article.get("publisherKey") or publisher_key(None, article["source"].split(" · via ")[0])
        ev = publishers.setdefault(key, {"votes": 0, "count": 0, "newest": math.inf})
        ev["votes"] += 1 if article["score"] > 0 else -1
        ev["count"] += 1
        ev["newest"] = min(ev["newest"], age)
    count = len(publishers)
    if not score or not count:
        return {"percent": None, "label": "No directional evidence", "publishers": count}
    direction = 1 if score > 0 else -1
    evs = list(publishers.values())
    agreement = sum((direction * e["votes"] / e["count"] + 1) / 2 for e in evs) / count
    freshness = sum(max(0, 1 - e["newest"] / (12 * 3600000)) for e in evs) / count
    breadth = min(count / 4, 1)
    cap = 49 if count == 1 else 74 if count == 2 else 90
    percent = min(cap, round(agreement * 60 + breadth * 25 + freshness * 15))
    return {
        "percent": percent, "label": "Strong" if percent >= 75 else "Moderate" if percent >= 50 else "Weak",
        "publishers": count, "agreementPct": round(agreement * 100), "freshnessPct": round(freshness * 100),
        "breadthPct": round(breadth * 100), "capped": percent == cap,
    }


# ------------------------------------------------------------------- RSS feeds
def parse_feed(xml_text: str, source: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    root = ET.fromstring(xml_text)
    items = root.findall("./channel/item")
    if not items and root.find("./channel") is None:
        raise ValueError("Feed contains no RSS articles")
    window_start = news_window_start(now)
    articles = []
    for item in items:
        src = item.find("source")
        publisher = (src.text or "").strip() if src is not None else ""
        raw_title = re.sub(r"<[^>]*>", "", html.unescape(item.findtext("title") or "")).strip()
        suffix = f" - {publisher}"
        title = (raw_title[: -len(suffix)] if publisher and raw_title.endswith(suffix) else raw_title)[:220]
        url = safe_url(item.findtext("link") or "")
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if not title or not url or published > now or published < window_start:
            continue
        articles.append({
            "title": title, "url": url, "publishedAt": published.astimezone(timezone.utc).isoformat(),
            "source": f"{publisher} · via {source['name']}" if publisher else source["name"],
            "publisherKey": publisher_key((src.get("url") if src is not None else None) or source["url"], publisher or source["name"]),
            **sentiment(title),
        })
    return articles


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return CACHE_DIR / f"{safe}.json"


def _read_cache(key: str, max_age_sec: float):
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - payload.get("ts", 0) <= max_age_sec:
            return payload.get("data")
    except Exception:  # noqa: BLE001
        return None
    return None


def _write_cache(key: str, data) -> None:
    try:
        _cache_path(key).write_text(
            json.dumps({"ts": time.time(), "data": data}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _get(url: str, as_json: bool = False, cache_key: str | None = None, ttl: float = 120.0):
    if cache_key:
        cached = _read_cache(cache_key, ttl)
        if cached is not None:
            return cached

    last_err = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=14)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else (1.5 * (2 ** attempt))
                time.sleep(min(wait, 8.0))
                last_err = requests.HTTPError(f"429 Client Error: Too Many Requests for url: {url}", response=response)
                continue
            response.raise_for_status()
            data = response.json() if as_json else response.text
            if cache_key:
                _write_cache(cache_key, data)
            return data
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(0.8 * (2 ** attempt))

    if cache_key:
        stale = _read_cache(cache_key, max_age_sec=86400 * 3)
        if stale is not None:
            return stale
    raise last_err


# ---------------------------------------------------------------------- quotes
def _chart(symbol: str, rng: str, interval: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urlquote(symbol, safe='')}?range={rng}&interval={interval}"
    ttl = 300.0 if interval == "1d" else 90.0
    data = _get(url, as_json=True, cache_key=f"chart_{symbol}_{rng}_{interval}", ttl=ttl)
    chart = data.get("chart") or {}
    result = (chart.get("result") or [None])[0]
    if not result or chart.get("error"):
        raise ValueError((chart.get("error") or {}).get("description") or "Price response missing")
    return result


def _candles(result: dict) -> list[dict]:
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, ts in enumerate(result.get("timestamp") or []):
        values = [(quote.get(k) or [])[i] if i < len(quote.get(k) or []) else None for k in ("open", "high", "low", "close")]
        if all(isinstance(v, (int, float)) and v > 0 for v in values):
            out.append({"time": datetime.fromtimestamp(ts, timezone.utc), "open": values[0], "high": values[1], "low": values[2], "close": values[3]})
    return out


def fetch_quote(symbol: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    result = _chart(symbol, "1mo", "1d")
    candles = _candles(result)
    meta = result.get("meta") or {}
    price, market_time = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if not isinstance(price, (int, float)) or not market_time or meta.get("currency") != "USD":
        raise ValueError("Missing USD price or timestamp")
    as_of = datetime.fromtimestamp(market_time, timezone.utc)
    today = et_parts(now)["day"]
    previous = [c for c in candles if et_parts(c["time"])["day"] < today]
    reference = previous[-1]["close"] if previous else None
    ranges = [max(c["high"] - c["low"], abs(c["high"] - previous[i]["close"]), abs(c["low"] - previous[i]["close"]))
              for i, c in enumerate(previous[1:])][-14:]
    atr = sum(ranges) / len(ranges) if len(ranges) >= 10 else None
    age = (now - as_of).total_seconds()
    return {
        "price": price, "asOf": as_of.isoformat(), "referenceClose": reference, "atr": atr,
        "changePct": (price / reference - 1) * 100 if reference else None,
        "fresh": et_parts(as_of)["day"] == today and 0 <= age <= 20 * 60,
        "candles": [{"time": c["time"].isoformat(), "close": c["close"]} for c in candles],
        "url": f"https://finance.yahoo.com/quote/{urlquote(symbol, safe='')}/",
    }


def fetch_intraday(symbol: str) -> list[dict]:
    """Five-minute candles for the last few sessions (used to replay stops, targets and the 15:45 ET square-off)."""
    return _candles(_chart(symbol, "5d", "5m"))


# ------------------------------------------------------------------- scenarios
def _tick(x: float) -> float:
    return round(x, 2)  # US equities tick in cents


def build_scenario(quote: dict | None, score: int, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    if not score or not quote or not quote.get("atr") or not quote.get("referenceClose"):
        return None
    direction = 1 if score > 0 else -1
    entry = _tick(quote["referenceClose"] + direction * quote["atr"] * 0.15)
    stop = _tick(entry - direction * quote["atr"] * 0.6)
    risk = direction * (entry - stop)
    if stop <= 0 or risk <= 0:
        return None
    target = _tick(entry + direction * risk * 2)
    if entry <= 0 or target <= 0:
        return None
    as_of = datetime.fromisoformat(quote["asOf"])
    fresh = 0 <= (now - as_of).total_seconds() <= 20 * 60 and et_parts(as_of)["day"] == et_parts(now)["day"]
    crossed = quote["price"] >= entry if direction > 0 else quote["price"] <= entry
    if not fresh:
        status = "Await fresh quote"
    elif not in_session(now):
        status = "Outside entry window"
    elif crossed:
        status = "Trigger already crossed"
    else:
        status = "Watch for breakout" if direction > 0 else "Watch for breakdown"
    return {
        "side": "LONG" if direction > 0 else "SHORT",
        "entryAction": "BUY" if direction > 0 else "SELL",
        "exitAction": "SELL" if direction > 0 else "BUY BACK",
        "entry": entry, "stop": stop, "target": target, "riskReward": direction * (target - entry) / risk,
        "status": status,
        "condition": (f"Illustrative {'buy then sell' if direction > 0 else 'sell then buy back'} scenario only: confirm a break and hold "
                      f"{'above' if direction > 0 else 'below'} entry using live broker prices and volume. Do not chase gaps. Exit at stop/target or by 15:45 ET; never carry overnight."
                      + (" Short selling requires broker eligibility and margin." if direction < 0 else "")),
    }


# ------------------------------------------------------------------ collection
def collect_market(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []

    def load(source: dict) -> dict:
        try:
            articles = parse_feed(_get(source["url"]), source, now)
            return {**source, "status": "ok", "count": len(articles), "articles": articles}
        except Exception as error:  # noqa: BLE001 - surfaced to the user as a source error
            errors.append(f"{source['name']}: {error}")
            return {**source, "status": "error", "count": 0, "error": str(error), "articles": []}

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        sources = list(pool.map(load, SOURCES))
    seen, articles = set(), []
    for a in sorted((a for s in sources for a in s["articles"]), key=lambda a: a["publishedAt"], reverse=True):
        key = re.sub(r"[^a-z0-9]", "", a["title"].lower())
        if key not in seen:
            seen.add(key)
            articles.append(a)
    candidates = []
    for stock in UNIVERSE:
        news = [a for a in articles if matches_stock(a["title"], stock)]
        if news:
            score = sum(a["score"] for a in news)
            candidates.append({**{k: v for k, v in stock.items() if k != "aliases"}, "news": news, "score": score,
                               "sentiment": "Positive" if score > 0 else "Negative" if score < 0 else "Neutral"})
    candidates.sort(key=lambda s: (-abs(s["score"]), -len(s["news"])))

    def price(stock: dict) -> None:
        time.sleep(0.35)
        try:
            stock["quote"] = fetch_quote(stock["symbol"], now)
        except Exception as error:  # noqa: BLE001
            stock["quoteError"] = str(error)
            errors.append(f"{stock['symbol']}: {error}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(price, candidates))
    index = None
    try:
        index = fetch_quote("^GSPC", now)
    except Exception as error:  # noqa: BLE001
        errors.append(f"S&P 500: {error}")
    return {
        "day": et_parts(now)["day"], "updatedAt": datetime.now(timezone.utc).isoformat(),
        "sources": [{k: v for k, v in s.items() if k != "articles"} for s in sources],
        "articles": articles, "stocks": candidates, "index": index, "errors": errors, "universeSize": len(UNIVERSE),
        "provider": "Yahoo Finance (unofficial public endpoint; delayed, availability not guaranteed)",
    }


def present_stocks(snapshot: dict, now: datetime | None = None) -> list[dict]:
    """Recompute time-dependent fields (confidence, scenario status) when the snapshot is read."""
    now = now or datetime.now(timezone.utc)
    return [{**s, "scenario": build_scenario(s.get("quote"), s["score"], now), "confidence": news_confidence(s["news"], s["score"], now)}
            for s in snapshot.get("stocks", [])]
