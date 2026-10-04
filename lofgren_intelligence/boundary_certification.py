"""V1 -> V2 boundary certification (`lofgren certify-boundary`).

Executable evidence for every code-level term of the gate before V2 step 4:

    ClaimIdentitySafe  EvidenceIdentitySafe  GraphIntegritySafe  QuestionIsolationSafe  MultiQuestionSynthesisSafe
    KnowledgeMap2Validated  ReceiptSemanticCommitmentValidated  ProvenanceBoundaryValidated
    DiscoveryContext2Validated  AdversarialSuitePassing  V1Certified

Each scenario builds real state (local fixtures, the heuristic provider, no network, no model) and asserts what must
hold; a scenario passes only by returning, never by raising. A term is TRUE only if every scenario under it passed.
Known defects are pinned as failing scenarios: they keep their term FALSE until fixed, and say why.

The process terms (V2RegressionPassing, PackageGatePassing, GitHubCIPassing, WorkingTreeClean, ExactSHAPinned)
cannot be evidenced from inside the package; scripts/boundary_gate.py evaluates them, runs this suite, and is the
only place that prints V1ReadyForV2Step4. All data is fictional.
"""

from __future__ import annotations

import copy
import csv
import json
import operator
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from . import __version__
from .adapters import AdapterRegistry, SensorAdapter
from .certification import AGREE_AND_CONFLICT, CERT_NOW, OBJECTIVE, SCOPED, STALE, SYNDICATED, _docs, _run
from .certification import run_certification
from .discovery import ContextMismatch, DiscoveryObjective, Hypothesis, KnownFact, PromotionRefused, promote
from .discovery.context import AssuranceLevel, DiscoveryContext, KnowledgeMapRefused, thaw
from .discovery.errors import MalformedInput, NonFiniteValue, UnknownReference
from .discovery.frame import frame_problem
from .evidence import Claim, ClaimOrigin, ClaimStatus, ClaimType, Evidence, EvidenceGraph, EvidenceKind, Scope, Source, SourceKind
from .evidence.graph import GraphValidationError, HypothesisCollision, UnknownRelation
from .evidence.types import Contradiction, make_id
from .intent.compiler import Question
from .kernel.answers import answer
from .kernel.findings import FindingIntegrityError, build_findings, finding_problems, synthesis_finding
from .kernel.knowledge_map import (
    compute_fingerprints,
    export_knowledge_map,
    knowledge_map_problems,
    knowledge_state_hash,
)
from .kernel.receipt import canonical_hash, verify_receipt
from .kernel.state import export_state
from .verification.engine import Verifier

CODE_TERMS = ("ClaimIdentitySafe", "EvidenceIdentitySafe", "GraphIntegritySafe", "QuestionIsolationSafe",
              "MultiQuestionSynthesisSafe", "KnowledgeMap2Validated", "ReceiptSemanticCommitmentValidated",
              "ProvenanceBoundaryValidated", "DiscoveryContext2Validated", "AdversarialSuitePassing", "V1Certified")
PROCESS_TERMS = ("V2RegressionPassing", "PackageGatePassing", "GitHubCIPassing", "WorkingTreeClean", "ExactSHAPinned")
GATE_ORDER = ("ClaimIdentitySafe", "EvidenceIdentitySafe", "GraphIntegritySafe", "QuestionIsolationSafe",
              "MultiQuestionSynthesisSafe", "KnowledgeMap2Validated", "ProvenanceBoundaryValidated",
              "DiscoveryContext2Validated", "ReceiptSemanticCommitmentValidated", "V1Certified",
              "V2RegressionPassing", "AdversarialSuitePassing", "PackageGatePassing", "GitHubCIPassing",
              "WorkingTreeClean", "ExactSHAPinned")
PHOENIX_2026 = Scope("2026-01-01", "2026-12-31", "Phoenix")


@dataclass
class BoundaryCheck:
    term: str
    scenario: str
    passed: bool
    detail: str


_SCENARIOS: list[tuple[str, str, Callable[[], str]]] = []


def scenario(term: str, name: str) -> Callable[[Callable[[], str]], Callable[[], str]]:
    def register(fn: Callable[[], str]) -> Callable[[], str]:
        _SCENARIOS.append((term, name, fn))
        return fn
    return register


def refused(fn: Callable[[], object], *errors: type[BaseException]) -> BaseException:
    try:
        fn()
    except errors as exc:
        return exc
    raise AssertionError(f"accepted; expected {' or '.join(e.__name__ for e in errors)}")


# ---- shared state (built once per certification) ------------------------------

class _State:
    def __init__(self) -> None:
        self._cache: dict[str, object] = {}

    def get(self, key: str, build: Callable[[], object]):
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]


