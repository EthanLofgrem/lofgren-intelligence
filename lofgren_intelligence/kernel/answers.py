"""Answer each contract question from the verified evidence graph.

A question's role decides which claims answer it: the leading verified
state, the claims that support it, the contested ones, the gaps, prior work,
or (for Lofgren Enterprise ventures) the findings relevant to each stage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..evidence.types import Claim, ClaimStatus, topic_tokens
from ..intent.compiler import PRIOR_WORDS, STAGE_KEYWORDS, Question

if TYPE_CHECKING:
    from .pipeline import RunResult

CONFIRMED = (ClaimStatus.VERIFIED, ClaimStatus.PARTIALLY_VERIFIED)
MAX_ITEMS = 5


@dataclass
class Answer:
    question: Question
    claims: list[Claim] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        if self.question.role == "gap":
            return True
        if self.question.role == "contradict":
            return True  # "none found" is itself an answer once research ran
        return any(c.status in CONFIRMED for c in self.claims)


def _ranked(claims: list[Claim]) -> list[Claim]:
    order = {ClaimStatus.VERIFIED: 0, ClaimStatus.PARTIALLY_VERIFIED: 1, ClaimStatus.CONTESTED: 2,
             ClaimStatus.SUPPORTED: 3, ClaimStatus.INSUFFICIENT: 4, ClaimStatus.UNVERIFIED: 5}
    return sorted(claims, key=lambda c: (order[c.status], -c.confidence, c.statement))


def declared_dependencies(q: Question, r: "RunResult") -> list[str]:
    """The questions `q` explicitly depends on in the research contract. Direct only: if q depends on B and B on C,
    q may use B's claims but not C's. A cycle cannot loop, because nothing is followed transitively."""
    in_contract = {x.id for x in r.contract.questions}
    return sorted({d for d in q.depends_on if d in in_contract and d != q.id})


def claim_scope(q: Question, r: "RunResult") -> dict[str, list[str]]:
    """claim id -> the question(s) through which `q` may use that claim.

    A claim gathered for `q` is used directly: [q.id]. A claim gathered only for questions `q` declares it
    depends on is used through those: [dependency ids]. Any other claim is absent: no declared path, no access.
    Claim.question_ids is read, never changed; using a dependency's claim does not re-associate it.
    """
    deps = set(declared_dependencies(q, r))
    scope: dict[str, list[str]] = {}
    for c in r.graph.claims.values():
        if q.id in c.question_ids:
            scope[c.id] = [q.id]
        else:
            via = sorted(deps & set(c.question_ids))
            if via:
                scope[c.id] = via
    return scope


def permitted_claims(q: Question, r: "RunResult") -> list[Claim]:
    """The claims an answer to `q` may use: its own, plus those of its declared direct dependencies."""
    scope = claim_scope(q, r)
    return [c for c in r.graph.claims.values() if c.id in scope]


def select_claims(role: str, stage: str, claims: list[Claim]) -> list[Claim]:
    """Rank and select, by question role, from an already-permitted pool of claims."""
    if role in ("state", "claim"):
        return _ranked(claims)[:MAX_ITEMS]
    if role == "support":
        return [c for c in _ranked(claims) if c.status in CONFIRMED][:MAX_ITEMS]
    if role == "contradict":
        return [c for c in _ranked(claims) if c.status == ClaimStatus.CONTESTED][:MAX_ITEMS]
    if role == "prior":
        return [c for c in _ranked(claims) if set(c.topic) & PRIOR_WORDS][:MAX_ITEMS]
    if role == "stage":
        words = STAGE_KEYWORDS.get(stage, frozenset())
        return [c for c in _ranked(claims) if set(c.topic) & words][:MAX_ITEMS]
    return []


# ---- trial registrations are not efficacy evidence ---------------------------------------------------------
# A ClinicalTrials.gov record says a trial exists, not that the intervention works. For an efficacy question its
# claims are never presented as the answer or as supporting evidence: they are set apart as "Registered trials (not
# efficacy evidence)", each with any population or condition mismatch against the question flagged.
REGISTRY_LABEL = "registered_trial_not_efficacy_evidence"
EFFICACY_WORDS = frozenset({
    "effective", "effectiveness", "efficacy", "efficacious", "work", "works", "working", "lower", "lowers",
    "lowering", "reduce", "reduces", "reducing", "improve", "improves", "improving", "treat", "treats", "treating",
    "cure", "cures", "prevent", "prevents", "preventing", "benefit", "benefits", "beneficial", "help", "helps",
    "relieve", "relieves", "slow", "slows", "outcome", "outcomes",
})
ANSWER_ROLES = ("state", "claim", "support")  # roles whose claims are presented as what the evidence supports
POPULATIONS = {
    "children": frozenset({"children", "child", "pediatric", "paediatric", "kids", "infant", "infants", "neonates",
                           "neonatal", "newborns", "adolescent", "adolescents", "teens", "teenagers", "juvenile",
                           "youth"}),
    "adults": frozenset({"adult", "adults"}),
    "older adults": frozenset({"older", "elderly", "geriatric", "seniors"}),
    "pregnant people": frozenset({"pregnant", "pregnancy"}),
    "women": frozenset({"women", "woman", "female", "females"}),
    "men": frozenset({"men", "man", "male", "males"}),
}


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z\-']*", text.lower()))


def efficacy_question(r: "RunResult") -> bool:
    return bool(_word_set(r.contract.objective) & EFFICACY_WORDS)


def registry_only(c: Claim, r: "RunResult") -> bool:
    """True when every piece of a claim's support is a trial-registry record."""
    ev = [r.graph.evidence.get(e) for e in c.supporting]
    return bool(ev) and all(e is not None and isinstance(e.data, dict)
                            and e.data.get("evidence_label") == REGISTRY_LABEL for e in ev)


