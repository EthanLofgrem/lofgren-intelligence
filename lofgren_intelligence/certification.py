"""V1 certification: the gate that must pass before V2 construction.

    V1Ready = StructuredEvidence AND StructuredAnswers AND Provenance AND Contradictions
              AND Gaps AND TemporalScope AND CostLedger AND ResearchReceipts
              AND ProviderAbstraction AND MCPContracts AND E2ECertification

Each scenario is an end-to-end run on fixed, fictional fixtures, fully
offline (network providers are replaced by deterministic fakes), so the
certification is reproducible on any machine and in CI. Unit tests prove the
parts; this proves the whole path.
"""

from __future__ import annotations

import csv
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .adapters import (
    AdapterRegistry,
    DocumentAdapter,
    ImageryCatalogAdapter,
    OrbitalPassAdapter,
    SearchProvider,
    SearchResult,
    SensorAdapter,
    WebSearchAdapter,
)
from .discovery import Hypothesis, PromotionRefused, add_hypothesis, promote
from .evidence.types import ClaimStatus, ClaimType, Evidence, EvidenceKind, Source, SourceKind
from .intent.compiler import compile_intent
from .kernel.pipeline import RunResult, run_investigation
from .kernel.receipt import verify_receipt
from .kernel.state import export_state
from .models.provider import HeuristicProvider, ReasoningProvider
from .orbital.tle import TLE
from .schemas import validate_shape
from .verification.calibration import PredictionLog
from .verification.engine import Verifier

CERT_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
OBJECTIVE = "Is industrial construction in the Phoenix metro increasing?"
PHOENIX = (33.4484, -112.0740)

# All fixtures are fictional.
AGREE_AND_CONFLICT = {
    "journal (fictional)": "Industrial construction in the Phoenix metro increased sharply in 2026. Completed "
                           "industrial warehouse space in the Phoenix metro reached 14 million square feet in 2026.",
    "permits (fictional)": "Permit records show industrial construction in the Phoenix metro increased in 2026. "
                           "Completed industrial warehouse space in the Phoenix metro reached 14.2 million square feet in 2026.",
    "broker (fictional)": "Industrial construction in the Phoenix metro decreased in 2026 as developers paused new starts.",
}
SYNDICATED = {
    "wire (fictional)": "Industrial vacancy in the Phoenix metro fell to 6 percent in 2026, the lowest level in a decade, "
                        "as logistics tenants absorbed new warehouse space across the West Valley corridor.",
    "reprint (fictional)": "Industrial vacancy in the Phoenix metro fell to 6 percent in 2026, the lowest level in a decade, "
                           "as logistics tenants absorbed new warehouse space across the West Valley corridor.",
}
STALE = {"old report (fictional)": "Industrial construction in the Phoenix metro increased in 2019. "
                                    "Industrial construction in the Phoenix metro increased in 2019 according to permits."}
SCOPED = {
    "survey 2024 (fictional)": "Completed industrial warehouse space in the Phoenix metro reached 9 million square feet in 2024.",
    "survey 2026 (fictional)": "Completed industrial warehouse space in the Phoenix metro reached 14 million square feet in 2026.",
}
SUN_SYNC = TLE("SENTINEL-2A (SYNTHETIC)", 40697, datetime(2026, 9, 30, tzinfo=timezone.utc),
               98.5692, 340.0, 0.0001, 90.0, 270.0, 14.30818)


class FakeSearch(SearchProvider):
    name = "fake-search"

    def __init__(self, results: list[SearchResult] | None = None, fail: bool = False) -> None:
        self.results = results or []
        self.fail = fail

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        if self.fail:
            raise OSError("search provider offline")
        return self.results[:limit]


def _docs(texts: dict[str, str]) -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register(DocumentAdapter(texts=texts))
    return reg


def _run(objective: str, reg: AdapterRegistry, provider: ReasoningProvider | None = None, **kw) -> RunResult:
    return run_investigation(compile_intent(objective, **kw.pop("intent", {})), reg,
                             provider or HeuristicProvider(), verifier=Verifier(now=CERT_NOW), **kw)


@dataclass
class Check:
    scenario: str
    criteria: tuple[str, ...]
    passed: bool
    detail: str


def _check(fn: Callable[[], str], scenario: str, criteria: tuple[str, ...]) -> Check:
    try:
        return Check(scenario, criteria, True, fn())
    except AssertionError as exc:
        return Check(scenario, criteria, False, f"failed: {exc}")
    except Exception as exc:  # a crash is a failure, never a pass
        return Check(scenario, criteria, False, f"error: {type(exc).__name__}: {exc}")


# ---- scenarios --------------------------------------------------------------