_S = _State()


def _phoenix():
    return _S.get("phoenix", lambda: _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT)))


def _phoenix_map() -> dict:
    return copy.deepcopy(_S.get("phoenix_map", lambda: export_knowledge_map(_phoenix())))


def _sensor_run(directory: Path):
    path = directory / "sensors.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "sensor_id", "metric", "value", "unit", "calibrated_at"])
        w.writerow(["2026-09-01T00:00:00Z", "t-1", "temperature", "212", "F", "2026-06-01T00:00:00Z"])
        w.writerow(["2026-09-02T00:00:00Z", "t-1", "temperature", "32", "F", "2026-06-01T00:00:00Z"])
    reg = AdapterRegistry()
    reg.register(SensorAdapter([path], authorized=True))
    return _run("Is warehouse temperature changing?", reg)


def _sensor():
    def build():
        with tempfile.TemporaryDirectory() as d:
            r = _sensor_run(Path(d))
        return r, export_knowledge_map(r)
    return _S.get("sensor", build)


def _forge(m: dict) -> dict:
    """Recompute everything a map computes about itself, as a forger who knows the algorithms would."""
    m["receipt"]["knowledge_state_hash"] = knowledge_state_hash(m)
    m["fingerprint"] = compute_fingerprints(m)
    return m


def _bound_problems(m: dict, receipt: dict | None = None) -> str:
    return " ".join(knowledge_map_problems(m, _phoenix().receipt if receipt is None else receipt))


def _question_run(*questions: Question):
    g = EvidenceGraph()
    src = g.add_source(Source(SourceKind.DOCUMENT, "fictional note", uri="inline:boundary", quality=0.8))
    run = SimpleNamespace(graph=g, gaps=[], unknowns=[], calibrated=False, findings=[],
                          contract=SimpleNamespace(questions=list(questions)))

    def claim(statement: str, qid: str, confidence: float = 0.8, status=ClaimStatus.VERIFIED) -> Claim:
        ev = g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, statement + " (source text)"))
        return g.add_claim(Claim(statement, question_id=qid, status=status, confidence=confidence), supported_by=[ev.id])
    return run, claim


def _q(qid: str, role: str = "state", depends_on: tuple[str, ...] = ()) -> Question:
    return Question(f"Fictional question {qid}?", ["text"], role=role, depends_on=list(depends_on), id=qid)


# ---- identity ----------------------------------------------------------------

@scenario("ClaimIdentitySafe", "claim identity v2")
def b_claim_identity() -> str:
    base = Claim("Warehouse vacancy rose (fictional).", scope=PHOENIX_2026)
    variants = {"geography": Claim(base.statement, scope=Scope("2026-01-01", "2026-12-31", "Tucson")),
                "period": Claim(base.statement, scope=Scope("2024-01-01", "2024-12-31", "Phoenix")),
                "value": Claim(base.statement, value=11.0, unit="%", scope=PHOENIX_2026),
                "subject": Claim(base.statement, subject="vacancy:flex", scope=PHOENIX_2026)}
    for name, other in variants.items():
        assert other.id != base.id, f"{name} does not change identity"
    assert Claim(base.statement.upper(), scope=PHOENIX_2026).id == base.id, "same proposition, two ids"
    assert Claim(base.statement, scope=PHOENIX_2026, question_id="Q-a").id == base.id, "question changed identity"
    return "scope, period, value and subject distinguish propositions; case and question do not"


@scenario("ClaimIdentitySafe", "polarity")
def b_polarity() -> str:
    a = Claim("Warehouse vacancy rose (fictional).", scope=PHOENIX_2026)
    b = Claim("Warehouse vacancy rose (fictional).", scope=PHOENIX_2026, polarity=-1)
    assert a.id != b.id, "a negation shares its affirmation's id"
    refused(lambda: Claim("x", polarity=0), ValueError)
    return "a negated proposition is a different claim; polarity outside {1, -1} is refused"


@scenario("ClaimIdentitySafe", "legacy claim identity v1")
def b_claim_v1() -> str:
    c = Claim("Warehouse vacancy rose (fictional).", identity_version=1)
    assert c.id == make_id("CL", "warehouse vacancy rose (fictional)."), c.id
    refused(lambda: Claim("x", identity_version=3), ValueError)
    return "v1 ids reproduce exactly; unknown identity versions are refused"


