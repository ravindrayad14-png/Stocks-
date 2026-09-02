"""Data layer for the multi-agent stock dashboard.

Two modes:
  - demo: loads pre-built evidence bundles from demo_data/*.json (fully offline)
  - live: pulls NSE data via yfinance for tickers in universe.json, screens each
          cap-segment bucket by day-change, and builds normalized evidence
          bundles for the top N movers per bucket.

Both modes emit the SAME evidence-bundle shape so scoring.py / llm.py never
need to know which mode produced the data.
"""
import glob
import json
import os
import statistics
from datetime import datetime, timezone, timedelta

import envconfig

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def load_universe():
    path = os.path.join(BASE_DIR, "universe.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _empty_bundle(symbol, cap_segment):
    """Skeleton bundle — every field defaults to null and is tracked as a gap."""
    gaps = [
        "price.live", "price.day_open", "price.day_high", "price.day_low",
        "price.prev_close", "price.day_change_pct", "price.volume",
        "range_52w.high", "range_52w.low", "range_52w.pct_from_high",
        "range_52w.position_pct", "technicals.rvol", "technicals.price_vs_sma_pct",
        "technicals.window_return_pct", "technicals.swing_high", "technicals.swing_low",
        "technicals.day_range_position_pct", "technicals.trend",
        "analyst.consensus", "analyst.num_analysts", "analyst.buy_pct",
        "analyst.hold_pct", "analyst.sell_pct", "analyst.target_mean",
        "analyst.target_low", "analyst.target_high", "analyst.upside_pct",
        "news.total", "news.positive", "news.negative", "news.neutral",
    ]
    return {
        "symbol": symbol,
        "name": symbol,
        "cap_segment": cap_segment,
        "sector": None,
        "price": {
            "live": None, "day_open": None, "day_high": None, "day_low": None,
            "prev_close": None, "day_change_pct": None, "volume": None,
        },
        "range_52w": {"high": None, "low": None, "pct_from_high": None, "position_pct": None},
        "technicals": {
            "rvol": None, "price_vs_sma_pct": None, "window_return_pct": None,
            "swing_high": None, "swing_low": None, "day_range_position_pct": None,
            "trend": None,
        },
        "analyst": {
            "consensus": None, "num_analysts": None, "buy_pct": None, "hold_pct": None,
            "sell_pct": None, "target_mean": None, "target_low": None,
            "target_high": None, "upside_pct": None,
        },
        "news": {"total": None, "positive": None, "negative": None, "neutral": None, "recent": []},
        "data_gaps": gaps,
        "note": "Feed has no raw fundamental ratios (P/E, ROE, etc.) — not available in this bundle.",
        "as_of": now_ist_str(),
    }


# --------------------------------------------------------------------------
# DEMO MODE
# --------------------------------------------------------------------------

def load_demo_bundles():
    """Load every *.json bundle in demo_data/, grouped by cap_segment bucket."""
    buckets = {"large": [], "mid": [], "small": []}
    for path in sorted(glob.glob(os.path.join(BASE_DIR, "demo_data", "*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            bundle = json.load(f)
        seg = bundle.get("cap_segment", "mid")
        buckets.setdefault(seg, []).append(bundle)
    return buckets


# --------------------------------------------------------------------------
# LIVE MODE (yfinance)
# --------------------------------------------------------------------------

def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _pct(a, b):
    """Percent change of a relative to b, or None if not computable."""
    if a is None or b in (None, 0):
        return None
    try:
        return round((a - b) / b * 100, 2)
    except Exception:
        return None


def fetch_live_evidence(symbol, cap_segment):
    """Build a normalized evidence bundle for one NSE ticker via yfinance."""
    import yfinance as yf

    bundle = _empty_bundle(symbol, cap_segment)
    gaps = set(bundle["data_gaps"])

    ticker = yf.Ticker(symbol)
    info = _safe(lambda: ticker.info) or {}
    hist = _safe(lambda: ticker.history(period="1mo", interval="1d"))
    news = _safe(lambda: ticker.news) or []

    bundle["name"] = info.get("shortName") or info.get("longName") or symbol
    bundle["sector"] = info.get("sector")

    # --- price ---
    live = info.get("currentPrice") or info.get("regularMarketPrice")
    prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
    day_open = info.get("open") or info.get("regularMarketOpen")
    day_high = info.get("dayHigh") or info.get("regularMarketDayHigh")
    day_low = info.get("dayLow") or info.get("regularMarketDayLow")
    volume = info.get("volume") or info.get("regularMarketVolume")

    bundle["price"].update({
        "live": live, "day_open": day_open, "day_high": day_high, "day_low": day_low,
        "prev_close": prev_close, "day_change_pct": _pct(live, prev_close), "volume": volume,
    })
    for k in ("live", "day_open", "day_high", "day_low", "prev_close", "day_change_pct", "volume"):
        if bundle["price"][k] is not None:
            gaps.discard(f"price.{k}")

    # --- 52w range ---
    hi52 = info.get("fiftyTwoWeekHigh")
    lo52 = info.get("fiftyTwoWeekLow")
    pct_from_high = _pct(live, hi52)
    position_pct = None
    if live is not None and hi52 not in (None, 0) and lo52 is not None and hi52 != lo52:
        position_pct = round((live - lo52) / (hi52 - lo52) * 100, 2)
    bundle["range_52w"].update({
        "high": hi52, "low": lo52, "pct_from_high": pct_from_high, "position_pct": position_pct,
    })
    for k in ("high", "low", "pct_from_high", "position_pct"):
        if bundle["range_52w"][k] is not None:
            gaps.discard(f"range_52w.{k}")

    # --- technicals from history ---
    closes, vols = [], []
    if hist is not None and not hist.empty:
        closes = list(hist["Close"].dropna())
        vols = list(hist["Volume"].dropna())

    rvol = None
    if volume and len(vols) > 1:
        prior_avg = statistics.mean(vols[:-1]) if len(vols) > 1 else None
        if prior_avg:
            rvol = round(volume / prior_avg, 2)
    elif len(vols) > 1:
        prior_avg = statistics.mean(vols[:-1])
        if prior_avg:
            rvol = round(vols[-1] / prior_avg, 2)

    sma = statistics.mean(closes) if closes else None
    price_vs_sma_pct = _pct(live, sma) if live is not None else None
    window_return_pct = _pct(closes[-1], closes[0]) if len(closes) >= 2 else None
    swing_high = max(closes) if closes else None
    swing_low = min(closes) if closes else None

    day_range_position_pct = None
    if live is not None and day_high is not None and day_low is not None and day_high != day_low:
        day_range_position_pct = round((live - day_low) / (day_high - day_low) * 100, 2)

    trend = None
    if len(closes) >= 5:
        recent_slope = closes[-1] - closes[-5]
        if sma is not None and closes[-1] > sma and recent_slope > 0:
            trend = "up"
        elif sma is not None and closes[-1] < sma and recent_slope < 0:
            trend = "down"
        else:
            trend = "sideways"

    bundle["technicals"].update({
        "rvol": rvol, "price_vs_sma_pct": price_vs_sma_pct, "window_return_pct": window_return_pct,
        "swing_high": round(swing_high, 2) if swing_high else None,
        "swing_low": round(swing_low, 2) if swing_low else None,
        "day_range_position_pct": day_range_position_pct, "trend": trend,
    })
    for k in ("rvol", "price_vs_sma_pct", "window_return_pct", "swing_high", "swing_low",
              "day_range_position_pct", "trend"):
        if bundle["technicals"][k] is not None:
            gaps.discard(f"technicals.{k}")

    # --- analyst ---
    target_mean = info.get("targetMeanPrice")
    target_low = info.get("targetLowPrice")
    target_high = info.get("targetHighPrice")
    num_analysts = info.get("numberOfAnalystOpinions")
    consensus = info.get("recommendationKey")
    rec_mean = info.get("recommendationMean")  # 1=strong buy .. 5=strong sell (rough proxy)
    buy_pct = hold_pct = sell_pct = None
    if rec_mean is not None:
        # crude but bounded mapping from the 1-5 mean into an approximate split
        buy_pct = max(0, min(100, round((5 - rec_mean) / 4 * 100)))
        sell_pct = max(0, min(100, round((rec_mean - 1) / 4 * 100)))
        hold_pct = max(0, 100 - buy_pct - sell_pct)
    upside_pct = _pct(target_mean, live) if live is not None else None

    bundle["analyst"].update({
        "consensus": consensus, "num_analysts": num_analysts, "buy_pct": buy_pct,
        "hold_pct": hold_pct, "sell_pct": sell_pct, "target_mean": target_mean,
        "target_low": target_low, "target_high": target_high, "upside_pct": upside_pct,
    })
    for k in ("consensus", "num_analysts", "buy_pct", "hold_pct", "sell_pct",
              "target_mean", "target_low", "target_high", "upside_pct"):
        if bundle["analyst"][k] is not None:
            gaps.discard(f"analyst.{k}")

    # --- news ---
    recent = []
    pos = neg = neu = 0
    POS_WORDS = ("surge", "beat", "growth", "upgrade", "record", "profit", "rally", "wins", "strong", "gain")
    NEG_WORDS = ("fall", "miss", "downgrade", "loss", "probe", "lawsuit", "decline", "weak", "cut", "slump")
    for item in news[:8]:
        title = (item.get("title") or item.get("content", {}).get("title", "")) if isinstance(item, dict) else ""
        if not title:
            continue
        low = title.lower()
        if any(w in low for w in POS_WORDS):
            tone = "positive"; pos += 1
        elif any(w in low for w in NEG_WORDS):
            tone = "negative"; neg += 1
        else:
            tone = "neutral"; neu += 1
        recent.append({"title": title, "tone": tone})
    if recent:
        bundle["news"].update({"total": len(recent), "positive": pos, "negative": neg,
                                "neutral": neu, "recent": recent})
        gaps.discard("news.total"); gaps.discard("news.positive")
        gaps.discard("news.negative"); gaps.discard("news.neutral")

    bundle["data_gaps"] = sorted(gaps)
    return bundle


def screen_universe(shortlist_per_bucket=4):
    """Live mode: fetch every ticker, rank each bucket by |day change|, keep top N."""
    universe = load_universe()
    buckets = {}
    for segment, tickers in universe.items():
        bundles = []
        for symbol in tickers:
            try:
                b = fetch_live_evidence(symbol, segment)
                bundles.append(b)
            except Exception:
                continue
        bundles.sort(key=lambda b: abs(b["price"]["day_change_pct"] or 0), reverse=True)
        buckets[segment] = bundles[:shortlist_per_bucket]
    return buckets


def get_universe_buckets(mode, shortlist_per_bucket=4):
    if mode == "demo":
        return load_demo_bundles()
    return screen_universe(shortlist_per_bucket)
