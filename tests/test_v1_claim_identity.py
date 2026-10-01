"""Claim identity v2: the id names a proposition, under an explicit, versioned rule. All data is fictional."""

from __future__ import annotations

import json
import unittest

from lofgren_intelligence.evidence import Claim, EvidenceGraph, Scope
from lofgren_intelligence.evidence.types import (
    CLAIM_IDENTITY_SCHEMA,
    CLAIM_IDENTITY_VERSION,
    ClaimIdentityError,
    claim_id,
    claim_identity_key,
    make_id,
)

S = "Industrial warehouse vacancy rose (fictional)."
PHOENIX = Scope("2026-01-01", "2026-12-31", "Phoenix")


class SerializationTests(unittest.TestCase):
    def test_identity_key_definition(self):
        c = Claim("  Industrial   warehouse VACANCY rose (fictional). ", value=11, unit=" % ",
                  subject=" vacancy:phx ", scope=Scope("2026-01-01", "2026-12-31", "  Phoenix   Metro ", 33.4, -112))
        self.assertEqual(claim_identity_key(c), {
            "identity": CLAIM_IDENTITY_SCHEMA,
            "statement": "industrial warehouse vacancy rose (fictional).",
            "subject": "vacancy:phx",
            "value": 11.0,
            "unit": "%",
            "scope": {"valid_from": "2026-01-01", "valid_to": "2026-12-31", "geography": "phoenix metro",
                      "lat": 33.4, "lon": -112.0},
        })
        canonical = json.dumps(claim_identity_key(c), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(c.id, make_id("CL", canonical))

    def test_absent_structure_is_null_not_guessed(self):
        # Nothing is parsed out of the statement: a year and a place in the text are not scope.
        key = claim_identity_key(Claim("Phoenix vacancy reached 11 percent in 2026 (fictional)."))
        self.assertEqual((key["subject"], key["value"], key["unit"]), (None, None, None))
        self.assertEqual(set(key["scope"].values()), {None})

    def test_current_version_is_two(self):
        self.assertEqual(CLAIM_IDENTITY_VERSION, 2)
        self.assertEqual(Claim(S).identity_version, 2)


class DeterminismTests(unittest.TestCase):
    def test_same_proposition_same_id(self):
        ids = {Claim(S, value=11.0, unit="%", scope=PHOENIX).id for _ in range(20)}
        self.assertEqual(len(ids), 1)

    def test_normalization(self):
        base = Claim(S, value=11, unit="%", scope=PHOENIX)
        for variant in (Claim("  " + S.upper() + "  ", value=11.0, unit="%", scope=PHOENIX),
                        Claim(S.replace(" ", "   "), value=11, unit=" %", scope=PHOENIX),
                        Claim(S, value=11, unit="%", scope=Scope("2026-01-01", "2026-12-31", "PHOENIX "))):
            with self.subTest(variant=variant.statement):
                self.assertEqual(variant.id, base.id)

    def test_each_identity_field_matters(self):
        base = Claim(S, value=11.0, unit="%", subject="vacancy:phx", scope=PHOENIX)
        variants = {
            "geography": Claim(S, value=11.0, unit="%", subject="vacancy:phx",
                               scope=Scope("2026-01-01", "2026-12-31", "Tucson")),
            "period": Claim(S, value=11.0, unit="%", subject="vacancy:phx",
                            scope=Scope("2024-01-01", "2024-12-31", "Phoenix")),
            "value": Claim(S, value=14.0, unit="%", subject="vacancy:phx", scope=PHOENIX),
            "unit": Claim(S, value=11.0, unit="points", subject="vacancy:phx", scope=PHOENIX),
            "subject": Claim(S, value=11.0, unit="%", subject="vacancy:tus", scope=PHOENIX),
            "statement": Claim("Industrial warehouse vacancy fell (fictional).", value=11.0, unit="%",
                               subject="vacancy:phx", scope=PHOENIX),
            "coordinates": Claim(S, value=11.0, unit="%", subject="vacancy:phx",
                                 scope=Scope("2026-01-01", "2026-12-31", "Phoenix", 33.45, -112.07)),
        }
        for name, other in variants.items():
            with self.subTest(field=name):
                self.assertNotEqual(other.id, base.id)

    def test_question_and_observation_do_not_change_identity(self):
        base = Claim(S, scope=PHOENIX)
        same = [Claim(S, scope=PHOENIX, question_id="Q-a"), Claim(S, scope=PHOENIX, question_id="Q-b"),
                Claim(S, scope=PHOENIX, supporting=["EV-1"], confidence=0.9),
                Claim(S, scope=PHOENIX, status="verified", issues=["stale"]),
                Claim(S, scope=PHOENIX, origin="hypothesis")]
        for other in same:
            self.assertEqual(other.id, base.id)


class VersionTests(unittest.TestCase):
    def test_version_one_keeps_its_original_rule(self):
        legacy = Claim(S, scope=PHOENIX, identity_version=1)
        self.assertEqual(legacy.id, make_id("CL", S.lower().strip()))
        self.assertEqual(Claim(S, scope=Scope("2024-01-01", "2024-12-31", "Tucson"), identity_version=1).id,
                         legacy.id)  # v1 ignores scope, as it always did
        self.assertNotEqual(legacy.id, Claim(S, scope=PHOENIX).id)

    def test_unsupported_versions_fail_closed(self):
        for bad in (0, 3, "2", True, None):
            with self.subTest(version=bad), self.assertRaises(ClaimIdentityError):
                Claim(S, identity_version=bad)

    def test_saved_graphs_without_a_version_load_as_version_one(self):
        g = EvidenceGraph()
        g.add_claim(Claim(S, scope=PHOENIX))
        data = g.to_json()
        saved_v2 = data["claims"][0]
        self.assertEqual(saved_v2["identity_version"], 2)
        self.assertEqual(EvidenceGraph.from_json(data).claims[saved_v2["id"]].identity_version, 2)
        legacy = dict(saved_v2, id=make_id("CL", S.lower().strip()))
        legacy.pop("identity_version")
        loaded = EvidenceGraph.from_json({"claims": [legacy]})
        claim = loaded.claims[legacy["id"]]
        self.assertEqual((claim.identity_version, claim.id), (1, legacy["id"]))  # kept, never recomputed

    def test_claim_id_reports_unknown_version(self):
        c = Claim(S)
        c.identity_version = 9
        with self.assertRaises(ClaimIdentityError):
            claim_id(c)


class FailClosedTests(unittest.TestCase):
    def test_malformed_identity_input(self):
        cases = {
            "nan value": dict(value=float("nan")),
            "infinite value": dict(value=float("inf")),
            "text value": dict(value="11"),
            "bool value": dict(value=True),
            "bad date": dict(scope=Scope("2026-02-30", None, "Phoenix")),
            "not a date": dict(scope=Scope("last year", None, "Phoenix")),
            "numeric date": dict(scope=Scope(2026, None, "Phoenix")),
            "infinite latitude": dict(scope=Scope(None, None, "Phoenix", float("inf"), 0.0)),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name), self.assertRaises(ClaimIdentityError):
                Claim(S, **kwargs)

    def test_iso_timestamps_are_accepted(self):
        c = Claim(S, scope=Scope("2026-09-01T00:00:00Z", "2026-09-29T00:00:00Z", "Phoenix"))
        self.assertEqual(claim_identity_key(c)["scope"]["valid_from"], "2026-09-01T00:00:00Z")

    def test_supplied_ids_are_kept(self):
        # Callers that restore a claim with its stored id (for example from a saved graph) keep that id.
        self.assertEqual(Claim(S, id="CL-0123456789").id, "CL-0123456789")


if __name__ == "__main__":
    unittest.main()