@scenario("EvidenceIdentitySafe", "evidence identity v2")
def b_evidence_identity() -> str:
    prefix = "x" * 200
    a = Evidence("SRC-a", EvidenceKind.DOCUMENT, prefix + " reading 21 C", observed_at="2026-09-01T00:00:00Z")
    b = Evidence("SRC-a", EvidenceKind.DOCUMENT, prefix + " reading 99 C", observed_at="2026-09-01T00:00:00Z")
    same = Evidence("SRC-a", EvidenceKind.DOCUMENT, prefix + " reading 21 C", observed_at="2026-09-01T00:00:00Z")
    assert a.id != b.id, "content beyond 200 characters ignored"
    assert a.id == same.id, "identical evidence has two ids"
    assert Evidence("SRC-b", EvidenceKind.DOCUMENT, a.content, observed_at=a.observed_at).id != a.id, "source ignored"
    return "full content and source distinguish evidence; identical evidence keeps one id"


@scenario("EvidenceIdentitySafe", "legacy evidence identity v1")
def b_evidence_v1() -> str:
    e = Evidence("SRC-a", EvidenceKind.DOCUMENT, "text", observed_at="2026-09-01", identity_version=1)
    assert e.id == make_id("EV", "SRC-a", "text", "2026-09-01"), e.id
    refused(lambda: Evidence("SRC-a", EvidenceKind.DOCUMENT, "t", identity_version=7), ValueError)
    return "v1 ids reproduce exactly; unknown identity versions are refused"


# ---- graph -------------------------------------------------------------------

def _small_graph():
    g = EvidenceGraph()
    src = g.add_source(Source(SourceKind.DOCUMENT, "note", uri="inline:g", quality=0.8))
    ev = g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, "Warehouse vacancy rose (fictional)."))
    c = g.add_claim(Claim("Warehouse vacancy rose (fictional).", scope=PHOENIX_2026), supported_by=[ev.id])
    return g, ev, c


@scenario("GraphIntegritySafe", "no dangling references")
def b_dangling() -> str:
    g, _, c = _small_graph()
    c.supporting.append("EV-missing0000")
    refused(g.validate, GraphValidationError)
    return "a claim citing missing evidence makes the graph invalid"


@scenario("GraphIntegritySafe", "no unknown relations")
def b_relation() -> str:
    g, ev, c = _small_graph()
    refused(lambda: g.link(ev.id, c.id, "mentions"), UnknownRelation)
    return "an edge relation outside the vocabulary is refused"


@scenario("GraphIntegritySafe", "no support/contradict dual edge")
def b_dual() -> str:
    g, ev, c = _small_graph()
    c.contradicting.append(ev.id)
    assert any("both supports and contradicts" in p for p in g.problems()), g.problems()
    return "evidence on both sides of one claim is a graph problem"


@scenario("GraphIntegritySafe", "hypothesis cannot become a verified claim")
def b_hypothesis_graph() -> str:
    g, ev, c = _small_graph()
    c.status = ClaimStatus.VERIFIED
    refused(lambda: g.add_claim(Claim(c.statement, scope=c.scope, origin=ClaimOrigin.HYPOTHESIS)), HypothesisCollision)
    g2, ev2, _ = _small_graph()
    hyp = g2.add_claim(Claim("A fictional untested idea.", origin=ClaimOrigin.HYPOTHESIS), supported_by=[ev2.id])
    Verifier(now=CERT_NOW).verify(g2)
    assert hyp.status == ClaimStatus.UNVERIFIED, hyp.status
    return "a hypothesis never merges into a verified claim and is never verified"


# ---- question isolation --------------------------------------------------------

@scenario("QuestionIsolationSafe", "unrelated stronger claim cannot leak")
def b_leak() -> str:
    run, claim = _question_run(_q("Q-A"), _q("Q-B"))
    a = claim("Phoenix vacancy rose (fictional).", "Q-A", 0.70)
    claim("Tucson permits: 40 (fictional).", "Q-B", 0.99)
    got = [c.id for c in answer(run.contract.questions[0], run).claims]
    assert got == [a.id], got
    return "question A answers from its own 0.70 claim, not B's 0.99 claim"


@scenario("QuestionIsolationSafe", "direct dependency works, undeclared fails")
def b_dependency() -> str:
    run, claim = _question_run(_q("Q-A", depends_on=("Q-B",)), _q("Q-B"), _q("Q-C"))
    b = claim("A claim for B (fictional).", "Q-B")
    c = claim("A claim for C (fictional).", "Q-C")
    got = {x.id for x in answer(run.contract.questions[0], run).claims}
    assert got == {b.id}, got
    f = next(f for f in build_findings(run) if f.question_id == "Q-A")
    assert (f.derivation, f.claim_scopes) == ("dependency", {b.id: ["Q-B"]}), (f.derivation, f.claim_scopes)
    assert c.id not in f.claim_ids and b.question_ids == ["Q-B"], "dependency use re-associated the claim"
    return "A uses B's claim through its declared edge, recorded as such; C's claim stays out"