def s_document_research() -> str:
    r = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
    assert r.completed, r.stopped_reason
    assert r.findings and len(r.findings) == len(r.contract.questions), "one finding per question"
    for f in r.findings:
        assert all(cid in r.graph.claims for cid in f.claim_ids), "findings reference only graph claims"
        assert validate_shape("finding", _plain(f)) == [], validate_shape("finding", _plain(f))
    verified = [c for c in r.graph.claims.values() if c.status == ClaimStatus.VERIFIED]
    assert verified, "some claim verified by independent sources"
    assert all(len(r.graph.sources_for_claim(c.id)) >= 2 for c in verified if c.sufficiency.startswith("quantitative"))
    return f"{len(r.findings)} typed findings, {len(verified)} verified claims, all cited"


def s_discovered_web_research() -> str:
    pages = {
        "https://www.phoenix.gov/permits?utm_source=x": "Permit records show industrial construction in the Phoenix "
                                                        "metro increased in 2026.",
        "https://example.org/blocked": "Industrial construction in the Phoenix metro increased in 2026.",
    }
    hits = [SearchResult("Permits", "https://www.phoenix.gov/permits?utm_source=x", "snippet"),
            SearchResult("Permits (dup)", "https://phoenix.gov/permits/", "snippet"),
            SearchResult("Blocked", "https://example.org/blocked", "snippet")]
    adapter = WebSearchAdapter(FakeSearch(hits), fetch=lambda u: pages[u],
                               robots_allowed=lambda u: "blocked" not in u)
    reg = AdapterRegistry()
    reg.register(adapter)
    r = _run(OBJECTIVE, reg)
    urls = {s.uri for s in r.graph.sources.values()}
    assert urls == {"https://phoenix.gov/permits"}, urls
    assert any("robots.txt" in g for g in r.gaps), "robots refusal recorded"
    assert adapter.history, "query history kept"
    gov = next(iter(r.graph.sources.values()))
    assert gov.quality >= 0.8, "government source classified"
    return f"discovered 1 page via search (duplicate collapsed, 1 refused by robots.txt), {len(adapter.history)} queries logged"


def s_conflicting_evidence() -> str:
    r = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
    inc = [c for c in r.graph.contradictions.values() if c.kind == "incompatible"]
    assert inc, "contradiction recorded"
    assert all(c.resolution for c in inc), "each contradiction says what would resolve it"
    assert any(c.status == ClaimStatus.CONTESTED for c in r.graph.claims.values())
    assert any(u.description.startswith("Resolve contradiction") for u in r.unknowns)
    return f"{len(inc)} incompatible contradiction(s), each with a resolution plan"


def s_syndicated_sources() -> str:
    r = _run("Is industrial vacancy in the Phoenix metro falling?", _docs(SYNDICATED))
    assert r.lineage and r.lineage[0].relation == "syndicated", "syndication detected"
    groups = {s.independence_group for s in r.graph.sources.values()}
    assert len(groups) == 1, "copies merged into one lineage"
    assert not any(c.status == ClaimStatus.VERIFIED for c in r.graph.claims.values()), "copies cannot verify"
    return "2 identical articles counted as 1 confirmation; nothing verified by duplicates"


def s_stale_evidence() -> str:
    r = _run(OBJECTIVE, _docs(STALE))
    stale = [c for c in r.graph.claims.values() if any(i.startswith("stale") for i in c.issues)]
    assert stale, "stale claims flagged"
    facts = [c for c in stale if c.claim_type != ClaimType.ATTRIBUTION]
    assert facts and all(c.status != ClaimStatus.VERIFIED for c in facts), "stale factual claims not verified"
    return (f"{len(stale)} claim(s) about 2019 flagged stale; factual ones downgraded "
            "(an attribution stays true as a record of what was said)")


def s_scope_mismatch() -> str:
    r = _run("How much industrial warehouse space was completed in the Phoenix metro?", _docs(SCOPED))
    kinds = {c.kind for c in r.graph.contradictions.values()}
    assert kinds == {"scope_mismatch"}, kinds
    assert all(c.scope.valid_from for c in r.graph.claims.values()), "every claim carries a period"
    return "2024 vs 2026 figures recorded as a scope difference, not a conflict"


def s_satellite_metadata() -> str:
    def stac(url, body):
        return {"features": [{"id": "S2A_FIXTURE", "bbox": [-112.2, 33.3, -111.9, 33.6],
                              "properties": {"datetime": "2026-09-20T18:00:00Z", "eo:cloud_cover": 2.5,
                                             "platform": "sentinel-2a", "proj:epsg": 32612}}]}

    reg = AdapterRegistry()
    reg.register(OrbitalPassAdapter(tles=[SUN_SYNC], start=CERT_NOW, hours=24))
    reg.register(ImageryCatalogAdapter(collections=("sentinel-2-l2a",), fetch_json=stac, now=CERT_NOW))
    r = _run(f"How is the land changing at {PHOENIX[0]}, {PHOENIX[1]}?", reg)
    scene = next(e for e in r.graph.evidence.values() if "scenes" in e.data).data["scenes"][0]
    for key in ("acquired_at", "cloud_cover_pct", "gsd_m", "footprint_bbox", "license", "provider"):
        assert scene.get(key) is not None, f"scene field {key}"
    orbit = next(e for e in r.graph.evidence.values() if "passes" in e.data)
    assert "not a provider acquisition schedule" in orbit.data["prediction_kind"]
    assert len(r.graph.calculations) >= 2, "pass and scene counts have calculation receipts"
    return "passes labelled as predictions; scenes normalized (time, cloud, resolution, footprint, license)"


