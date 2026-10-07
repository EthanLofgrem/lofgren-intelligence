"""Free public-source ingestion: Europe PMC, ClinicalTrials.gov and operator-selected URLs (Q24).

Every test here runs offline against recorded JSON fixtures (tests/fixtures/public_sources, written by hand
from the documented response shapes; the drug "zelvapril" is fictional). The one exception is
tests/live/test_public_sources_live.py, which makes one tiny query to each API and runs only when LI_LIVE_SMOKE=1
(explicitly: it is outside the default suite).
"""

import contextlib
import email.message
import hashlib
import io
import json
import socket
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence import build_registry
from lofgren_intelligence.adapters import AdapterRegistry
from lofgren_intelligence.adapters.clinicaltrials import EVIDENCE_LABEL, ClinicalTrialsAdapter, parse_study
from lofgren_intelligence.adapters.europepmc import EuropePMCAdapter, parse_record, quality_prior
from lofgren_intelligence.adapters.manifest import (
    MANIFEST_SCHEMA,
    MAX_MANIFEST_SOURCES,
    ManifestError,
    OperatorSourcesAdapter,
    load_manifest,
    parse_manifest,
)
from lofgren_intelligence.adapters.net import UnsafeURL
from lofgren_intelligence.adapters.public_api import (
    OUTCOME_COMPLETE,
    OUTCOME_PARTIAL,
    OUTCOME_UNAVAILABLE,
    PublicHTTPClient,
)
from lofgren_intelligence.intent.compiler import compile_intent
from lofgren_intelligence.kernel.pipeline import run_investigation
from lofgren_intelligence.verification.engine import Verifier
from lofgren_intelligence.verification.policies import classify_claim

FIX = Path(__file__).parent / "fixtures" / "public_sources"
NOW = "2026-10-01T12:00:00+00:00"
VERIFY_AT = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
OBJECTIVE = "Does zelvapril lower blood pressure in adults with hypertension?"


def fixture(name):
    return (FIX / name).read_bytes()