@scenario("QuestionIsolationSafe", "dependencies are not transitive")
def b_transitive() -> str:
    run, claim = _question_run(_q("Q-A", depends_on=("Q-B",)), _q("Q-B", depends_on=("Q-C",)), _q("Q-C"))
    b = claim("A claim for B (fictional).", "Q-B")
    claim("A claim for C (fictional).", "Q-C", 0.99)
    got = [x.id for x in answer(run.contract.questions[0], run).claims]
    assert got == [b.id], got
    return "A -> B -> C gives A access to B only"


@scenario("QuestionIsolationSafe", "cycles are bounded and deterministic")
def b_cycle() -> str:
    run, claim = _question_run(_q("Q-A", depends_on=("Q-B",)), _q("Q-B", depends_on=("Q-A",)))
    claim("A claim for A (fictional).", "Q-A")
    claim("A claim for B (fictional).", "Q-B")
    first = [json.dumps(f.__dict__, default=str, sort_keys=True) for f in build_findings(run)]
    second = [json.dumps(f.__dict__, default=str, sort_keys=True) for f in build_findings(run)]
    assert first == second
    return "A <-> B terminates and gives the same findings twice"


@scenario("QuestionIsolationSafe", "contradiction scope is isolated")
def b_contradiction_scope() -> str:
    run, claim = _question_run(_q("Q-A", role="contradict"), _q("Q-B"))
    x = claim("Vacancy rose (fictional).", "Q-B", 0.6, ClaimStatus.CONTESTED)
    y = claim("Vacancy fell (fictional).", "Q-B", 0.6, ClaimStatus.CONTESTED)
    cx = run.graph.add_contradiction(Contradiction(x.id, y.id, "opposite directions"))
    f = next(f for f in build_findings(run) if f.question_id == "Q-A")
    assert cx.id not in f.contradiction_ids and not f.claim_ids, f
    return "a contradiction between another question's claims does not reach question A"


@scenario("MultiQuestionSynthesisSafe", "explicit synthesis records every named question")
def b_synthesis() -> str:
    run, claim = _question_run(_q("Q-A"), _q("Q-B"), _q("Q-C"), _q("Q-D"))
    a = claim("A claim for A (fictional).", "Q-A")
    b = claim("A claim for B (fictional).", "Q-B")
    c = claim("A claim for C (fictional).", "Q-C", 0.99)
    f = synthesis_finding(run, ["Q-B", "Q-A", "Q-D"])
    assert (f.derivation, f.question_ids) == ("synthesis", ["Q-A", "Q-B", "Q-D"]), (f.derivation, f.question_ids)
    assert set(f.claim_ids) == {a.id, b.id} and c.id not in f.claim_ids
    assert f.claim_scopes == {a.id: ["Q-A"], b.id: ["Q-B"]}, f.claim_scopes
    refused(lambda: synthesis_finding(run, ["Q-A"]), FindingIntegrityError)
    f.claim_ids.append(c.id)
    assert any(c.id in p for p in finding_problems([f], run)), "out-of-scope synthesis claim accepted"
    return "a synthesis draws only on named questions, records each claim's question, refuses tampering"


# ---- knowledge-map/2 ------------------------------------------------------------

@scenario("KnowledgeMap2Validated", "fingerprints")
def b_fingerprints() -> str:
    m = _phoenix_map()
    assert compute_fingerprints(m) == m["fingerprint"], "exact fingerprint does not recompute"
    again = export_knowledge_map(_run(OBJECTIVE, _docs(AGREE_AND_CONFLICT)))
    assert again["fingerprint"]["content"] == m["fingerprint"]["content"], "content fingerprint not reproducible"
    return "KM2 recomputes exactly; KM2C reproduces on a rerun of identical inputs"


@scenario("KnowledgeMap2Validated", "tampering, wrong run and wrong receipt are refused")
def b_map_binding() -> str:
    m = _phoenix_map()
    m["claims"][0]["issues"].append("edited")
    assert "altered after export" in _bound_problems(m)
    assert "differ from the receipt" in _bound_problems(_forge(m))
    w = _phoenix_map()
    w["research_id"] = w["receipt"]["research_id"] = "RR-" + "0" * 20
    assert "another run's map" in _bound_problems(_forge(w))
    other, _ = _sensor()
    assert "does not match the receipt" in _bound_problems(_phoenix_map(), other.receipt)
    return "edited, re-fingerprinted, renamed and mismatched maps all fail against the receipt"


