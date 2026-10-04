"""Future-dated evidence (LI-V1-HARDEN-07A; was the pinned defect V1-FUTURE-DATED-EVIDENCE).

Evidence dated more than FUTURE_SKEW (5 minutes) after the verifier's clock stays in the graph and in provenance,
but supports, contradicts, adds independence and freshness to nothing, and its claim carries a "future-dated:" issue.
Up to 5 minutes ahead is clock skew: eligible, age zero. All data is fictional.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from lofgren_intelligence.evidence import (
    Claim,
    ClaimStatus,
    Evidence,
    EvidenceGraph,
    EvidenceKind,
    Source,
    SourceKind,
)
from lofgren_intelligence.verification.engine import FUTURE_DATED, FUTURE_SKEW, Verifier, temporally_eligible

T = datetime(2026, 10, 1, 15, 0, 0, tzinfo=timezone.utc)
RISE = "Industrial construction in the Phoenix metro increased in 2026 (fictional)."
FALL = "Industrial construction in the Phoenix metro decreased in 2026 (fictional)."
PAST = (T - timedelta(days=30)).isoformat()


class World:
    """A graph whose evidence comes from independent sources (one publisher each)."""

    def __init__(self) -> None:
        self.g = EvidenceGraph()
        self.n = 0

    def evidence(self, statement: str, when: str | None) -> str:
        self.n += 1
        src = self.g.add_source(Source(SourceKind.DOCUMENT, f"report {self.n}", uri=f"inline:fe{self.n}",
                                       publisher=f"publisher {self.n}", quality=0.8))
        return self.g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, f"{statement} (copy {self.n})",
                                            observed_at=when)).id

    def claim(self, statement: str, *whens: str | None) -> tuple[Claim, list[str]]:
        c = self.g.add_claim(Claim(statement))
        ids = [self.evidence(statement, w) for w in whens]
        for e in ids:
            self.g.link(e, c.id, "supports")
        return c, ids

    def verify(self, now: datetime = T) -> Verifier:
        v = Verifier(now=now)
        v.verify(self.g)
        return v


def at(delta: timedelta) -> str:
    return (T + delta).isoformat()


class Eligibility(unittest.TestCase):
    def eligible(self, stamp: str | None) -> bool:
        w = World()
        e = w.evidence(RISE, stamp)
        return temporally_eligible(w.g, e, T)

    def test_boundaries(self):
        cases = {
            "T - 1s": (timedelta(seconds=-1), True),
            "T": (timedelta(0), True),
            "T + 1s": (timedelta(seconds=1), True),
            "T + 4m59s": (timedelta(minutes=4, seconds=59), True),
            "T + 5m00s": (timedelta(minutes=5), True),
            "T + 5m01s": (timedelta(minutes=5, seconds=1), False),
            "T + 1h": (timedelta(hours=1), False),
            "T + 1d": (timedelta(days=1), False),
            "T + 1y": (timedelta(days=365), False),
        }
        self.assertEqual(FUTURE_SKEW, timedelta(minutes=5))
        for name, (delta, expected) in cases.items():
            with self.subTest(case=name):
                self.assertIs(self.eligible(at(delta)), expected)

    def test_same_instant_in_any_offset(self):
        self.assertTrue(self.eligible("2026-10-01T15:00:00Z"))
        self.assertTrue(self.eligible("2026-10-01T08:00:00-07:00"))
        self.assertTrue(self.eligible("2026-10-01T15:05:00+00:00"))
        self.assertTrue(self.eligible("2026-10-01T08:05:00-07:00"))  # T + 5m00s, written in another offset
        self.assertFalse(self.eligible("2026-10-01T08:05:01-07:00"))  # T + 5m01s
        self.assertFalse(self.eligible("2026-10-02T00:05:01+09:00"))  # T + 5m01s, east of UTC

    def test_naive_timestamps_are_utc_and_undated_is_eligible(self):
        self.assertTrue(self.eligible("2026-10-01T15:05:00"))
        self.assertFalse(self.eligible("2026-10-01T15:05:01"))
        self.assertTrue(self.eligible(None))  # nothing says it lies in the future
        with self.assertRaises(ValueError):  # an unreadable date never reaches the graph (evidence identity v2)
            self.eligible("not a date")

    def test_source_publication_date_is_the_fallback(self):
        w = World()
        src = w.g.add_source(Source(SourceKind.DOCUMENT, "dated report", uri="inline:pub", publisher="p",
                                    quality=0.8, published_at=at(timedelta(days=2))))
        e = w.g.add_evidence(Evidence(src.id, EvidenceKind.DOCUMENT, RISE)).id
        self.assertFalse(temporally_eligible(w.g, e, T))


class ClaimBehaviour(unittest.TestCase):
    # 1. Two otherwise valid sources, both far in the future: the claim cannot be verified.
    def test_only_future_support_cannot_verify(self):
        w = World()
        c, _ = w.claim(RISE, at(timedelta(days=365)), at(timedelta(days=365)))
        f = w.verify().factors[c.id]
        self.assertNotEqual(c.status, ClaimStatus.VERIFIED)
        self.assertEqual(c.status, ClaimStatus.INSUFFICIENT)
        self.assertEqual((f.independent_sources, f.quality, f.raw), (0, 0.0, 0.0))

    # 2. One present source and one future source cannot meet a two-independent-source policy.
    def test_future_source_does_not_count_toward_independence(self):
        control = World()
        cc, _ = control.claim(RISE, PAST, PAST)
        control.verify()
        self.assertEqual(cc.status, ClaimStatus.VERIFIED)  # two present sources would verify
        w = World()
        c, _ = w.claim(RISE, PAST, at(timedelta(hours=1)))
        f = w.verify().factors[c.id]
        self.assertEqual(f.independent_sources, 1)
        self.assertNotEqual(c.status, ClaimStatus.VERIFIED)

    # 3. Enough present sources still verify; the future one changes nothing at all.
    def test_future_source_contributes_nothing(self):
        without = World()
        cw, _ = without.claim(RISE, PAST, PAST, PAST)
        fw = without.verify().factors[cw.id]
        w = World()
        c, ids = w.claim(RISE, PAST, PAST, PAST, at(timedelta(days=30)))
        f = w.verify().factors[c.id]
        self.assertEqual(c.status, ClaimStatus.VERIFIED)
        self.assertEqual((f.quality, f.independent_sources, f.recency, f.contradicting_sources, f.raw, f.calibrated),
                         (fw.quality, fw.independent_sources, fw.recency, fw.contradicting_sources, fw.raw,
                          fw.calibrated))
        self.assertEqual(c.confidence, cw.confidence)

    # 4. Future contradicting evidence does not lower present standing as if it had already happened.
    def test_future_contradiction_does_not_contest_the_present(self):
        control = World()
        ca, _ = control.claim(RISE, PAST, PAST)
        control.claim(FALL, PAST, PAST)
        control.verify()
        self.assertEqual(ca.status, ClaimStatus.CONTESTED)  # the same conflict, dated now, does contest it
        w = World()
        a, _ = w.claim(RISE, PAST, PAST)
        b, future = w.claim(FALL, at(timedelta(days=90)), at(timedelta(days=90)))
        fa = w.verify().factors[a.id]
        self.assertTrue(set(future) <= set(a.contradicting), "the conflict is still recorded on the claim")
        self.assertTrue(w.g.contradictions, "the contradiction itself stays visible")
        self.assertEqual(fa.contradicting_sources, 0)
        self.assertEqual(a.status, ClaimStatus.VERIFIED)
        self.assertEqual(b.status, ClaimStatus.INSUFFICIENT)
        self.assertTrue(any(i.startswith(f"{FUTURE_DATED}:") for i in a.issues), a.issues)

    # 5. Within the 5-minute tolerance evidence remains eligible.
    def test_clock_skew_tolerance(self):
        w = World()
        c, _ = w.claim(RISE, at(timedelta(minutes=4, seconds=59)), at(timedelta(minutes=5)))
        f = w.verify().factors[c.id]
        self.assertEqual(f.independent_sources, 2)
        self.assertEqual(c.status, ClaimStatus.VERIFIED)
        self.assertFalse([i for i in c.issues if i.startswith(FUTURE_DATED)])
        self.assertEqual(f.recency, 1.0)  # age zero, not negative

    # 6. Future evidence stays visible and inspectable.
    def test_future_evidence_is_preserved(self):
        w = World()
        stamp = at(timedelta(days=365))
        c, ids = w.claim(RISE, PAST, stamp)
        before = {e: json.dumps(w.g.evidence[e].__dict__, default=str, sort_keys=True) for e in ids}
        w.verify()
        self.assertEqual({e: json.dumps(w.g.evidence[e].__dict__, default=str, sort_keys=True) for e in ids}, before)
        self.assertEqual(c.supporting, ids)
        trace = w.g.trace(c.id)
        self.assertIn(ids[1], [x["evidence"]["id"] for x in trace["supporting"]])
        self.assertEqual(w.g.evidence[ids[1]].observed_at, stamp)  # not clamped, not rewritten
        self.assertEqual(w.g.problems(), [])

    def test_issue_names_the_evidence_and_both_times(self):
        w = World()
        stamp = at(timedelta(days=365))
        c, ids = w.claim(RISE, PAST, stamp)
        w.verify()
        issues = [i for i in c.issues if i.startswith("future-dated:")]
        self.assertEqual(len(issues), 1, c.issues)
        issue = issues[0]
        for part in (ids[1], stamp, T.isoformat(), "not used as evidence"):
            self.assertIn(part, issue)
        self.assertNotIn(ids[0], issue)

    def test_deterministic_and_idempotent(self):
        w = World()
        c, _ = w.claim(RISE, PAST, at(timedelta(days=3)))
        first = (w.verify().factors[c.id], c.status, list(c.issues))
        second = (w.verify().factors[c.id], c.status, list(c.issues))
        self.assertEqual(first, second)  # same clock, same answer; the issue is not added twice

    def test_the_verifier_clock_decides(self):
        w = World()
        c, _ = w.claim(RISE, at(timedelta(days=10)), at(timedelta(days=10)))
        w.verify(now=T)
        self.assertNotEqual(c.status, ClaimStatus.VERIFIED)
        later = World()
        c2, _ = later.claim(RISE, at(timedelta(days=10)), at(timedelta(days=10)))
        later.verify(now=T + timedelta(days=11))  # the same evidence, verified after it was observed
        self.assertEqual(c2.status, ClaimStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