def s_authorized_sensors() -> str:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sensors.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "sensor_id", "metric", "value", "unit", "calibrated_at"])
            w.writerow(["2026-09-01T00:00:00Z", "t-1", "temperature", "212", "F", "2026-06-01T00:00:00Z"])
            w.writerow(["2026-09-02T00:00:00Z", "t-1", "temperature", "32", "°F", "2026-06-01T00:00:00Z"])
            w.writerow(["2026-09-01T00:00:00Z", "x-1", "pressure", "5", "", ""])
        denied = AdapterRegistry()
        denied.register(SensorAdapter([path], authorized=False))
        r0 = _run("Is warehouse temperature changing?", denied)
        assert not r0.graph.evidence and any("authorized" in g for g in r0.gaps)
        ok = AdapterRegistry()
        ok.register(SensorAdapter([path], authorized=True))
        r1 = _run("Is warehouse temperature changing?", ok)
    ev = [e for e in r1.graph.evidence.values()]
    assert len(ev) == 1 and ev[0].data["unit"] == "°C", "°F normalized to °C"
    assert abs(ev[0].data["mean"] - 50.0) < 1e-6, ev[0].data["mean"]
    assert any("x-1/pressure" in g for g in r1.gaps), "unit-less series rejected with a reason"
    return "unauthorized readings refused; °F normalized to °C; unit-less series rejected"


def s_budget_exhaustion() -> str:
    r = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT), intent={"max_spend_usd": 0.01})
    assert not r.completed and "cap" in r.stopped_reason
    assert not r.graph.claims and r.charge is None, "nothing gathered, nothing charged"
    r2 = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT), plan_id="free")
    assert not r2.completed and "upgrade" in r2.stopped_reason
    return "over-cap job stopped before research with no charge; plan limits enforced"


def s_offline_behavior() -> str:
    def down(url, body):
        raise OSError("offline")

    reg = AdapterRegistry()
    reg.register(WebSearchAdapter(FakeSearch(fail=True)))
    reg.register(ImageryCatalogAdapter(collections=("sentinel-2-l2a",), fetch_json=down, now=CERT_NOW))
    r = _run(f"What changed on the land at {PHOENIX[0]}, {PHOENIX[1]}?", reg)
    assert r.completed, "run completes without the network"
    assert any("unavailable" in g for g in r.gaps), "outages become unknowns"
    return "search and imagery outages recorded as unknowns; run completed"


def s_mcp_contract() -> str:
    from .mcp.server import Server

    s = Server()
    init = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["version"] == __version__
    out = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "investigate", "arguments": {"objective": OBJECTIVE, "texts": AGREE_AND_CONFLICT}}})
    data = out["result"]["structuredContent"]
    assert data["findings"] and data["run_id"].startswith("RR-")
    rec = s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "get_receipt", "arguments": {"run_id": data["run_id"]}}})
    assert rec["result"]["structuredContent"]["intact"]
    return f"{len(s.handlers)} structured tools; investigate -> receipt intact"


def s_reproducible_receipt() -> str:
    a = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
    b = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
    assert a.receipt["inputs_hash"] == b.receipt["inputs_hash"], "same inputs"
    assert a.receipt["state_hash"] == b.receipt["state_hash"], "same inputs must give the same findings"
    assert verify_receipt(a.receipt)
    tampered = {**a.receipt, "claims": a.receipt["claims"][:-1]}
    assert not verify_receipt(tampered), "tampering detected"
    assert a.receipt["operations"] and a.ledger and a.ledger.entries, "every operation in the ledger"
    return f"inputs and state hashes reproduce; tampering detected; {len(a.ledger.entries)} ledger entries"


