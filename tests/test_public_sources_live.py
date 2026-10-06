"""Opt-in live smoke test for the free public-source adapters (Q24).

One tiny real query to Europe PMC and one to ClinicalTrials.gov, through the SSRF-protected fetcher. It runs
only when LI_LIVE_SMOKE=1 and is skipped otherwise, so CI never depends on a third-party API. It lives in its
own module, with no pinned instant, so the clock-shift check (tests/test_clock_drift.py) does not run it.
"""

import os
import unittest

from lofgren_intelligence.adapters.clinicaltrials import EVIDENCE_LABEL, ClinicalTrialsAdapter
from lofgren_intelligence.adapters.europepmc import EuropePMCAdapter
from lofgren_intelligence.adapters.public_api import OUTCOME_COMPLETE


@unittest.skipUnless(os.environ.get("LI_LIVE_SMOKE") == "1", "live public-API smoke test: set LI_LIVE_SMOKE=1 to run")
class LiveSmokeTest(unittest.TestCase):
    """One tiny real query to each API (opt-in; skipped in CI by design)."""

    def test_europepmc_live(self):
        a = EuropePMCAdapter("metformin AND PUB_TYPE:\"systematic review\"", max_records=2, max_pages=1)
        r = a.retrieve()
        self.assertEqual(r.outcome, OUTCOME_COMPLETE, r.errors)
        self.assertEqual(r.records, 2)
        self.assertTrue(all(x["pmid"] or x["doi"] for x in a.records + a.excluded))
        self.assertTrue(all(x["text_scope"] in ("abstract_only", "title_only") for x in a.records))

    def test_clinicaltrials_live(self):
        a = ClinicalTrialsAdapter("metformin", max_records=2, max_pages=1)
        r = a.retrieve()
        self.assertEqual(r.outcome, OUTCOME_COMPLETE, r.errors)
        self.assertEqual(r.records, 2)
        self.assertTrue(all(x["nct_id"].startswith("NCT") for x in a.records))
        self.assertTrue(all(x["evidence_label"] == EVIDENCE_LABEL for x in a.records))


if __name__ == "__main__":
    unittest.main()