def without_registrations(role: str, claims: list[Claim], r: "RunResult") -> list[Claim]:
    """The claims an answer may present: registry records are removed for an efficacy question."""
    if role not in ANSWER_ROLES:
        return claims
    kept = [c for c in claims if not registry_only(c, r)]
    return claims if len(kept) == len(claims) or not efficacy_question(r) else kept


def populations(text: str) -> set[str]:
    words = _word_set(text)
    return {name for name, terms in POPULATIONS.items() if words & terms}


def registered_trials(r: "RunResult") -> list[dict]:
    """The registry records of a run, one per trial, with their claim ids and any mismatch with the question.

    Population mismatch: the question names a population and the record's titles and conditions name a different
    set of populations, or none. Condition mismatch: no registered condition shares a word with the question.
    """
    question = r.contract.objective
    q_pop, q_words = populations(question), set(topic_tokens(question))
    out: dict[str, dict] = {}
    data_of: dict[str, dict] = {}
    for c in r.graph.claims.values():
        if not registry_only(c, r):
            continue
        for eid in c.supporting:
            data = r.graph.evidence[eid].data
            nct = str(data.get("nct_id") or eid)
            data_of.setdefault(nct, data)
            t = out.setdefault(nct, {"nct_id": nct, "title": str(data.get("title") or ""), "claims": [],
                                     "flags": []})
            if c.id not in t["claims"]:
                t["claims"].append(c.id)
    for nct, t in out.items():
        data = data_of[nct]
        conditions = [str(x) for x in data.get("conditions") or []]
        t_pop = populations(" ".join([t["title"], str(data.get("official_title") or ""), *conditions]))
        if q_pop and t_pop != q_pop:
            t["flags"].append(f"population mismatch: the question concerns {', '.join(sorted(q_pop))}; the trial "
                              f"registers {', '.join(sorted(t_pop)) or 'no stated population'}")
        if conditions and not set(topic_tokens(" ".join(conditions))) & q_words:
            t["flags"].append(f"condition mismatch: the trial studies {', '.join(conditions)}, which the question "
                              "does not name")
    return sorted(out.values(), key=lambda t: t["nct_id"])


def set_apart_registrations(q: Question, r: "RunResult") -> list[Claim]:
    """The registry claims an answer to `q` would have presented, had they not been set apart."""
    if q.role not in ANSWER_ROLES:
        return []
    registered = [c for c in permitted_claims(q, r) if registry_only(c, r)]
    return registered if registered and efficacy_question(r) else []


def registered_note(removed: list[Claim], r: "RunResult") -> str:
    """The sentence a finding carries when registry records were set apart from its answer ('' if none)."""
    if not removed:
        return ""
    ids = {c.id for c in removed}
    trials = [t for t in registered_trials(r) if ids & set(t["claims"])]
    note = (f"Registered trials (not efficacy evidence): {len(trials)} trial registration(s) "
            f"({', '.join(t['nct_id'] for t in trials)}) listed separately; a registration does not show that the "
            "intervention works.")
    flagged = sum(1 for t in trials if t["flags"])
    if flagged:
        note += f" {flagged} of them do not match the question's population or condition."
    return note


def answer(q: Question, r: "RunResult") -> Answer:
    a = Answer(q)
    if q.role == "gap":
        a.gaps = [g for g in r.gaps if not g.endswith("no verified finding yet")]
    else:
        pool = without_registrations(q.role, permitted_claims(q, r), r)
        a.claims = select_claims(q.role, q.stage, pool)  # filter first, then rank
    return a


def answer_all(r: "RunResult") -> list[Answer]:
    return [answer(q, r) for q in r.contract.questions]