def s_hypothesis_guard() -> str:
    r = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
    hyp = Hypothesis("Industrial construction in the Phoenix metro increased sharply in 2026 thanks to data centers.",
                     originating=[u.id for u in r.unknowns[:1]])
    claim = add_hypothesis(r.graph, hyp)
    src = r.graph.add_source(Source(SourceKind.DOCUMENT, "extra", uri="inline:extra", quality=0.9))
    ev = r.graph.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, hyp.statement))
    r.graph.link(ev.id, claim.id, "supports")
    Verifier(now=CERT_NOW).verify(r.graph)
    assert claim.status == ClaimStatus.UNVERIFIED, claim.status
    r.graph.validate()  # the graph stays consistent with a hypothesis in it
    try:
        promote(claim)
        raise AssertionError("promotion allowed")
    except PromotionRefused:
        pass
    state = export_state(r)
    assert all(k["id"] != claim.id for k in state["known"])
    return "hypothesis with supporting text stays unverified; promotion refused; absent from the known map"


def s_calibration_infrastructure() -> str:
    with tempfile.TemporaryDirectory() as d:
        log = PredictionLog(Path(d) / "predictions.jsonl")
        r = run_investigation(compile_intent(OBJECTIVE), _docs(AGREE_AND_CONFLICT), HeuristicProvider(),
                              verifier=Verifier(now=CERT_NOW), prediction_log=log)
        pending = log.pending()
        assert len(pending) == len(r.graph.claims), "every stated confidence logged"
        log.resolve(pending[0]["claim_id"], True)
        summary = log.summary()
    assert summary["resolved"] == 1 and summary["brier"] is not None
    assert all(f.confidence_status == "provisional" for f in r.findings), "uncalibrated scores labelled provisional"
    return "confidences logged, outcomes recordable, Brier score computed, scores labelled provisional"


def s_provider_abstraction() -> str:
    class Canned(ReasoningProvider):
        name, version = "canned", "test"

        def extract_claims(self, text: str, objective: str) -> list[dict]:
            self.usage["calls"] += 1
            return [{"statement": "Industrial construction in the Phoenix metro increased in 2026.", "value": None,
                     "unit": "", "polarity": 1}]

    r = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT), provider=Canned())
    assert r.receipt["provider"]["name"] == "canned" and r.provider_info["usage"]["calls"] > 0
    return "any ReasoningProvider plugs in; provider name, version and usage land in the receipt"


SCENARIOS: tuple[tuple[str, Callable[[], str], tuple[str, ...]], ...] = (
    ("document research", s_document_research, ("StructuredEvidence", "StructuredAnswers", "Provenance")),
    ("discovered web research", s_discovered_web_research, ("Provenance",)),
    ("conflicting evidence", s_conflicting_evidence, ("Contradictions", "Gaps")),
    ("syndicated sources", s_syndicated_sources, ("Provenance",)),
    ("stale evidence", s_stale_evidence, ("TemporalScope",)),
    ("scope mismatch", s_scope_mismatch, ("TemporalScope", "Contradictions")),
    ("satellite metadata", s_satellite_metadata, ("StructuredEvidence",)),
    ("authorized sensors", s_authorized_sensors, ("StructuredEvidence", "Provenance")),
    ("budget exhaustion", s_budget_exhaustion, ("CostLedger",)),
    ("offline behavior", s_offline_behavior, ("Gaps",)),
    ("MCP contract", s_mcp_contract, ("MCPContracts",)),
    ("reproducible receipt", s_reproducible_receipt, ("ResearchReceipts", "CostLedger")),
    ("hypothesis guard", s_hypothesis_guard, ("StructuredAnswers",)),
    ("calibration infrastructure", s_calibration_infrastructure, ("StructuredAnswers",)),
    ("provider abstraction", s_provider_abstraction, ("ProviderAbstraction",)),
)
GATE = ("StructuredEvidence", "StructuredAnswers", "Provenance", "Contradictions", "Gaps", "TemporalScope",
        "CostLedger", "ResearchReceipts", "ProviderAbstraction", "MCPContracts")


def _plain(obj) -> dict:
    from .evidence.types import to_dict

    return to_dict(obj)


def run_certification() -> dict:
    checks = [_check(fn, name, crit) for name, fn, crit in SCENARIOS]
    criteria = {c: all(ch.passed for ch in checks if c in ch.criteria) for c in GATE}
    criteria["E2ECertification"] = all(ch.passed for ch in checks)
    return {
        "engine_version": __version__,
        "certified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scenarios": [ch.__dict__ for ch in checks],
        "criteria": criteria,
        "v1_ready": all(criteria.values()),
    }


def render_certification(cert: dict) -> str:
    lines = [f"Lofgren Intelligence {cert['engine_version']} — V1 certification", ""]
    for s in cert["scenarios"]:
        lines.append(f"  [{'PASS' if s['passed'] else 'FAIL'}] {s['scenario']}: {s['detail']}")
    lines.append("")
    for k, v in cert["criteria"].items():
        lines.append(f"  {'✓' if v else '✗'} {k}")
    lines.append("")
    lines.append("V1Ready = " + ("TRUE — V2 construction may begin" if cert["v1_ready"] else "FALSE — fix failures first"))
    return "\n".join(lines)
