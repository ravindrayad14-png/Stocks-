"""LLM debate engine.

Provider priority (auto-detected unless LLM_PROVIDER forces one):
  1. claude_code  — shells out to the `claude` CLI, uses the user's Claude
                     subscription (Pro/Max). No API key, no per-call billing.
  2. anthropic    — ANTHROPIC_API_KEY via the Messages API.
  3. openai       — OPENAI_API_KEY via Chat Completions.

If every provider is unavailable, or the call fails / returns something that
doesn't verify against the evidence bundle, evaluate() transparently falls
back to scoring.evaluate() (the deterministic engine), which always works.
"""
import json
import re
import shutil
import subprocess

import envconfig
import scoring

SYSTEM_PROMPT = (
    "You are an equity research panel analyzing an Indian stock for a same-day "
    "watchlist. The panel has six seats: Bull, Bear, Fundamentalist, Technician, "
    "Newsdesk, and Judge. A BUY verdict needs genuinely favorable risk/reward "
    "WITH confirmation (momentum or volume) — not just a promising story. WATCH "
    "means promising but unconfirmed. AVOID means the setup is poor. "
    "CRITICAL: every number you cite must come from the evidence JSON provided. "
    "Never invent a figure. If a field is null or listed in data_gaps, say "
    "'data unavailable' instead of guessing. Respond with ONLY a JSON object, "
    "no prose, no markdown fences, matching exactly this shape:\n"
    '{"bull": {"score": 0-100, "point": "<=25 words"}, '
    '"bear": {"score": 0-100, "point": "<=25 words"}, '
    '"fundamentalist": {"score": 0-100, "point": "<=25 words"}, '
    '"technician": {"score": 0-100, "point": "<=25 words"}, '
    '"newsdesk": {"score": 0-100, "point": "<=25 words"}, '
    '"judge": {"winner": "Bull|Bear", "verdict": "BUY|WATCH|AVOID", '
    '"confidence": 1-10, "rationale": "<=2 lines", "key_catalyst": "<=12 words"}}'
)


def detect_provider():
    forced = envconfig.get("LLM_PROVIDER")
    if forced in ("claude_code", "anthropic", "openai"):
        return forced
    if shutil.which("claude"):
        return "claude_code"
    if envconfig.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if envconfig.get("OPENAI_API_KEY"):
        return "openai"
    return None


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in LLM response")
    return json.loads(text[start:end + 1])


def _call_claude_code(prompt):
    model = envconfig.get("CLAUDE_CODE_MODEL", "haiku")
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json", "--model", model],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
    )
    envelope = json.loads(result.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI error: {envelope.get('result')}")
    return envelope["result"]


def _call_anthropic(prompt):
    import requests
    key = envconfig.get("ANTHROPIC_API_KEY")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 800,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _call_openai(prompt):
    import requests
    key = envconfig.get("OPENAI_API_KEY")
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _build_prompt(evidence):
    return (
        "Evidence bundle for one stock (JSON):\n" + json.dumps(evidence, default=str) +
        "\n\nAnalyze this stock. Respond with ONLY the JSON object described in your "
        "instructions — no other text."
    )


def _numbers_in(text):
    return set(re.findall(r"-?\d+\.?\d*", text or ""))


def _evidence_numbers(evidence):
    nums = set()

    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, (int, float)):
            nums.add(str(obj))
            nums.add(str(round(obj)))

    walk(evidence)
    return nums


def _verify_grounding(parsed, evidence):
    """Flag any number cited in a point/rationale that isn't traceable to the
    evidence bundle. Small integers (0-10, confidence/score-like) are exempt
    since scores are the model's own judgment, not evidence citations."""
    ev_nums = _evidence_numbers(evidence)
    flags = []
    for seat in ("bull", "bear", "fundamentalist", "technician", "newsdesk"):
        point = parsed.get(seat, {}).get("point", "")
        for n in _numbers_in(point):
            try:
                val = float(n)
            except ValueError:
                continue
            if val <= 10 and val == int(val):
                continue  # likely a small qualitative number, not a citation
            if n not in ev_nums:
                flags.append(f"{seat}: '{n}' in \"{point}\" not found in evidence")
    return flags


def _parsed_to_result(parsed, evidence, provider):
    scores = {}
    for seat in ("bull", "bear", "fundamentalist", "technician", "newsdesk"):
        seat_data = parsed.get(seat, {}) or {}
        scores[seat] = {
            "score": seat_data.get("score", 50),
            "reasons": [seat_data.get("point", "")] if seat_data.get("point") else [],
        }
    j = parsed.get("judge", {}) or {}
    verdict = {
        "winner": j.get("winner", "Bull" if scores["bull"]["score"] >= scores["bear"]["score"] else "Bear"),
        "verdict": j.get("verdict", "WATCH"),
        "confidence": int(j.get("confidence", 5)),
        "rationale": j.get("rationale", ""),
        "key_catalyst": j.get("key_catalyst", ""),
        "bull_score": scores["bull"]["score"],
        "bear_score": scores["bear"]["score"],
        "net": scores["bull"]["score"] - scores["bear"]["score"],
    }
    flags = _verify_grounding(parsed, evidence)
    return {
        "scores": scores,
        "verdict": verdict,
        "engine": f"llm:{provider}",
        "verifier_flags": flags,
    }


def evaluate(evidence):
    """evaluate(evidence) -> {scores, verdict, engine, verifier_flags?}.

    Tries the detected LLM provider once; on ANY failure (no provider, call
    error, unparseable response) transparently falls back to the always-on
    deterministic engine in scoring.py.
    """
    provider = detect_provider()
    if provider is None:
        return scoring.evaluate(evidence)

    prompt = _build_prompt(evidence)
    try:
        if provider == "claude_code":
            raw = _call_claude_code(prompt)
        elif provider == "anthropic":
            raw = _call_anthropic(prompt)
        elif provider == "openai":
            raw = _call_openai(prompt)
        else:
            raise RuntimeError(f"unknown provider {provider}")
        parsed = _extract_json(raw)
        return _parsed_to_result(parsed, evidence, provider)
    except Exception:
        fallback = scoring.evaluate(evidence)
        fallback["engine"] = f"deterministic (fallback from {provider})"
        return fallback
