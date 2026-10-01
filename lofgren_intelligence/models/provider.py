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


_NAME_WORDS = {"loop", "route", "highway", "interstate", "i", "sr", "us", "state", "road", "suite", "unit",
               "building", "phase", "section", "chapter", "page", "gate", "exit", "lot", "block", "tract", "no.",
               "number", "model", "version", "series", "level", "zone", "district", "ward", "precinct"}


def trend(tokens: set[str]) -> int:
    up, down = bool(tokens & UP_WORDS), bool(tokens & DOWN_WORDS)
    return 1 if up and not down else -1 if down and not up else 0


def parse_value(sentence: str) -> tuple[float | None, str]:
    for m in _NUM.finditer(sentence):
        raw = m.group(2).replace(",", "")
        before = sentence[:m.start()].rstrip().rsplit(" ", 1)[-1].lower().strip("(")
        if before in _NAME_WORDS and not m.group(3) and not m.group(1):
            continue  # "Loop 303", "Route 66", "Phase 2": names, not measurements
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


class ReasoningProvider(ABC):
    """Interface every reasoning supplier implements: a hosted model, a local
    model, or deterministic code. The kernel depends only on this."""

    name = "provider"
    version = "0"

    def __init__(self) -> None:
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "fallbacks": 0}

    def capabilities(self) -> set[str]:
        return {"claim_extraction"}

    def describe(self) -> dict:
        return {"name": self.name, "version": self.version, "capabilities": sorted(self.capabilities()),
                "usage": dict(self.usage)}

    @abstractmethod
    def extract_claims(self, text: str, objective: str) -> list[dict]:
        """Return [{statement, value, unit, polarity}] factual claims found in text."""


ModelProvider = ReasoningProvider  # backwards-compatible name


class HeuristicProvider(ReasoningProvider):
    """Deterministic, offline claim extraction. No model required."""

    name = "heuristic"
    version = "1.1"

    def capabilities(self) -> set[str]:
        return {"claim_extraction", "deterministic", "offline"}

    def extract_claims(self, text: str, objective: str) -> list[dict]:
        self.usage["calls"] += 1
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


EXTRACTION_SYSTEM = ("You extract atomic, checkable factual claims from a source passage. Only include claims "
                     "stated in the passage, in the passage's own words where possible; never add knowledge. "
                     "Respond with a JSON array only.")
EXTRACTION_TEMPLATE_VERSION = "extract-v1"


def extraction_prompt(text: str, objective: str) -> str:
    return (f"Objective: {objective}\n\nPassage:\n{text[:6000]}\n\nReturn a JSON array of objects with keys "
            "statement (one self-contained sentence), value (number or null), unit (string), "
            "polarity (1, or -1 if the claim is a negation). Include only claims relevant to the objective.")


def parse_claims_json(raw: str) -> list[dict]:
    data = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
    out = []
    for d in data:
        if isinstance(d, dict) and d.get("statement"):
            value = d.get("value")
            out.append({"statement": str(d["statement"]),
                        "value": float(value) if isinstance(value, (int, float)) else None,
                        "unit": str(d.get("unit") or ""), "polarity": -1 if d.get("polarity") == -1 else 1})
    return out


class _LLMProvider(ReasoningProvider):
    """Shared behaviour: structured extraction with a deterministic fallback."""

    def __init__(self) -> None:
        super().__init__()
        self.fallback = HeuristicProvider()

    def capabilities(self) -> set[str]:
        return {"claim_extraction", "structured_output"}

    def configured(self) -> bool:
        return True

    def _call(self, system: str, user: str, max_tokens: int = 2000) -> str:
        raise NotImplementedError

    def extract_claims(self, text: str, objective: str) -> list[dict]:
        if not self.configured():
            self.usage["fallbacks"] += 1
            return self.fallback.extract_claims(text, objective)
        try:
            self.usage["calls"] += 1
            return parse_claims_json(self._call(EXTRACTION_SYSTEM, extraction_prompt(text, objective)))
        except Exception:
            self.usage["fallbacks"] += 1
            return self.fallback.extract_claims(text, objective)


class AnthropicProvider(_LLMProvider):
    """Claude via the Messages API."""

    name = "anthropic"
    endpoint = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 60.0) -> None:
        super().__init__()
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("LOFGREN_MODEL", "claude-sonnet-5-5")
        self.version = self.model
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.api_key)

    def _call(self, system: str, user: str, max_tokens: int = 2000) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode(), method="POST",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed host
            payload = json.loads(resp.read().decode())
        u = payload.get("usage", {})
        self.usage["input_tokens"] += int(u.get("input_tokens", 0))
        self.usage["output_tokens"] += int(u.get("output_tokens", 0))
        return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


class OpenAICompatibleProvider(_LLMProvider):
    """Any OpenAI-compatible chat-completions endpoint: hosted APIs or a local
    model server (for example Ollama at http://localhost:11434/v1)."""

    name = "openai-compatible"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float = 120.0) -> None:
        super().__init__()
        self.base_url = (base_url or os.environ.get("LOFGREN_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LOFGREN_API_KEY", "")
        self.model = model or os.environ.get("LOFGREN_MODEL", "")
        self.version = self.model
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def _call(self, system: str, user: str, max_tokens: int = 2000) -> str:
        body = {"model": self.model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - user-configured endpoint
            payload = json.loads(resp.read().decode())
        u = payload.get("usage", {})
        self.usage["input_tokens"] += int(u.get("prompt_tokens", 0))
        self.usage["output_tokens"] += int(u.get("completion_tokens", 0))
        return payload["choices"][0]["message"]["content"]


def default_provider() -> ReasoningProvider:
    """LOFGREN_PROVIDER = anthropic | openai-compatible | heuristic (default: anthropic if a key exists)."""
    choice = os.environ.get("LOFGREN_PROVIDER", "").lower()
    if choice == "openai-compatible":
        return OpenAICompatibleProvider()
    if choice == "heuristic":
        return HeuristicProvider()
    if choice == "anthropic" or os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    return HeuristicProvider()