@scenario("KnowledgeMap2Validated", "malformed values, dangling ids, unsupported schema")
def b_map_values() -> str:
    m = _forge(_phoenix_map())
    m["evidence"][0]["observed_at"] = "2026-13-45"
    assert "not an ISO 8601" in " ".join(knowledge_map_problems(_forge(m)))
    text = json.dumps(_phoenix_map()).replace('"quality": ', '"quality": NaN, "x": ', 1)
    assert "not a finite number" in " ".join(knowledge_map_problems(text))
    d = _phoenix_map()
    d["findings"][0]["claim_ids"].append("CL-missing00")
    assert "CL-missing00 is not in this map" in " ".join(knowledge_map_problems(_forge(d)))
    s = _phoenix_map()
    s["schema"] = "lofgren.knowledge-map/3"
    assert "expected 'lofgren.knowledge-map/2'" in " ".join(knowledge_map_problems(s))
    return "impossible dates, NaN, dangling ids and other schema versions are refused"


@scenario("ReceiptSemanticCommitmentValidated", "every exported field is committed")
def b_commitment_fields() -> str:
    edits = {
        "question_ids": lambda m: next(c for c in m["claims"] if len(c["question_ids"]) > 1)["question_ids"].pop(),
        "assessment": lambda m: m["claims"][0]["assessment"]["factors"].update(quality=0.01),
        "location": lambda m: m["evidence"][0].update(location={"lat": 33.45, "lon": -112.07, "name": "x"}),
        "transformations": lambda m: m["evidence"][0]["transformations"].append("silently rescaled"),
        "source quality": lambda m: m["sources"][0].update(quality=0.01),
    }
    for name, edit in edits.items():
        m = _phoenix_map()
        edit(m)
        assert "not the state the receipt committed to" in _bound_problems(_forge(m)), name
    return "editing question_ids, assessment, location, transformations or source quality fails the commitment"


@scenario("ReceiptSemanticCommitmentValidated", "receipt commitment tampering and legacy receipts")
def b_commitment_receipt() -> str:
    receipt = copy.deepcopy(_phoenix().receipt)
    receipt["knowledge_state_hash"] = "0" * 64
    assert not verify_receipt(receipt) and "not intact" in _bound_problems(_phoenix_map(), receipt)
    legacy = {k: v for k, v in _phoenix().receipt.items() if k not in ("knowledge_state_hash", "research_id")}
    legacy["schema"] = "lofgren.research-receipt/1"
    legacy["research_id"] = "RR-" + canonical_hash(legacy)[:20]
    assert verify_receipt(legacy), "a legacy receipt no longer verifies"
    assert "carries no knowledge_state_hash" in _bound_problems(_phoenix_map(), legacy)
    return "a changed commitment breaks the receipt; /1 receipts verify but cannot vouch for /2"


# ---- provenance -----------------------------------------------------------------

def _syndicated():
    def build():
        r = _run("Is industrial vacancy in the Phoenix metro falling?", _docs(SYNDICATED))
        return r, DiscoveryContext(export_knowledge_map(r), r.receipt)
    return _S.get("syndicated", build)


@scenario("ProvenanceBoundaryValidated", "evidence resolves to its source")
def b_evidence_source() -> str:
    r = _phoenix()
    ctx = DiscoveryContext(_phoenix_map(), r.receipt)
    for e in r.graph.evidence.values():
        assert ctx.source_of(e.id)["id"] == e.source_id
        assert ctx.evidence(e.id)["content_hash"] == e.content_hash
    return f"{len(r.graph.evidence)} evidence ids resolve to their records and sources"


@scenario("ProvenanceBoundaryValidated", "lineage is preserved; copies make no independence")
def b_lineage() -> str:
    r, ctx = _syndicated()
    links = [dict(x) for x in ctx.lineage()]
    assert links and links[0]["relation"] == "syndicated", links
    assert len({s["independence_group"] for s in ctx.entities("sources")}) == 1, "copies kept separate groups"
    assert not ctx.claims("known"), "a copy verified a claim"
    return "syndication is visible from V2; the copies share one group and verify nothing"


@scenario("ProvenanceBoundaryValidated", "missing lineage does not imply independence")
def b_missing_lineage() -> str:
    r = _phoenix()
    ctx = DiscoveryContext(_phoenix_map(), r.receipt)
    assert ctx.lineage() == (), ctx.lineage()
    assert {s["id"]: s["independence_group"] for s in ctx.entities("sources")} == \
        {s.id: s.independence_group for s in r.graph.sources.values()}, "the context altered independence groups"
    assert any("not thereby independent" in x for x in ctx.limitations), "the limitation is not stated"
    return "with no lineage the context keeps V1's groups and states that absence proves nothing"


