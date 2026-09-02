"""Deterministic, rule-based scoring engine.

This is the ALWAYS-WORKS fallback — no LLM, no API key, no network needed.
It implements the same evaluate(evidence) -> result contract used by llm.py,
so app.py never has to know which engine actually produced a verdict.

Grounding rule: every number cited in a `reasons` string must come straight
from the evidence bundle. Nothing here is invented.
"""


def _g(d, *path, default=None):
    """Safe nested getter, e.g. _g(evidence, 'technicals', 'rvol')."""
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur or cur[key] is None:
            return default
        cur = cur[key]
    return cur


def score_bull(evidence):
    score = 0
    reasons = []

    rvol = _g(evidence, "technicals", "rvol")
    if rvol is not None and rvol > 1:
        add = min(20, round((rvol - 1) * 10))
        score += add
        reasons.append(f"RVOL {rvol}x average volume")

    pos52 = _g(evidence, "range_52w", "position_pct")
    if pos52 is not None and pos52 >= 85:
        score += 20
        reasons.append(f"near 52w high ({pos52}% of range)")

    price_vs_sma = _g(evidence, "technicals", "price_vs_sma_pct")
    trend = _g(evidence, "technicals", "trend")
    if price_vs_sma is not None and price_vs_sma > 0 and trend == "up":
        score += 15
        reasons.append(f"price {price_vs_sma}% above SMA, trend up")

    day_pos = _g(evidence, "technicals", "day_range_position_pct")
    if day_pos is not None and day_pos >= 70:
        score += 10
        reasons.append(f"closing strong in day range ({day_pos}%)")

    upside = _g(evidence, "analyst", "upside_pct")
    if upside is not None and upside >= 10:
        score += 15
        reasons.append(f"{upside}% upside to analyst target")

    buy_pct = _g(evidence, "analyst", "buy_pct")
    if buy_pct is not None and buy_pct >= 80:
        score += 10
        reasons.append(f"{buy_pct}% analyst buy consensus")

    news_pos = _g(evidence, "news", "positive")
    news_neg = _g(evidence, "news", "negative")
    if news_pos is not None and (news_neg is None or news_pos > news_neg):
        score += 5
        reasons.append(f"{news_pos} positive news items")

    window_ret = _g(evidence, "technicals", "window_return_pct")
    if window_ret is not None and window_ret > 0:
        score += 5
        reasons.append(f"{window_ret}% return over the window")

    return min(100, score), reasons


def score_bear(evidence):
    score = 0
    reasons = []

    rvol = _g(evidence, "technicals", "rvol")
    if rvol is not None and rvol < 1:
        score += 15
        reasons.append(f"RVOL {rvol}x — below-average volume")

    pos52 = _g(evidence, "range_52w", "position_pct")
    if pos52 is not None and pos52 < 30:
        score += 20
        reasons.append(f"near 52w low ({pos52}% of range)")

    price_vs_sma = _g(evidence, "technicals", "price_vs_sma_pct")
    trend = _g(evidence, "technicals", "trend")
    if (price_vs_sma is not None and price_vs_sma < 0) or trend == "down":
        score += 15
        reasons.append("below SMA / downtrend")

    upside = _g(evidence, "analyst", "upside_pct")
    if upside is not None and upside <= 0:
        score += 15
        reasons.append(f"{upside}% — no headroom to analyst target")

    buy_pct = _g(evidence, "analyst", "buy_pct")
    if buy_pct is not None and buy_pct < 55:
        score += 10
        reasons.append(f"only {buy_pct}% analyst buy consensus")

    pct_from_high = _g(evidence, "range_52w", "pct_from_high")
    if pct_from_high is not None and pct_from_high <= -20:
        score += 10
        reasons.append(f"{pct_from_high}% off 52w high")

    sell_pct = _g(evidence, "analyst", "sell_pct")
    if sell_pct is not None and sell_pct >= 25:
        score += 10
        reasons.append(f"{sell_pct}% analyst sell rating")

    news_neg = _g(evidence, "news", "negative")
    news_pos = _g(evidence, "news", "positive")
    if news_neg is not None and (news_pos is None or news_neg > news_pos):
        score += 5
        reasons.append(f"{news_neg} negative news items")

    day_pos = _g(evidence, "technicals", "day_range_position_pct")
    if day_pos is not None and day_pos < 30:
        score += 10
        reasons.append(f"weak close in day range ({day_pos}%)")

    return min(100, score), reasons