class FakeResponse:
    def __init__(self, status=200, body=b"", ctype="application/json", headers=None, read_error=None):
        self.status = status
        self._body = body
        self._read_error = read_error
        self.headers = email.message.Message()
        if ctype:
            self.headers["Content-Type"] = ctype
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self, amt=None):
        if self._read_error:
            raise self._read_error
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Replays a scripted sequence of responses (or exceptions) and records every request URL."""

    def __init__(self, script):
        self.script = list(script)
        self.urls = []
        self.timeouts = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        self.timeouts.append(timeout)
        if not self.script:
            raise AssertionError(f"unexpected request {req.full_url}")
        step = self.script.pop(0)
        if callable(step):
            step = step(req)
        if isinstance(step, BaseException):
            raise step
        return step

    def params(self, i):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.urls[i]).query))


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


def client(name, script, **kw):
    clock = FakeClock()
    opener = FakeOpener(script)
    kw.setdefault("min_interval_s", 0.0)
    c = PublicHTTPClient(name, opener=opener, sleep=clock.sleep, monotonic=clock.monotonic, **kw)
    return c, opener, clock


def ok(name):
    return FakeResponse(body=fixture(name))


def contract():
    return compile_intent(OBJECTIVE)


def question():
    return contract().questions[0]


def run(*adapters):
    reg = AdapterRegistry()
    for a in adapters:
        reg.register(a)
    return run_investigation(contract(), reg, plan_id="payg", verifier=Verifier(now=VERIFY_AT))


# ---------------------------------------------------------------------------------------------------------------
class EuropePMCTest(unittest.TestCase):
    def adapter(self, script, **kw):
        c, opener, clock = client("europepmc", script)
        kw.setdefault("page_size", 3)
        return EuropePMCAdapter("zelvapril hypertension", client=c, clock=lambda: NOW, **kw), opener, clock

    def test_parses_ids_bibliography_and_labels(self):
        rec = parse_record(json.loads(fixture("europepmc_page1.json"))["resultList"]["result"][0])
        self.assertEqual((rec["pmid"], rec["pmcid"], rec["doi"]), ("90000001", "PMC9000001", "10.5555/fixture.0001"))
        self.assertEqual(rec["title"], "Zelvapril for hypertension in adults: a systematic review and meta-analysis.")
        self.assertEqual(rec["authors"], ["Rivera A", "Okafor B", "Lind C"])
        self.assertEqual(rec["journal"], "Journal of Fixture Cardiology")
        self.assertEqual(rec["publication_date"], "2024-02-15")
        self.assertEqual(rec["publication_types"], ["Systematic Review", "Meta-Analysis", "Journal Article", "Review"])
        self.assertEqual(rec["study_designs"], ["systematic_review", "meta_analysis", "review"])
        self.assertIn("lowered systolic blood pressure", rec["abstract"])
        self.assertNotIn("<h4>", rec["abstract"])
        self.assertEqual(rec["text_scope"], "abstract_only")
        self.assertIs(rec["full_text_fetched"], False)
        self.assertIs(rec["open_access"], True)
        self.assertIs(rec["full_text_in_europepmc"], True)
        self.assertEqual(rec["source_class"], "peer_reviewed_literature")
        self.assertEqual(rec["source_url"], "https://europepmc.org/article/MED/90000001")
        # a "Comment in" is neither a correction nor a retraction
        self.assertEqual((rec["retracted"], rec["corrected"], rec["expression_of_concern"]), (False, False, False))
        self.assertEqual(rec["correction_signals_checked"], ["pubTypeList", "commentCorrectionList"])
        self.assertEqual(quality_prior(rec), 0.8)

    def test_retraction_and_correction_flags(self):
        results = json.loads(fixture("europepmc_page1.json"))["resultList"]["result"]
        rct, retracted = parse_record(results[1]), parse_record(results[2])
        self.assertIn("randomized_controlled_trial", rct["study_designs"])
        self.assertIs(rct["corrected"], True)
        self.assertIs(rct["retracted"], False)
        self.assertEqual(quality_prior(rct), 0.65)  # 0.75 for an RCT, less 0.1 for the erratum
        self.assertIs(rct["open_access"], False)
        self.assertIs(retracted["retracted"], True)
        # by commentCorrectionList alone
        only_cc = dict(results[2], pubTypeList={"pubType": ["Journal Article"]})
        self.assertIs(parse_record(only_cc)["retracted"], True)
        # no correction field at all: not flagged, and the record says the field was not available to check
        bare = {k: v for k, v in results[1].items() if k != "commentCorrectionList"}
        rec = parse_record(bare)
        self.assertIs(rec["corrected"], False)
        self.assertEqual(rec["correction_signals_checked"], ["pubTypeList"])
        concern = dict(results[1], commentCorrectionList={"commentCorrection": {"type": "Expression of concern in"}})
        self.assertIs(parse_record(concern)["expression_of_concern"], True)

    def test_preprint_and_title_only(self):
        rec = parse_record(json.loads(fixture("europepmc_page2.json"))["resultList"]["result"][0])
        self.assertEqual(rec["source_class"], "preprint")
        self.assertIn("preprint", rec["study_designs"])
        self.assertEqual(rec["journal"], "medRxiv")
        self.assertEqual(rec["text_scope"], "title_only")
        self.assertIsNone(rec["pmid"])
        self.assertEqual(quality_prior(rec), 0.4)
        self.assertIsNone(parse_record(json.loads(fixture("europepmc_page2.json"))["resultList"]["result"][1]))
        self.assertIsNone(parse_record("not a record"))

    def test_cursor_pagination_until_the_cursor_stops(self):
        a, opener, _ = self.adapter([ok("europepmc_page1.json"), ok("europepmc_page2.json")], max_records=10)
        r = a.retrieve()
        self.assertEqual(len(opener.urls), 2)
        self.assertTrue(opener.urls[0].startswith("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"))
        p0, p1 = opener.params(0), opener.params(1)
        self.assertEqual((p0["cursorMark"], p0["pageSize"], p0["format"], p0["resultType"]), ("*", "3", "json", "core"))
        self.assertEqual(p0["query"], "zelvapril hypertension")
        self.assertEqual(p1["cursorMark"], "AoIIQFixturePage2")
        self.assertEqual((r.pages, r.records, r.malformed, r.hit_count), (2, 4, 1, 5))
        self.assertFalse(r.truncated)
        self.assertEqual(r.outcome, OUTCOME_PARTIAL)  # one malformed record was skipped
        self.assertEqual(r.retrieved_at, NOW)
        self.assertEqual([x["pmid"] or x["doi"] for x in a.records], ["90000001", "90000002", "10.1101/2025.01.01.900004"])
        self.assertEqual([x["pmid"] for x in a.excluded], ["90000003"])

    def test_record_bound_stops_paging_and_reports_truncation(self):
        a, opener, _ = self.adapter([ok("europepmc_page1.json")], max_records=2)
        r = a.retrieve()
        self.assertEqual(len(opener.urls), 1)
        self.assertEqual(opener.params(0)["pageSize"], "2")
        self.assertEqual((r.records, r.outcome, r.truncated), (2, OUTCOME_COMPLETE, True))
        notes = a.gather(question(), contract()).notes
        self.assertTrue(any("bounded at 2 of 5" in n for n in notes), notes)

    def test_page_bound(self):
        a, opener, _ = self.adapter([ok("europepmc_page1.json")], max_records=10, max_pages=1)
        self.assertEqual(a.retrieve().pages, 1)
        self.assertEqual(len(opener.urls), 1)

    def test_bounds_and_query_fail_closed(self):
        c, _, _ = client("europepmc", [])
        for bad in (0, 101, True, "5"):
            with self.assertRaises(ValueError):
                EuropePMCAdapter("q", max_records=bad, client=c)
        for bad in ("", "   ", "a\nb", "x" * 501, None):
            with self.assertRaises(ValueError):
                EuropePMCAdapter(bad, client=c)
        with self.assertRaises(ValueError):
            EuropePMCAdapter("q", page_size=101, client=c)
        with self.assertRaises(ValueError):
            EuropePMCAdapter("q", max_pages=11, client=c)

    def test_evidence_labels_and_retraction_exclusion(self):
        a, _, _ = self.adapter([ok("europepmc_page1.json"), ok("europepmc_page2.json")], max_records=10)
        out = a.gather(question(), contract())
        self.assertTrue(out.items)
        by_pmid = {ev.data["pmid"]: (src, ev) for src, ev in out.items}
        self.assertNotIn("90000003", by_pmid)  # retracted: never evidence
        self.assertTrue(any("excluded PMID 90000003" in n and "retracted" in n for n in out.notes), out.notes)
        self.assertTrue(any("partial retrieval" in n for n in out.notes), out.notes)
        src, ev = by_pmid["90000001"]
        self.assertEqual(ev.data["source_class"], "peer_reviewed_literature")
        self.assertEqual(ev.data["text_scope"], "abstract_only")
        self.assertIs(ev.data["full_text_fetched"], False)
        self.assertEqual(ev.data["retrieval"]["outcome"], OUTCOME_PARTIAL)
        self.assertEqual(ev.data["retrieval"]["retrieved_at"], NOW)
        self.assertEqual(src.retrieved_at, NOW)
        self.assertEqual(src.independence_group, "work:doi:10.5555/fixture.0001")
        self.assertEqual(src.uri, "https://europepmc.org/article/MED/90000001")
        self.assertEqual(src.published_at, "2024-02-15")
        self.assertNotIn("systematic review and meta-analysis", ev.content)  # the title is not read as a claim
        self.assertTrue(any("abstract only" in t for t in ev.transformations))
        _, rct = by_pmid["90000002"]
        self.assertTrue(any("Erratum in" in t for t in rct.transformations))
        # notes about the retrieval are run-level: reported on the first gather only
        again = a.gather(contract().questions[1], contract())
        self.assertFalse(any("excluded" in n or "partial retrieval" in n for n in again.notes))

    def test_end_to_end_run_carries_source_classes(self):
        a, _, _ = self.adapter([ok("europepmc_page1.json"), ok("europepmc_page2.json")], max_records=10)
        result = run(a)
        self.assertTrue(result.completed)
        classes = {e.data.get("source_class") for e in result.graph.evidence.values()}
        self.assertTrue(classes <= {"peer_reviewed_literature", "preprint"} and classes, classes)
        self.assertNotIn("https://europepmc.org/article/MED/90000003", {s.uri for s in result.graph.sources.values()})
        self.assertTrue(any("retracted" in u.description for u in result.unknowns))
        self.assertEqual(len({s.independence_group for s in result.graph.sources.values()}), len(result.graph.sources))


# ---------------------------------------------------------------------------------------------------------------
class ClinicalTrialsTest(unittest.TestCase):
    def adapter(self, script, **kw):
        c, opener, clock = client("clinicaltrials", script)
        kw.setdefault("page_size", 2)
        return ClinicalTrialsAdapter("zelvapril hypertension", client=c, clock=lambda: NOW, **kw), opener, clock

    def test_parses_registry_fields(self):
        rec = parse_study(json.loads(fixture("clinicaltrials_page1.json"))["studies"][0])
        self.assertEqual(rec["nct_id"], "NCT90000001")
        self.assertEqual(rec["title"], "Zelvapril in Adults With Hypertension")
        self.assertEqual(rec["overall_status"], "COMPLETED")
        self.assertEqual(rec["phases"], ["PHASE3"])
        self.assertEqual(rec["conditions"], ["Hypertension"])
        self.assertEqual(rec["interventions"], [{"type": "DRUG", "name": "Zelvapril"}, {"type": "DRUG", "name": "Placebo"}])
        self.assertEqual((rec["enrollment"], rec["enrollment_type"]), (820, "ACTUAL"))
        self.assertEqual((rec["start_date"], rec["primary_completion_date"], rec["completion_date"]),
                         ("2021-03", "2022-09-30", "2022-12"))
        self.assertEqual(rec["lead_sponsor"], "Fixture Pharma")
        self.assertEqual(rec["source_url"], "https://clinicaltrials.gov/study/NCT90000001")
        self.assertEqual(rec["source_class"], "trial_registry")
        self.assertEqual(rec["evidence_label"], EVIDENCE_LABEL)
        self.assertEqual(EVIDENCE_LABEL, "registered_trial_not_efficacy_evidence")
        self.assertIs(rec["results_posted"], True)
        self.assertIs(rec["results_fetched"], False)
        self.assertEqual(rec["results_label"], "results_posted_not_fetched")

    def test_results_only_when_has_results_is_set(self):
        studies = json.loads(fixture("clinicaltrials_page1.json"))["studies"]
        self.assertIs(parse_study(studies[1])["results_posted"], False)
        self.assertEqual(parse_study(studies[1])["results_label"], "no_results_posted")
        # a string "true" is not the boolean the API documents: not read as results
        self.assertIs(parse_study(json.loads(fixture("clinicaltrials_page2.json"))["studies"][0])["results_posted"], False)
        missing = {"protocolSection": studies[1]["protocolSection"]}
        self.assertIs(parse_study(missing)["results_posted"], False)
        self.assertIsNone(parse_study(json.loads(fixture("clinicaltrials_page2.json"))["studies"][1]))
        self.assertIsNone(parse_study({"hasResults": True}))

    def test_page_token_pagination(self):
        a, opener, _ = self.adapter([ok("clinicaltrials_page1.json"), ok("clinicaltrials_page2.json")], max_records=10)
        r = a.retrieve()
        self.assertTrue(opener.urls[0].startswith("https://clinicaltrials.gov/api/v2/studies?"))
        p0, p1 = opener.params(0), opener.params(1)
        self.assertEqual((p0["query.term"], p0["pageSize"], p0["format"], p0["countTotal"]),
                         ("zelvapril hypertension", "2", "json", "true"))
        self.assertNotIn("pageToken", p0)
        self.assertIn("HasResults", p0["fields"])
        self.assertEqual(p1["pageToken"], "FixtureToken2")
        self.assertNotIn("countTotal", p1)
        self.assertEqual((r.pages, r.records, r.malformed, r.hit_count, r.outcome), (2, 3, 1, 3, OUTCOME_PARTIAL))

    def test_record_bound(self):
        a, opener, _ = self.adapter([ok("clinicaltrials_page1.json")], max_records=1, page_size=1)
        r = a.retrieve()
        self.assertEqual(len(opener.urls), 1)
        self.assertEqual((r.records, r.truncated, r.outcome), (1, True, OUTCOME_COMPLETE))

    def test_every_record_is_labelled_registered_not_efficacy(self):
        a, _, _ = self.adapter([ok("clinicaltrials_page1.json"), ok("clinicaltrials_page2.json")], max_records=10)
        out = a.gather(question(), contract())
        self.assertEqual(len(out.items), 3)
        for src, ev in out.items:
            self.assertEqual(ev.data["evidence_label"], EVIDENCE_LABEL)
            self.assertEqual(ev.data["source_class"], "trial_registry")
            self.assertIs(ev.data["results_fetched"], False)
            self.assertEqual(ev.data["claim_subject"], f"trial:{ev.data['nct_id']}")
            self.assertEqual(src.independence_group, f"trial:{ev.data['nct_id']}")
            self.assertIn("registered trial is not evidence of efficacy", " ".join(ev.transformations))
            self.assertEqual(ev.data["retrieval"]["retrieved_at"], NOW)
            self.assertNotIn("will test whether", ev.content)  # the sponsor's aims are never evidence text
        posted = {ev.data["nct_id"]: ev.data["results_posted"] for _, ev in out.items}
        self.assertEqual(posted, {"NCT90000001": True, "NCT90000002": False, "NCT90000003": False})

    def test_registry_claims_are_attributions_pinned_to_their_trial(self):
        a, _, _ = self.adapter([ok("clinicaltrials_page1.json"), ok("clinicaltrials_page2.json")], max_records=10)
        result = run(a)
        claims = list(result.graph.claims.values())
        self.assertTrue(claims)
        for c in claims:
            self.assertTrue(c.subject.startswith("trial:NCT"), c.statement)
            self.assertTrue(c.statement.startswith("ClinicalTrials.gov reports that trial " + c.subject[6:]), c.statement)
            self.assertEqual(classify_claim(c).value, "attribution")
        # look-alike statements about different trials never corroborate each other
        for c in claims:
            groups = {result.graph.sources[result.graph.evidence[e].source_id].independence_group for e in c.supporting}
            self.assertEqual(groups, {c.subject})
        self.assertFalse(result.graph.contradictions)


# ---------------------------------------------------------------------------------------------------------------
class TransportTest(unittest.TestCase):
    """Timeouts, bounded retries with backoff, rate limiting and explicit outcomes."""

    def test_timeouts_are_retried_with_exponential_backoff(self):
        c, opener, clock = client("x", [socket.timeout("timed out"), TimeoutError(), ok("clinicaltrials_page2.json")],
                                  timeout=7.0)
        data = c.get_json("https://clinicaltrials.gov/api/v2/studies?x=1")
        self.assertIn("studies", data)
        self.assertEqual(c.requests, 3)
        self.assertEqual(clock.sleeps, [0.5, 1.0])
        self.assertEqual(opener.timeouts, [7.0, 7.0, 7.0])

    def test_retries_are_bounded(self):
        from lofgren_intelligence.adapters.public_api import ProviderUnavailable

        c, opener, clock = client("x", [socket.timeout()] * 3, max_retries=2)
        with self.assertRaises(ProviderUnavailable) as cm:
            c.get("https://example.org/")
        self.assertEqual(cm.exception.attempts, 3)
        self.assertEqual(len(opener.urls), 3)
        self.assertEqual(clock.sleeps, [0.5, 1.0])
        with self.assertRaises(ValueError):
            PublicHTTPClient("x", max_retries=5)
        with self.assertRaises(ValueError):
            PublicHTTPClient("x", timeout=0)

    def test_5xx_and_429_retry_but_4xx_does_not(self):
        c, opener, clock = client("x", [FakeResponse(503), FakeResponse(429, headers={"Retry-After": "2"}),
                                        ok("clinicaltrials_page2.json")])
        c.get_json("https://example.org/")
        self.assertEqual(clock.sleeps, [0.5, 2.0])
        from lofgren_intelligence.adapters.public_api import ProviderUnavailable

        c, opener, _ = client("x", [FakeResponse(404)])
        with self.assertRaises(ProviderUnavailable) as cm:
            c.get("https://example.org/")
        self.assertEqual((len(opener.urls), cm.exception.reason), (1, "HTTP 404 after 1 attempt(s)"))

    def test_retry_after_beyond_the_bound_gives_up(self):
        from lofgren_intelligence.adapters.public_api import ProviderUnavailable

        c, opener, clock = client("x", [FakeResponse(429, headers={"Retry-After": "3600"})])
        with self.assertRaises(ProviderUnavailable) as cm:
            c.get("https://example.org/")
        self.assertEqual((len(opener.urls), clock.sleeps), (1, []))
        self.assertIn("exceeds", cm.exception.reason)

    def test_rate_limit_spaces_requests(self):
        c, opener, clock = client("x", [ok("clinicaltrials_page2.json")] * 3, min_interval_s=1.2)
        for _ in range(3):
            c.get("https://example.org/")
        self.assertEqual(clock.sleeps, [1.2, 1.2])

    def test_oversized_or_malformed_responses_are_not_retried(self):
        from lofgren_intelligence.adapters.public_api import ProviderUnavailable

        c, opener, _ = client("x", [FakeResponse(read_error=ValueError("HTTP response exceeds public fetch limit"))])
        with self.assertRaises(ProviderUnavailable) as cm:
            c.get("https://example.org/")
        self.assertIn("exceeds public fetch limit", cm.exception.reason)
        self.assertEqual(len(opener.urls), 1)
        c, _, _ = client("x", [FakeResponse(body=b"{not json")])
        with self.assertRaises(ProviderUnavailable):
            c.get_json("https://example.org/")
        c, _, _ = client("x", [FakeResponse(body=b"<html></html>", ctype="text/html")])
        with self.assertRaises(ProviderUnavailable):
            c.get_json("https://example.org/")

    def test_provider_unavailable_is_explicit_never_silent(self):
        c, _, _ = client("europepmc", [socket.timeout()] * 3)
        a = EuropePMCAdapter("zelvapril", client=c, clock=lambda: NOW)
        out = a.gather(question(), contract())
        self.assertEqual(out.items, [])
        self.assertEqual(a.retrieval.outcome, OUTCOME_UNAVAILABLE)
        self.assertEqual(a.retrieval.requests, 3)
        self.assertTrue(any(n.startswith("europepmc: provider_unavailable") for n in out.notes), out.notes)
        result = run(EuropePMCAdapter("zelvapril", client=client("europepmc", [socket.timeout()] * 3)[0]))
        self.assertTrue(result.completed)
        self.assertTrue(any("provider_unavailable" in u.description for u in result.unknowns))

    def test_a_failed_later_page_is_partial(self):
        c, _, _ = client("clinicaltrials", [ok("clinicaltrials_page1.json")] + [FakeResponse(500)] * 3)
        a = ClinicalTrialsAdapter("zelvapril", client=c, page_size=2, clock=lambda: NOW)
        out = a.gather(question(), contract())
        self.assertEqual(a.retrieval.outcome, OUTCOME_PARTIAL)
        self.assertEqual(a.retrieval.records, 2)
        self.assertEqual(len(out.items), 2)
        self.assertTrue(all(ev.data["retrieval"]["outcome"] == OUTCOME_PARTIAL for _, ev in out.items))
        self.assertTrue(any("partial retrieval" in n and "HTTP 500" in n for n in out.notes), out.notes)

    def test_ssrf_refusal_still_applies(self):
        def no_socket(*a, **k):
            raise AssertionError("a connection was attempted")

        with patch("socket.create_connection", no_socket):
            for endpoint in ("http://127.0.0.1/search", "http://169.254.169.254/latest/meta-data",
                             "http://localhost/api", "file:///etc/passwd", "http://[::1]/x"):
                a = EuropePMCAdapter("q", client=PublicHTTPClient("europepmc", min_interval_s=0), endpoint=endpoint)
                r = a.retrieve()
                self.assertEqual(r.outcome, OUTCOME_UNAVAILABLE, endpoint)
                self.assertIn("refused by the public-fetch guard", r.errors[0])
                self.assertEqual(r.requests, 1)  # never retried
            private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
            with patch("socket.getaddrinfo", return_value=private):
                a = ClinicalTrialsAdapter("q", client=PublicHTTPClient("clinicaltrials", min_interval_s=0))
                r = a.retrieve()
                self.assertEqual(r.outcome, OUTCOME_UNAVAILABLE)
                self.assertIn("non-public", r.errors[0])


# ---------------------------------------------------------------------------------------------------------------
class ManifestTest(unittest.TestCase):
    def manifest_bytes(self, sources, **top):
        return json.dumps({"schema": MANIFEST_SCHEMA, "sources": sources, **top}).encode()

    def test_manifest_is_hashed_and_parsed(self):
        m = load_manifest(FIX / "sources_manifest.json")
        self.assertEqual(m.sha256, hashlib.sha256(fixture("sources_manifest.json")).hexdigest())
        self.assertEqual([s["url"] for s in m.sources], ["https://reports.example.org/zelvapril-market.html",
                                                         "https://data.example.net/zelvapril.txt"])
        self.assertEqual(m.sources[0]["title"], "Zelvapril market note")

    def test_manifest_fails_closed(self):
        good = {"url": "https://example.org/a"}
        bad_cases = [
            b"[]", b"{not json", json.dumps({"sources": [good]}).encode(),
            json.dumps({"schema": "other/1", "sources": [good]}).encode(),
            self.manifest_bytes([]), self.manifest_bytes([good], extra=1),
            self.manifest_bytes([dict(good, cookie="x")]),
            self.manifest_bytes([good, good]),
            self.manifest_bytes([{"url": "https://example.org/x", "sha256": "ABC"}]),
            self.manifest_bytes([{"title": "no url"}]),
            self.manifest_bytes([{"url": "https://example.org/" + "a" * 2100}]),
            self.manifest_bytes([{"url": f"https://example.org/{i}"} for i in range(MAX_MANIFEST_SOURCES + 1)]),
            b" " * 256_001,
        ]
        for raw in bad_cases:
            with self.assertRaises(ManifestError, msg=raw[:80]):
                parse_manifest(raw)
        for url in ("http://127.0.0.1/", "http://10.1.2.3/x", "http://169.254.169.254/", "ftp://example.org/",
                    "https://user:pw@example.org/", "http://localhost:8000/", "http://printer.local/",
                    "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/"):
            with self.assertRaises(ManifestError, msg=url):
                parse_manifest(self.manifest_bytes([{"url": url}]))
        with self.assertRaises(ManifestError):
            load_manifest(FIX / "missing.json")

    def adapter(self, script, manifest=None):
        c, opener, _ = client("operator_sources", script)
        m = manifest or load_manifest(FIX / "sources_manifest.json")
        return OperatorSourcesAdapter(m, client=c, clock=lambda: NOW), opener

    PAGE = (b"<html><body><p>Zelvapril sales to adults with hypertension reached 40 million units in 2025, "
            b"and blood pressure clinics reported that zelvapril is prescribed widely.</p></body></html>")
    TEXT = b"Zelvapril lowered blood pressure in adults with hypertension in a 2024 audit of 3 clinics."

    def test_operator_selected_label_date_and_hash(self):
        a, opener = self.adapter([FakeResponse(body=self.PAGE, ctype="text/html; charset=utf-8"),
                                  FakeResponse(body=self.TEXT, ctype="text/plain")])
        out = a.gather(question(), contract())
        self.assertEqual(len(opener.urls), 2)
        self.assertTrue(out.items)
        m = load_manifest(FIX / "sources_manifest.json")
        for src, ev in out.items:
            self.assertEqual(ev.data["selection"], "operator_selected")
            self.assertEqual(ev.data["source_class"], "operator_selected")
            self.assertEqual(ev.data["retrieved_at"], NOW)
            self.assertEqual(src.retrieved_at, NOW)
            self.assertEqual(ev.data["manifest_sha256"], m.sha256)
            body = self.PAGE if ev.data["url"].endswith(".html") else self.TEXT
            self.assertEqual(ev.data["content_sha256"], hashlib.sha256(body).hexdigest())
            self.assertTrue(any(t.startswith("operator_selected:") for t in ev.transformations))
        titles = {src.title for src, _ in out.items}
        self.assertIn("Zelvapril market note", titles)
        # fetched once per run, whatever the number of questions
        a.gather(contract().questions[1], contract())
        self.assertEqual(len(opener.urls), 2)

    def test_unreadable_mismatched_or_unsupported_sources_are_reported(self):
        expected = hashlib.sha256(b"something else").hexdigest()
        m = parse_manifest(self.manifest_bytes([
            {"url": "https://a.example.org/one.html", "sha256": expected},
            {"url": "https://b.example.org/two.pdf"},
            {"url": "https://c.example.org/three.html"},
        ]))
        a, _ = self.adapter([FakeResponse(body=self.PAGE, ctype="text/html"),
                             FakeResponse(body=b"%PDF-1.7", ctype="application/pdf"),
                             FakeResponse(404)], manifest=m)
        out = a.gather(question(), contract())
        self.assertEqual(out.items, [])
        joined = "\n".join(out.notes)
        self.assertIn("does not match the manifest", joined)
        self.assertIn("unsupported content type 'application/pdf'", joined)
        self.assertIn("HTTP 404", joined)
        self.assertEqual(sum("provider_unavailable" in n for n in out.notes), 3)

    def test_manifest_hash_check_passes_when_content_matches(self):
        m = parse_manifest(self.manifest_bytes([{"url": "https://a.example.org/t.txt",
                                                 "sha256": hashlib.sha256(self.TEXT).hexdigest()}]))
        a, _ = self.adapter([FakeResponse(body=self.TEXT, ctype="text/plain")], manifest=m)
        out = a.gather(question(), contract())
        self.assertTrue(out.items)
        self.assertIs(out.items[0][1].data["manifest_hash_checked"], True)

    def test_fetch_time_ssrf_check_on_manifest_urls(self):
        m = parse_manifest(self.manifest_bytes([{"url": "https://internal.example.org/x"}]))
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))]
        with patch("socket.getaddrinfo", return_value=private):
            a = OperatorSourcesAdapter(m, client=PublicHTTPClient("operator_sources", min_interval_s=0))
            out = a.gather(question(), contract())
        self.assertEqual(out.items, [])
        self.assertTrue(any("refused by the public-fetch guard" in n for n in out.notes), out.notes)


# ---------------------------------------------------------------------------------------------------------------
class WiringTest(unittest.TestCase):
    def test_build_registry_registers_the_public_sources(self):
        with patch("lofgren_intelligence.adapters.public_api.safe_urlopen",
                   side_effect=AssertionError("no fetch at registration")):
            reg = build_registry(europepmc="zelvapril", trials="zelvapril", max_records=7,
                                 sources_manifest=str(FIX / "sources_manifest.json"))
        ids = {a.id for a in reg.all()}
        self.assertEqual(ids, {"europepmc", "clinicaltrials", "operator_sources"})
        self.assertEqual(reg.get("europepmc").max_records, 7)
        self.assertEqual(reg.get("clinicaltrials").max_records, 7)
        self.assertEqual({a.id for a in reg.find("text")}, ids)
        for a in reg.all():
            self.assertEqual(a.authorized_operations, frozenset({"read"}))
            self.assertEqual(a.cost_per_call_usd, 0.0)
        with self.assertRaises(ValueError):
            build_registry(europepmc="zelvapril", max_records=1000)

    def test_cli_flags(self):
        from lofgren_intelligence import cli

        argv = ["estimate", OBJECTIVE, "--europepmc", "zelvapril", "--trials", "zelvapril hypertension",
                "--max-records", "5", "--sources-manifest", str(FIX / "sources_manifest.json")]
        out = io.StringIO()
        with patch("lofgren_intelligence.adapters.public_api.safe_urlopen",
                   side_effect=AssertionError("estimate must not fetch")), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(argv), 0)
        text = out.getvalue()
        self.assertIn("gather tasks", text)
        for cmd in ("investigate", "discover", "produce"):
            parser_args = [cmd, OBJECTIVE, "--europepmc", "q", "--trials", "q", "--max-records", "3",
                           "--sources-manifest", "m.json"]
            if cmd == "produce":
                parser_args += ["--design", "d.json", "--out-dir", "o"]
            ns = self._parse(parser_args)
            self.assertEqual((ns.europepmc, ns.trials, ns.max_records, ns.sources_manifest), ("q", "q", 3, "m.json"))
        for bad in ("0", "101", "x"):
            with self.assertRaises(SystemExit), patch("sys.stderr"):
                self._parse(["investigate", OBJECTIVE, "--max-records", bad])

    @staticmethod
    def _parse(argv):
        from lofgren_intelligence import cli

        captured = {}

        def fake(args):
            captured["ns"] = args
            return 0

        with patch.object(cli, "cmd_investigate", fake), patch.object(cli, "cmd_discover", fake), \
                patch.object(cli, "cmd_produce", fake):
            cli.main(argv)
        return captured["ns"]

    def test_local_mcp_schema_and_registry(self):
        from lofgren_intelligence.mcp.server import TOOLS, Server

        schema = {t["name"]: t for t in TOOLS}["investigate"]["inputSchema"]["properties"]
        self.assertEqual(schema["europepmc"]["type"], "string")
        self.assertEqual(schema["trials"]["type"], "string")
        self.assertEqual((schema["max_records"]["minimum"], schema["max_records"]["maximum"]), (1, 100))
        self.assertEqual(schema["sources_manifest"]["type"], "string")
        reg = Server._registry({"europepmc": "zelvapril", "trials": "zelvapril", "max_records": 4,
                                "sources_manifest": str(FIX / "sources_manifest.json")})
        self.assertEqual({a.id for a in reg.all()}, {"europepmc", "clinicaltrials", "operator_sources"})
        self.assertEqual(reg.get("clinicaltrials").max_records, 4)



# ---------------------------------------------------------------------------------------------------------------
class PipelineRunTest(unittest.TestCase):
    """Owner review (Q24): both adapters take part in an actual LI research run.

    `lofgren investigate` and the discovery pipeline run through build_registry (the CLI path) against the
    recorded fixtures, served by URL through the SSRF-protected fetcher's seam. The evidence behind the
    run's claims must carry its source id, dates, passage and the provenance labels (text_scope, the
    results label, the registered-trial label), and so must the stored result, the report and the receipt.
    """

    LIT = {"peer_reviewed_literature", "preprint"}

    def router(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        host = urllib.parse.urlsplit(url).hostname
        if host == "www.ebi.ac.uk":
            return ok("europepmc_page1.json" if q.get("cursorMark") == "*" else "europepmc_page2.json")
        if host == "clinicaltrials.gov":
            return ok("clinicaltrials_page2.json" if q.get("pageToken") else "clinicaltrials_page1.json")
        raise AssertionError(f"unexpected request {url}")

    def setUp(self):
        self.urls = []
        seam = patch("lofgren_intelligence.adapters.public_api.safe_urlopen", self.router)
        seam.start()
        self.addCleanup(seam.stop)

    def investigate(self, out):
        from lofgren_intelligence import cli

        argv = ["investigate", OBJECTIVE, "--europepmc", "zelvapril", "--trials", "zelvapril",
                "--max-records", "10", "--as-of", NOW,
                "--out", str(out / "report.md"), "--json", str(out / "run.json"), "--receipt", str(out / "receipt.json")]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(argv), 0)
        return (json.loads((out / "run.json").read_text(encoding="utf-8")),
                (out / "report.md").read_text(encoding="utf-8"),
                json.loads((out / "receipt.json").read_text(encoding="utf-8")))

    def test_investigate_carries_labelled_evidence_into_the_result_report_and_receipt(self):
        import tempfile

        from lofgren_intelligence.kernel.receipt import verify_receipt

        with tempfile.TemporaryDirectory() as tmp:
            stored, report, receipt = self.investigate(Path(tmp))
        hosts = {urllib.parse.urlsplit(u).hostname for u in self.urls}
        self.assertEqual(hosts, {"www.ebi.ac.uk", "clinicaltrials.gov"}, "both adapters fetched")

        graph = stored["evidence_graph"]
        sources = {src["id"]: src for src in graph["sources"]}
        evidence = {e["id"]: e for e in graph["evidence"]}
        claims = {c["id"]: c for c in graph["claims"]}
        lit = {i: e for i, e in evidence.items() if e["data"].get("source_class") in self.LIT}
        trials = {i: e for i, e in evidence.items() if e["data"].get("source_class") == "trial_registry"}
        self.assertEqual(len(lit), 3)
        self.assertEqual(len(trials), 3)
        self.assertEqual(set(evidence), set(lit) | set(trials))

        # Stored result: every evidence item carries its source id, dates, passage and labels.
        for eid, e in lit.items():
            with self.subTest(evidence=eid):
                src = sources[e["source_id"]]
                self.assertTrue(e["content"].strip())
                self.assertIn(e["data"]["text_scope"], ("abstract_only", "title_only"))
                self.assertIs(e["data"]["full_text_fetched"], False)
                self.assertEqual(e["observed_at"], e["data"]["publication_date"])
                self.assertEqual(src["published_at"], e["data"]["publication_date"])
                self.assertTrue(src["retrieved_at"])
                self.assertEqual(e["data"]["retrieval"]["retrieved_at"], src["retrieved_at"])
        for eid, e in trials.items():
            with self.subTest(evidence=eid):
                self.assertTrue(e["content"].startswith("ClinicalTrials.gov reports that trial "))
                self.assertEqual(e["data"]["evidence_label"], EVIDENCE_LABEL)
                self.assertIn(e["data"]["results_label"], ("results_posted_not_fetched", "no_results_posted"))
                self.assertEqual(e["data"]["results_label"] == "results_posted_not_fetched",
                                 e["data"]["results_posted"])
                self.assertIs(e["data"]["results_fetched"], False)
                self.assertTrue(sources[e["source_id"]]["retrieved_at"])
        self.assertEqual({e["data"]["results_label"] for e in trials.values()},
                         {"results_posted_not_fetched", "no_results_posted"})
        self.assertEqual({e["data"]["text_scope"] for e in lit.values()}, {"abstract_only", "title_only"})

        # Claims rest on both adapters' evidence; the findings cite claims whose evidence is labelled.
        support = {cid: {evidence[x]["data"]["source_class"] for x in c["supporting"]} for cid, c in claims.items()}
        self.assertTrue(any(classes & self.LIT for classes in support.values()))
        self.assertTrue(any("trial_registry" in classes for classes in support.values()))
        cited = [cid for f in stored["findings"] for cid in f["claim_ids"]]
        self.assertTrue(cited)
        for cid in cited:
            for x in claims[cid]["supporting"]:
                self.assertTrue(evidence[x]["data"].get("source_class"))
                if evidence[x]["data"]["source_class"] == "trial_registry":
                    self.assertEqual(evidence[x]["data"]["evidence_label"], EVIDENCE_LABEL)

        # Receipt: intact, and each evidence entry carries the same source id, labels, dates and passage.
        self.assertTrue(verify_receipt(receipt))
        self.assertEqual(receipt["research_id"], stored["research_id"])
        r_evidence = {e["id"]: e for e in receipt["evidence"]}
        self.assertEqual(set(r_evidence), set(evidence))
        self.assertEqual({src["id"] for src in receipt["sources"]}, set(sources))
        for eid, e in evidence.items():
            with self.subTest(receipt_evidence=eid):
                r = r_evidence[eid]
                self.assertEqual(r["source_id"], e["source_id"])
                self.assertEqual(r["passage"], e["content"])
                self.assertEqual(r["content_hash"], hashlib.sha256(r["passage"].encode()).hexdigest())
                self.assertEqual(r["labels"], {k: e["data"][k] for k in r["labels"]})
                self.assertEqual(r["labels"]["source_class"], e["data"]["source_class"])
                self.assertEqual(r["dates"]["retrieved_at"], sources[e["source_id"]]["retrieved_at"])
                if eid in lit:
                    self.assertEqual(r["labels"]["text_scope"], e["data"]["text_scope"])
                    self.assertEqual(r["dates"]["observed_at"], e["observed_at"])
                else:
                    self.assertEqual(r["labels"]["evidence_label"], EVIDENCE_LABEL)
                    self.assertEqual(r["labels"]["results_label"], e["data"]["results_label"])
                    for k in ("start_date", "primary_completion_date", "completion_date"):
                        if e["data"].get(k):
                            self.assertEqual(r["dates"][k], e["data"][k])
        r_claims = {c["id"]: c for c in receipt["claims"]}
        for cid in cited:
            self.assertEqual(r_claims[cid]["supporting"], claims[cid]["supporting"])

        # Report: every labelled evidence item is listed under its source with its labels, dates and passage.
        self.assertIn(receipt["research_id"], report)
        for eid, e in evidence.items():
            with self.subTest(report_evidence=eid):
                line = next(x for x in report.splitlines() if f"evidence `{eid}`" in x)
                self.assertIn(f"source `{e['source_id']}`", line)
                self.assertIn(f"source_class={e['data']['source_class']}", line)
                self.assertIn(f"retrieved_at {sources[e['source_id']]['retrieved_at']}", line)
                if eid in lit:
                    self.assertIn(f"text_scope={e['data']['text_scope']}", line)
                    self.assertIn(f"observed_at {e['observed_at']}", line)
                else:
                    self.assertIn(f"evidence_label={EVIDENCE_LABEL}", line)
                    self.assertIn(f"results_label={e['data']['results_label']}", line)
                self.assertIn("passage: “" + " ".join(e["content"].split()) + "”", report)
        self.assertIn(sources[next(iter(trials.values()))["source_id"]]["uri"], report)

    def test_discovery_runs_on_the_labelled_research(self):
        from lofgren_intelligence.discovery.pipeline import discover_from_run
        from lofgren_intelligence.kernel.knowledge_map import export_knowledge_map

        reg = build_registry(europepmc="zelvapril", trials="zelvapril", max_records=10)
        self.assertEqual({a.id for a in reg.all()}, {"europepmc", "clinicaltrials"})
        result = run_investigation(contract(), reg, plan_id="payg", verifier=Verifier(now=VERIFY_AT))
        self.assertTrue(result.completed)
        classes = {e.data.get("source_class") for e in result.graph.evidence.values()}
        self.assertEqual(classes, {"peer_reviewed_literature", "preprint", "trial_registry"})
        labelled = [e for e in result.receipt["evidence"] if "labels" in e]
        self.assertEqual(len(labelled), len(result.graph.evidence))
        discovery = discover_from_run(result, OBJECTIVE)
        # The discovery rests on exactly this research and its knowledge state.
        self.assertEqual(discovery.receipt["evidence"]["research_id"], result.receipt["research_id"])
        self.assertEqual(discovery.receipt["evidence"]["knowledge_state_hash"], result.receipt["knowledge_state_hash"])
        km = export_knowledge_map(result)
        self.assertEqual({e["id"] for e in km["evidence"]}, set(result.graph.evidence))
        self.assertEqual({e["source_id"] for e in km["evidence"]}, set(result.graph.sources))


if __name__ == "__main__":
    unittest.main()