# ---- DiscoveryContext/2 -----------------------------------------------------------

@scenario("DiscoveryContext2Validated", "assurance levels cannot be confused")
def b_assurance() -> str:
    r = _phoenix()
    v2 = DiscoveryContext(_phoenix_map(), r.receipt)
    v1 = DiscoveryContext(export_state(r))
    assert (v2.assurance.level, v1.assurance.level) == (AssuranceLevel.VALIDATED_V2, AssuranceLevel.DEGRADED_V1)
    refused(lambda: DiscoveryContext(export_state(r), r.receipt), MalformedInput)
    refused(lambda: DiscoveryContext(_phoenix_map()), MalformedInput)
    refused(lambda: v1.resolve(next(iter(r.graph.evidence))), UnknownReference)
    refused(lambda: v1.resolve(next(iter(r.graph.sources))), UnknownReference)
    assert v1.receipt is None and v1.content_fingerprint is None
    return "/2 is validated_v2 only with its receipt; /1 stays degraded_v1 and cannot resolve EV or SRC"


@scenario("DiscoveryContext2Validated", "immutable inputs and snapshots")
def b_immutable() -> str:
    r = _phoenix()
    m, receipt = _phoenix_map(), copy.deepcopy(r.receipt)
    ctx = DiscoveryContext(m, receipt)
    before = ctx.canonical_json()
    m["claims"][0]["statement"] = "changed"
    receipt["state_hash"] = "0" * 64
    ctx.snapshot()["claims"][0]["statement"] = "changed"
    ctx.resolve(ctx.entities("claims")[0]["id"]).value["statement"] = "changed"
    assert ctx.canonical_json() == before, "the context changed"
    refused(lambda: operator.setitem(ctx.evidence(ctx.entities("evidence")[0]["id"]), "content_hash", "x"), TypeError)
    return "changes to the input, the receipt, snapshots and resolved copies never reach the context"


@scenario("DiscoveryContext2Validated", "questions, derivation and V2 registration")
def b_context_v2() -> str:
    r, m = _sensor()
    ctx = DiscoveryContext(m, r.receipt)
    for c in r.graph.claims.values():
        assert list(ctx.resolve(c.id).value["question_ids"]) == c.question_ids
    dep = [f for f in r.findings if f.derivation == "dependency"]
    assert dep, "no dependency-derived finding in the fixture"
    for f in dep:
        rec = ctx.resolve(f.id).value
        assert (rec["derivation"], thaw(rec["claim_scopes"])) == (f.derivation, f.claim_scopes)
    p = _phoenix()
    pctx = DiscoveryContext(_phoenix_map(), p.receipt)
    framed = frame_problem(pctx, DiscoveryObjective("Find a warehouse opportunity (fictional)", pctx.research_id,
                                                    created_at="2026-09-30T12:00:00+00:00"))
    assert framed.frame is not None and framed.frame.id in pctx
    return "question associations and dependency derivation survive; V2 framing registers against /2"


@scenario("DiscoveryContext2Validated", "promotion rules hold under /2")
def b_promotion() -> str:
    r = _phoenix()
    ctx = DiscoveryContext(_phoenix_map(), r.receipt)
    u = (ctx.claims("uncertain") + ctx.claims("contradicted"))[0]
    refused(lambda: ctx.register(KnownFact(u["id"], u["statement"], thaw(u["scope"]), u["value"], u["unit"],
                                           u["policy"], u["confidence"])), PromotionRefused)
    k = ctx.claims("known")[0]
    refused(lambda: ctx.register(KnownFact(k["id"], k["statement"] + " (edited)", thaw(k["scope"]), k["value"],
                                           k["unit"], k["policy"], k["confidence"])), ContextMismatch)
    return "an uncertain or contested claim cannot stand as a known fact; a known fact must restate V1 exactly"


# ---- adversarial -----------------------------------------------------------------

@scenario("AdversarialSuitePassing", "fabricated source")
def a_fabricated_source() -> str:
    m = _phoenix_map()
    m["sources"].append({**copy.deepcopy(m["sources"][0]), "id": "SRC-fabricated", "title": "Invented authority"})
    assert "differ from the receipt" in _bound_problems(_forge(m))
    return "a source the run never retrieved is refused against the receipt"


@scenario("AdversarialSuitePassing", "copied evidence")
def a_copied() -> str:
    r, _ = _syndicated()
    assert not any(c.status == ClaimStatus.VERIFIED for c in r.graph.claims.values())
    return "two copies of one article verify nothing"