def judge_verdict(evidence, bull_score, bear_score):
    net = bull_score - bear_score
    pos52 = _g(evidence, "range_52w", "position_pct")
    rvol = _g(evidence, "technicals", "rvol")
    leadership = (pos52 is not None and pos52 >= 60) or (rvol is not None and rvol >= 3)

    if net >= 25 and leadership:
        verdict = "BUY"
    elif net <= -15:
        verdict = "AVOID"
    else:
        verdict = "WATCH"

    confidence = max(1, min(10, round(4 + net / 15)))
    if verdict == "BUY":
        confidence = max(confidence, 7)
    else:
        confidence = min(confidence, 6)

    winner = "Bull" if bull_score >= bear_score else "Bear"

    catalyst = None
    upside = _g(evidence, "analyst", "upside_pct")
    if rvol is not None and rvol >= 2:
        catalyst = f"volume spike ({rvol}x)"
    elif pos52 is not None and pos52 >= 85:
        catalyst = "breakout near 52w high"
    elif upside is not None and upside >= 10:
        catalyst = f"{upside}% upside to target"
    else:
        catalyst = "no single dominant catalyst — mixed signals"

    rationale = (
        f"Bull {bull_score}/100 vs Bear {bear_score}/100 (net {net}). "
        f"{'Leadership confirmed' if leadership else 'No leadership confirmation'}."
    )

    return {
        "winner": winner,
        "verdict": verdict,
        "confidence": confidence,
        "rationale": rationale,
        "key_catalyst": catalyst,
        "bull_score": bull_score,
        "bear_score": bear_score,
        "net": net,
    }


def evaluate(evidence):
    """Deterministic evaluate(evidence) -> {scores, verdict}. Always succeeds."""
    bull_score, bull_reasons = score_bull(evidence)
    bear_score, bear_reasons = score_bear(evidence)

    fundamentalist_reasons = []
    upside = _g(evidence, "analyst", "upside_pct")
    consensus = _g(evidence, "analyst", "consensus")
    if upside is not None:
        fundamentalist_reasons.append(f"{upside}% upside to mean target")
    if consensus is not None:
        fundamentalist_reasons.append(f"consensus: {consensus}")
    fundamentalist_score = max(0, min(100, round(50 + (upside or 0) * 2)))

    technician_reasons = []
    trend = _g(evidence, "technicals", "trend")
    rvol = _g(evidence, "technicals", "rvol")
    if trend:
        technician_reasons.append(f"trend: {trend}")
    if rvol is not None:
        technician_reasons.append(f"RVOL {rvol}x")
    technician_score = bull_score if trend == "up" else (bear_score if trend == "down" else 50)

    newsdesk_reasons = []
    news_pos = _g(evidence, "news", "positive")
    news_neg = _g(evidence, "news", "negative")
    if news_pos is not None or news_neg is not None:
        newsdesk_reasons.append(f"{news_pos or 0} positive / {news_neg or 0} negative headlines")
    newsdesk_score = 50 + 10 * ((news_pos or 0) - (news_neg or 0))
    newsdesk_score = max(0, min(100, newsdesk_score))

    verdict = judge_verdict(evidence, bull_score, bear_score)

    return {
        "scores": {
            "bull": {"score": bull_score, "reasons": bull_reasons},
            "bear": {"score": bear_score, "reasons": bear_reasons},
            "fundamentalist": {"score": fundamentalist_score, "reasons": fundamentalist_reasons},
            "technician": {"score": technician_score, "reasons": technician_reasons},
            "newsdesk": {"score": newsdesk_score, "reasons": newsdesk_reasons},
        },
        "verdict": verdict,
        "engine": "deterministic",
    }
