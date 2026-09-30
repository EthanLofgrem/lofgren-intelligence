"""Model providers: replaceable reasoning suppliers.

The platform never depends on one model. Every provider implements the same
small interface; the offline HeuristicProvider keeps the system working (and
testable) with no model at all, and model providers improve quality when
configured. A model's output is always stored as a claim to be verified,
never as truth.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from abc import ABC, abstractmethod

from ..evidence.types import topic_tokens

UP_WORDS = {"increase", "increased", "increases", "increasing", "rose", "rise", "rises", "rising", "grew", "grow",
            "grows", "growing", "expanded", "expands", "expanding", "gained", "doubled", "tripled", "surged"}
DOWN_WORDS = {"decrease", "decreased", "decreases", "decreasing", "fell", "falls", "falling", "declined", "declines",
              "declining", "shrank", "shrinks", "shrinking", "contracted", "dropped", "drops", "plunged", "slowed"}
_NEG = re.compile(r"\b(not|no|never|none|without|cannot|isn't|aren't|wasn't|weren't|didn't|doesn't|don't|hasn't|haven't)\b", re.I)
_NUM = re.compile(r"(\$)?(-?\d{1,3}(?:,\d{3})+|-?\d+(?:\.\d+)?)\s*(%|percent|million|billion|thousand|k\b|km|kg|acres|hectares|units|people|°c|°f|c\b|f\b)?", re.I)
_FACT_VERB = re.compile(r"\b(is|are|was|were|has|have|had|reported|reports|shows|showed|measured|operates|"
                        r"employs|produced|produces|reached|totals?|costs?|accounts?)\b", re.I)


def trend(tokens: set[str]) -> int:
    up, down = bool(tokens & UP_WORDS), bool(tokens & DOWN_WORDS)
    return 1 if up and not down else -1 if down and not up else 0


def parse_value(sentence: str) -> tuple[float | None, str]:
    for m in _NUM.finditer(sentence):
        raw = m.group(2).replace(",", "")
        # skip likely years standing alone
        if re.fullmatch(r"(19|20)\d{2}", raw) and not m.group(3) and not m.group(1):
            continue
        value = float(raw)
        unit = (m.group(3) or "").lower()
        if unit in ("thousand", "k"):
            value, unit = value * 1e3, ""
        elif unit == "million":
            value, unit = value * 1e6, ""
        elif unit == "billion":
            value, unit = value * 1e9, ""
        if m.group(1):
            unit = "usd"
        if unit == "percent":
            unit = "%"
        return value, unit
    return None, ""


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'$])", re.sub(r"\s+", " ", text.strip()))
    return [p.strip() for p in parts if p.strip()]


class ModelProvider(ABC):
    name = "provider"

    @abstractmethod
    def extract_claims(self, text: str, objective: str) -> list[dict]:
        """Return [{statement, value, unit, polarity}] factual claims found in text."""

    def summarize(self, question: str, findings: list[str]) -> str:
        if not findings:
            return "No verified finding answers this question yet."
        return " ".join(findings[:3])


class HeuristicProvider(ModelProvider):
    """Deterministic, offline claim extraction. No model required."""

    name = "heuristic"

    def extract_claims(self, text: str, objective: str) -> list[dict]:
        focus = set(topic_tokens(objective))
        claims: list[dict] = []
        for s in split_sentences(text):
            if s.endswith("?") or len(s) < 25 or len(s) > 400:
                continue
            toks = set(topic_tokens(s))
            value, unit = parse_value(s)
            factual = value is not None or trend(toks) != 0 or _FACT_VERB.search(s)
            if not factual or len(toks) < 3:
                continue
            if focus and not (toks & focus):
                continue
            claims.append({
                "statement": s.rstrip(),
                "value": value,
                "unit": unit,
                "polarity": -1 if _NEG.search(s) else 1,
            })
        return claims


class AnthropicProvider(ModelProvider):
    """Claude via the Messages API. Falls back to heuristics on any failure."""

    name = "anthropic"
    endpoint = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 60.0) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("LOFGREN_MODEL", "claude-sonnet-5-5")
        self.timeout = timeout
        self.fallback = HeuristicProvider()

    def _call(self, system: str, user: str, max_tokens: int = 2000) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode(), method="POST",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed host
            payload = json.loads(resp.read().decode())
        return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")

    def extract_claims(self, text: str, objective: str) -> list[dict]:
        if not self.api_key:
            return self.fallback.extract_claims(text, objective)
        system = ("You extract atomic, checkable factual claims from a source passage. Only include claims "
                  "stated in the passage; never add knowledge. Respond with a JSON array only.")
        user = (f"Objective: {objective}\n\nPassage:\n{text[:6000]}\n\nReturn a JSON array of objects with keys "
                "statement (one self-contained sentence), value (number or null), unit (string), "
                "polarity (1, or -1 if the claim is a negation). Include only claims relevant to the objective.")
        try:
            raw = self._call(system, user)
            data = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
            out = []
            for d in data:
                if isinstance(d, dict) and d.get("statement"):
                    out.append({"statement": str(d["statement"]), "value": d.get("value"),
                                "unit": str(d.get("unit") or ""), "polarity": -1 if d.get("polarity") == -1 else 1})
            return out
        except Exception:
            return self.fallback.extract_claims(text, objective)


def default_provider() -> ModelProvider:
    return AnthropicProvider() if os.environ.get("ANTHROPIC_API_KEY") else HeuristicProvider()