@scenario("AdversarialSuitePassing", "outdated evidence")
def a_outdated() -> str:
    r = _run(OBJECTIVE, _docs(STALE))
    flagged = [c for c in r.graph.claims.values() if any(i.startswith("stale") for i in c.issues)]
    facts = [c for c in flagged if c.claim_type != ClaimType.ATTRIBUTION]
    assert facts and not any(c.status == ClaimStatus.VERIFIED for c in facts), "a stale factual claim was verified"
    return ("claims about a long-ended period are flagged stale; factual ones are not verified (an attribution "
            "stays true as a record of what was said)")


@scenario("AdversarialSuitePassing", "future-dated evidence")
def a_future() -> str:
    def verdict(when: str):
        g = EvidenceGraph()
        c = g.add_claim(Claim("Industrial construction in the Phoenix metro increased in 2026 (fictional)."))
        ids = []
        for i in range(2):
            s = g.add_source(Source(SourceKind.DOCUMENT, f"report {i}", uri=f"inline:f{i}", publisher=f"p{i}",
                                    quality=0.8))
            e = g.add_evidence(Evidence(s.id, EvidenceKind.DOCUMENT, f"{c.statement} ({i})", observed_at=when))
            g.link(e.id, c.id, "supports")
            ids.append(e.id)
        v = Verifier(now=CERT_NOW)
        v.verify(g)
        return g, c, v.factors[c.id], ids

    future = CERT_NOW.replace(year=CERT_NOW.year + 5).isoformat()
    g, c, f, ids = verdict(future)
    assert c.status != ClaimStatus.VERIFIED, f"evidence dated {future[:10]} verified a claim on {CERT_NOW.date()}"
    assert (f.independent_sources, f.quality) == (0, 0.0), f"future evidence counted: {f}"
    assert all(e in g.evidence and e in c.supporting for e in ids), "future evidence was not preserved"
    flagged = [i for i in c.issues if i.startswith("future-dated:")]
    assert len(flagged) == 2 and all(any(e in i for i in flagged) for e in ids), c.issues
    _, near, nf, _ = verdict((CERT_NOW + timedelta(minutes=5)).isoformat())
    assert near.status == ClaimStatus.VERIFIED and nf.independent_sources == 2, "clock-skew tolerance not honoured"
    return ("evidence dated past the 5-minute skew tolerance is kept and flagged but supports nothing; within the "
            "tolerance it still counts")


@scenario("AdversarialSuitePassing", "wrong geography")
def a_geography() -> str:
    r = _run(OBJECTIVE, _docs(SCOPED))
    kinds = {cx.kind for cx in r.graph.contradictions.values()}
    assert kinds == {"scope_mismatch"}, kinds
    return "claims about different places are a scope difference, not a conflict"


@scenario("AdversarialSuitePassing", "conflicting evidence")
def a_conflict() -> str:
    r = _phoenix()
    contested = [c for c in r.graph.claims.values() if c.status == ClaimStatus.CONTESTED]
    assert contested and r.graph.contradictions, "conflict not surfaced"
    return f"{len(contested)} contested claims with {len(r.graph.contradictions)} recorded contradictions"


@scenario("AdversarialSuitePassing", "unsupported claim")
def a_unsupported() -> str:
    g = EvidenceGraph()
    c = g.add_claim(Claim("Vacancy fell to 2 percent (fictional)."))
    Verifier(now=CERT_NOW).verify(g)
    assert c.status == ClaimStatus.INSUFFICIENT, c.status
    return "a claim with no evidence is insufficient_evidence"


@scenario("AdversarialSuitePassing", "hypothesis as fact")
def a_hypothesis() -> str:
    hyp = Hypothesis("Warehouses near rail outperform (fictional).")
    refused(lambda: promote(hyp), PromotionRefused)
    r = _run(OBJECTIVE, _docs(AGREE_AND_CONFLICT))
    claim = r.graph.add_claim(hyp.as_claim())
    from .kernel.pipeline import _finish
    from .models.provider import HeuristicProvider
    _finish(r, HeuristicProvider())
    ctx = DiscoveryContext(export_knowledge_map(r), r.receipt)
    refused(lambda: ctx.reference(claim.id, ("any",)), PromotionRefused)
    return "a hypothesis cannot be promoted, and V2 cannot cite one even when V1 exports it"


@scenario("AdversarialSuitePassing", "cross-question leak")
def a_leak() -> str:
    r, m = _sensor()
    m = copy.deepcopy(m)
    qs = {q["id"]: q for q in m["questions"]}
    f, foreign = next((f, c["id"]) for f in m["findings"] for c in m["claims"]
                      if not set(c["question_ids"]) & ({f["question_id"]} | set(qs[f["question_id"]]["depends_on"])))
    f["claim_ids"].append(foreign)
    exc = refused(lambda: DiscoveryContext(_forge(m), r.receipt), KnowledgeMapRefused)
    assert "outside the questions this finding may use" in str(exc), exc
    return "a finding citing an undeclared question's claim is refused"


@scenario("AdversarialSuitePassing", "recomputed-map tampering")
def a_recomputed() -> str:
    m = _phoenix_map()
    m["claims"][0]["confidence"] = 0.99
    exc = refused(lambda: DiscoveryContext(_forge(m), _phoenix().receipt), KnowledgeMapRefused)
    assert "differ from the receipt" in str(exc) or "committed to" in str(exc), exc
    return "a map edited and fully re-fingerprinted still fails against its receipt"


@scenario("AdversarialSuitePassing", "receipt tampering")
def a_receipt() -> str:
    receipt = copy.deepcopy(_phoenix().receipt)
    receipt["claims"][0]["status"] = "verified"
    refused(lambda: DiscoveryContext(_phoenix_map(), receipt), KnowledgeMapRefused)
    return "an altered receipt is not intact and binds nothing"


@scenario("AdversarialSuitePassing", "malformed lineage")
def a_lineage() -> str:
    r, ctx = _syndicated()
    base = ctx.snapshot()
    for edit in (lambda m: m["lineage"][0].update(relation="copied"),
                 lambda m: m["lineage"][0].update(derived_from="SRC-missing0")):
        m = copy.deepcopy(base)
        edit(m)
        refused(lambda: DiscoveryContext(_forge(m), r.receipt), KnowledgeMapRefused)
    return "an unknown lineage relation or a link to a missing source is refused"


@scenario("AdversarialSuitePassing", "source-quality tampering")
def a_quality() -> str:
    m = _phoenix_map()
    m["sources"][0]["quality"] = 1.0 if m["sources"][0]["quality"] != 1.0 else 0.1
    exc = refused(lambda: DiscoveryContext(_forge(m), _phoenix().receipt), KnowledgeMapRefused)
    assert "committed to" in str(exc), exc
    return "raising a source's quality fails the receipt's knowledge-state commitment"


@scenario("AdversarialSuitePassing", "non-finite values")
def a_nonfinite() -> str:
    m = _phoenix_map()
    m["claims"][0]["confidence"] = float("inf")
    refused(lambda: DiscoveryContext(m, _phoenix().receipt), NonFiniteValue)
    return "Infinity in a map is refused before validation"


@scenario("V1Certified", "V1 certification")
def b_v1() -> str:
    cert = run_certification()
    failed = [s["scenario"] for s in cert["scenarios"] if not s["passed"]]
    assert cert["v1_ready"], f"V1Ready is FALSE: {failed}"
    return f"{len(cert['scenarios'])}/{len(cert['scenarios'])} V1 scenarios pass; V1Ready = TRUE"


# ---- runner ------------------------------------------------------------------------

def _run_one(term: str, name: str, fn: Callable[[], str]) -> BoundaryCheck:
    try:
        return BoundaryCheck(term, name, True, fn())
    except AssertionError as exc:
        return BoundaryCheck(term, name, False, f"failed: {exc}")
    except Exception as exc:  # a crash is a failure, never a pass
        return BoundaryCheck(term, name, False, f"error: {type(exc).__name__}: {exc}")


def run_boundary_certification() -> dict:
    _S._cache.clear()
    checks = [_run_one(*s) for s in _SCENARIOS]
    terms = {t: bool([c for c in checks if c.term == t]) and all(c.passed for c in checks if c.term == t)
             for t in CODE_TERMS}
    return {
        "engine_version": __version__,
        "certified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scenarios": [c.__dict__ for c in checks],
        "terms": terms,
        "code_terms_certified": all(terms.values()),
        "process_terms": list(PROCESS_TERMS),
    }


def render_boundary(cert: dict) -> str:
    lines = [f"Lofgren Intelligence {cert['engine_version']} — V1 -> V2 boundary certification", ""]
    for term in CODE_TERMS:
        lines.append(f"{term}")
        for s in cert["scenarios"]:
            if s["term"] == term:
                lines.append(f"  [{'PASS' if s['passed'] else 'FAIL'}] {s['scenario']}: {s['detail']}")
    lines.append("")
    for term, ok in cert["terms"].items():
        lines.append(f"  {'✓' if ok else '✗'} {term}")
    lines.append("")
    lines.append("Process terms (" + ", ".join(cert["process_terms"]) + ") are evaluated by scripts/boundary_gate.py.")
    lines.append("BoundaryCodeTerms = " + ("TRUE" if cert["code_terms_certified"] else "FALSE"))
    return "\n".join(lines)
