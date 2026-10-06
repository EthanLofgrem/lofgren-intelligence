"""Public website and research-console SHELL: structure, truthfulness and accessibility.

The console is illustrative only. These tests pin that every console page says
so, that no result is summarized as a bare confidence percentage, that paid
plans are labelled as planned, that capability claims stay inside the
capability manifest, and that the pages keep a basic accessible structure.
"""

from __future__ import annotations

import os
import re
import unittest
import urllib.parse
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from lofgren_intelligence.billing.pricing import BASE_UNIT_USD, PLANS
from lofgren_intelligence.hosted import capabilities, console, site, web_app

ENV = {
    "LI_PUBLIC_BASE_URL": "https://li.example",
    "SUPABASE_URL": "https://project.supabase.example",
    "SUPABASE_PUBLISHABLE_KEY": "publishable-test-key",
}

PUBLIC_PAGES = {
    "/": ("Evidence-first research", "Lofgren Intelligence"),
    "/product": ("Product", "Product"),
    "/pricing": ("Pricing", "Pricing"),
    site.MCP_PAGE: ("MCP", "Connect through MCP"),
    "/docs": ("Docs", "Documentation"),
    "/about": ("About", "About Lofgren Intelligence"),
}
CONSOLE_PAGES = {
    "/app": ("Research console", "Research console"),
    "/app/research/new": ("New research", "New research"),
    "/app/research/demo-warehouse": ("Demo case", "Should I open a warehouse in Phoenix?"),
    "/app/receipts": ("Research receipts", "Research receipts"),
    "/app/library": ("Knowledge library", "Knowledge library"),
    "/app/usage": ("Usage and quota", "Usage & quota"),
    "/app/settings": ("Settings", "Settings"),
}
ALL_PAGES = {**PUBLIC_PAGES, **CONSOLE_PAGES}

BANNER = "Illustrative demo data — not a live research result."
PLANNED = "Planned — not yet available; paid checkout opens after pricing is approved."

GOVERNED_MESSAGES = {
    "Needs clarification": "We need more context before research can begin.",
    "Insufficient evidence": "Available evidence is not enough to support a conclusion.",
    "Contradicted": "Reliable sources materially disagree. Review both sides.",
    "Paused for budget": "The next stage exceeds your approved research budget.",
    "Completed with limitations": "Research is complete for the approved scope; important limits remain.",
    "Failed safely": "The case did not complete. No conclusion was produced.",
    "Quota reached": "You've reached your current usage limit.",
}

# Overclaiming phrases (checked against visible text). The capability-manifest
# CONTRADICTIONS patterns from the round-2 docs tests are applied as well.
OVERCLAIMS = (
    r"\bworks (with|in) (ChatGPT|Claude|Codex)\b",
    r"\b(fully|officially|certified) compatible\b",
    r"\bcompatible with (ChatGPT|Claude|Codex)\b",
    r"\b(ChatGPT|Claude|Codex) (is|are) (supported|verified|compatible)\b",
    r"\bproduction[- ]ready\b",
    r"\bnow (live|available)\b",
    r"\b(generally|publicly) available\b",
    r"\bbackups? (are|is) (enabled|automatic|tested|verified|in place)\b",
    r"\bautomatic(ally)? backed up\b",
    r"\b(restore|backup/restore) (is|are) (tested|verified|supported)\b",
    r"\bPublicMCPReady\s*(passed|met|achieved)\b",
    r"\b(is|are) (now )?(live|deployed) (at|on)\b",
    r"\bsubscribe now\b",
    r"\bguarantee[sd]?\b",
    r"\b\d{1,3}(\.\d+)?\s*%\s*(confidence|confident|accurate|accuracy)\b",
    r"\bconfidence\W{0,40}\d{1,3}(\.\d+)?\s*%",
)


class _Doc(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text: list[str] = []
        self.title = ""
        self.h1: list[str] = []
        self._stack: list[str] = []
        self._capture: list[str] | None = None
        self.labels_for: set[str] = set()
        self._label_depth = 0
        self.controls_in_label: set[int] = set()

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        self.tags.append((tag, a))
        if tag == "label":
            self._label_depth += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea") and self._label_depth:
            self.controls_in_label.add(len(self.tags) - 1)
        if tag in ("title", "h1"):
            self._stack.append(tag)
            self._capture = []

    def handle_endtag(self, tag):
        if tag == "label":
            self._label_depth = max(0, self._label_depth - 1)
        if self._stack and tag == self._stack[-1]:
            text = re.sub(r"\s+", " ", "".join(self._capture or [])).strip()
            if tag == "title":
                self.title = text
            else:
                self.h1.append(text)
            self._stack.pop()
            self._capture = None

    def handle_data(self, data):
        self.text.append(data)
        if self._capture is not None:
            self._capture.append(data)

    def visible_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text))


def parse(page: str) -> _Doc:
    doc = _Doc()
    doc.feed(page)
    return doc


class WebShellTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls._env = patch.dict(os.environ, ENV)
        cls._env.start()
        cls.client = TestClient(web_app.build_app(), base_url="https://li.example")
        cls.pages = {}
        for path in ALL_PAGES:
            response = cls.client.get(path)
            cls.pages[path] = response

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def html(self, path: str) -> str:
        return self.pages[path].text

    def doc(self, path: str) -> _Doc:
        return parse(self.html(path))


class PageTests(WebShellTestCase):
    def test_every_page_returns_200_with_title_and_one_h1(self):
        for path, (title, h1) in ALL_PAGES.items():
            with self.subTest(path=path):
                response = self.pages[path]
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith("text/html"))
                doc = self.doc(path)
                self.assertEqual(doc.title, f"{title} · Lofgren Intelligence")
                self.assertEqual(doc.h1, [h1])

    def test_pages_send_a_strict_csp_whose_nonce_matches_inline_styles(self):
        for path in ALL_PAGES:
            with self.subTest(path=path):
                policy = self.pages[path].headers["content-security-policy"]
                self.assertIn("script-src 'self';", policy)
                self.assertIn("default-src 'none'", policy)
                self.assertNotIn("unsafe-inline", policy)
                nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
                for tag, attrs in self.doc(path).tags:
                    if tag == "style":
                        self.assertEqual(attrs.get("nonce"), nonce)
                    if tag == "script":
                        self.assertTrue(attrs.get("src", "").startswith("/static/"), attrs)

    def test_public_pages_have_the_nav_and_footer(self):
        for path in PUBLIC_PAGES:
            with self.subTest(path=path):
                doc = self.doc(path)
                links = {(a.get("href"), ) for t, a in doc.tags if t == "a"}
                for href in ("/product", "/pricing", site.MCP_PAGE, "/docs", "/about", "/account"):
                    self.assertIn((href,), links)
                text = doc.visible_text()
                for label in ("Product", "Pricing", "MCP", "Docs", "About", "Sign in", "Get started",
                              "Documentation", "Account", "Billing", "Privacy", "Terms", "API & MCP docs"):
                    self.assertIn(label, text)

    def test_internal_links_resolve_including_fragments(self):
        checked: dict[str, str] = {}
        for path in ALL_PAGES:
            for tag, attrs in self.doc(path).tags:
                if tag != "a":
                    continue
                href = attrs.get("href", "")
                parts = urllib.parse.urlsplit(href)
                if parts.scheme or parts.netloc:
                    self.fail(f"external link on {path}: {href}")
                target = parts.path or path
                with self.subTest(page=path, href=href):
                    if target not in checked:
                        response = self.client.get(target)
                        self.assertEqual(response.status_code, 200, target)
                        checked[target] = response.text
                    if parts.fragment:
                        ids = {a.get("id") for _, a in parse(checked[target]).tags}
                        self.assertIn(parts.fragment, ids)

    def test_mcp_transport_path_is_not_shadowed_by_the_site(self):
        self.assertNotEqual(site.MCP_PAGE, "/mcp")
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                    headers={"Accept": "application/json, text/event-stream"})
        self.assertEqual(response.status_code, 401)

    def test_existing_routes_are_still_registered(self):
        import inspect
        source = inspect.getsource(web_app.build_app)
        for path in ("/healthz", "/readyz", "/.well-known/oauth-protected-resource",
                     "/.well-known/oauth-authorization-server", "/oauth/register", "/oauth/authorize",
                     "/oauth/authorize/complete", "/oauth/token", "/stripe/webhook", "/account",
                     "/account/export", "/account/delete", "/billing/success", "/billing/cancelled"):
            self.assertIn(f'Route("{path}"', source)
        self.assertEqual(self.client.get("/.well-known/oauth-authorization-server").status_code, 200)
        self.assertEqual(self.client.get("/billing/success").status_code, 200)


class TruthfulnessTests(WebShellTestCase):
    def test_every_console_page_shows_the_demo_banner(self):
        for path in CONSOLE_PAGES:
            with self.subTest(path=path):
                page = self.html(path)
                self.assertIn(BANNER, page)
                banner = re.search(r'<p class="demo-banner"[^>]*>.*?</p>', page, re.S).group(0)
                self.assertNotRegex(banner, r"(?i)<button|dismiss|close")
                # The banner is inside <main>, before any page content.
                self.assertLess(page.index('<main id="main"'), page.index(BANNER))
        # The sample result on the home page is labelled too.
        self.assertIn(BANNER, self.html("/"))

    def test_no_bare_confidence_percentage_anywhere(self):
        for path in ALL_PAGES:
            with self.subTest(path=path):
                text = self.doc(path).visible_text()
                self.assertIsNone(re.search(r"(?i)confidence\W{0,40}\d{1,3}(\.\d+)?\s*%", text))
                self.assertIsNone(re.search(r"(?i)\d{1,3}(\.\d+)?\s*%\s*confiden", text))

    def test_demo_case_uses_the_layered_assurance_model(self):
        text = self.doc("/app/research/demo-warehouse").visible_text()
        for expected in ("Assurance", "PROVISIONAL", "Evidence coverage", "Well supported",
                         "Partially supported", "Uncertain", "Contradicted", "Contradiction exposure",
                         "Decision-relevant unknowns", "Scope"):
            self.assertIn(expected, text)

    def test_demo_identifiers_are_labelled(self):
        for path in CONSOLE_PAGES:
            with self.subTest(path=path):
                text = self.doc(path).visible_text()
                # Nothing that looks like a real receipt id or hash fingerprint.
                self.assertIsNone(re.search(r"\b(rct|kf|run|li)_[0-9a-z]{4,}", text, re.I))
                self.assertIsNone(re.search(r"\b[0-9a-f]{12,}\b", text))
                for match in re.finditer(r"DEMO-RECEIPT-\d+", text):
                    self.assertIn("demo", text[match.end():match.end() + 12].lower())

    def test_research_receipt_values_are_all_labelled_demo(self):
        page = self.html("/app/research/demo-warehouse")
        receipt = re.search(r'<aside class="panel receipt".*?</aside>', page, re.S).group(0)
        values = re.findall(r"<dd>(.*?)</dd>", receipt, re.S)
        self.assertGreaterEqual(len(values), 8)
        for value in values:
            self.assertIn('<span class="demo-tag">demo</span>', value)

    def test_governed_states_and_messages(self):
        for path in ("/app", "/product", "/docs"):
            text = self.doc(path).visible_text()
            for name, message in GOVERNED_MESSAGES.items():
                with self.subTest(path=path, state=name):
                    self.assertIn(name, text)
                    self.assertIn(message, text)

    def test_status_is_never_color_alone(self):
        for path in CONSOLE_PAGES:
            page = self.html(path)
            pills = re.findall(r'<span class="pill pill-[a-z]+">(?:<span aria-hidden="true">[^<]*</span>)?([^<]*)</span>',
                               page)
            self.assertEqual(len(pills), page.count('<span class="pill pill-'))
            for label in pills:
                with self.subTest(path=path, pill=label):
                    self.assertRegex(label, r"[A-Za-z]{3,}")
        page = self.html("/app/research/demo-warehouse")
        for label in ("Well supported", "Partially supported", "Uncertain", "Contradicted"):
            self.assertIn(label, page)

    def test_paid_plans_are_labelled_planned(self):
        page = self.html("/pricing")
        doc = parse(page)
        text = doc.visible_text()
        for plan_id in ("payg", "researcher", "good-idea"):
            with self.subTest(plan=plan_id):
                card = re.search(rf'<article class="plan" id="plan-{plan_id}".*?</article>', page, re.S).group(0)
                self.assertIn(PLANNED, parse(card).visible_text())
        free = re.search(r'<article class="plan" id="plan-founding-free".*?</article>', page, re.S).group(0)
        self.assertNotIn("Planned", free)
        self.assertIn("first 1,000", parse(free).visible_text())
        for figure in ("$0", "1–1,000", "$0.0312", "per work unit", "Heavy work: $12.48 each",
                       "$49.99", "$0.0156", "$79.99", "$0.0050",
                       "500 intelligence units per rolling 7 days (UTC)", "Allowance: not yet decided."):
            self.assertIn(figure, text)
        # Unenforced "entries" figures are no longer published as allowances.
        for stale in ("400 weekly entries", "20,000 monthly entries", "per prompted research"):
            self.assertNotIn(stale, text)
        # The home page summary carries the same labels.
        self.assertEqual(self.doc("/").visible_text().count(PLANNED), 3)

    def test_pricing_figures_match_the_billing_module(self):
        self.assertAlmostEqual(BASE_UNIT_USD, 0.0312)
        self.assertAlmostEqual(PLANS["payg"].heavy_price, 12.48)
        self.assertAlmostEqual(PLANS["researcher"].monthly_fee, 49.99)
        self.assertEqual((PLANS["researcher"].entry_limit, PLANS["researcher"].limit_period), (400, "week"))
        self.assertAlmostEqual(PLANS["researcher"].rate, 0.0156)
        self.assertAlmostEqual(PLANS["good_idea"].monthly_fee, 79.99)
        self.assertEqual((PLANS["good_idea"].entry_limit, PLANS["good_idea"].limit_period), (20_000, "month"))
        self.assertAlmostEqual(PLANS["good_idea"].rate, 0.005)

    def test_no_overclaiming(self):
        from tests.test_public_ops_hardening import CapabilityManifestTests

        patterns = OVERCLAIMS + CapabilityManifestTests.CONTRADICTIONS
        for path in ALL_PAGES:
            text = self.doc(path).visible_text()
            for pattern in patterns:
                with self.subTest(path=path, pattern=pattern):
                    self.assertIsNone(re.search(pattern, text, re.IGNORECASE))

    def test_mcp_clients_are_described_as_being_verified(self):
        text = self.doc(site.MCP_PAGE).visible_text()
        for client in ("ChatGPT", "Claude", "Codex", "Other MCP clients"):
            self.assertIn(client, text)
        self.assertGreaterEqual(text.count("Designed to connect via MCP; client compatibility is being verified."), 4)
        self.assertIn("https://li.example/mcp", text)

    def test_docs_tools_match_the_capability_manifest(self):
        page = self.html("/docs")
        listed = set(re.findall(r"<li><code>([a-z0-9_]+)</code>", page))
        self.assertEqual(listed, set(capabilities.TOOL_LEVELS))
        text = parse(page).visible_text()
        for item in capabilities.NOT_PROVEN:
            self.assertIn(item, text)

    def test_home_states_what_is_not_proven(self):
        text = self.doc("/").visible_text().lower()
        for phrase in ("public readiness is a separate gate", "backup/restore", "real-client"):
            self.assertIn(phrase, text)


class DemoCaseTests(WebShellTestCase):
    PATH = "/app/research/demo-warehouse"

    def test_case_sections(self):
        text = self.doc(self.PATH).visible_text()
        for expected in ("Answer", "Claims", "Evidence", "Contradictions", "Unknowns", "Gaps", "Recommendations",
                         "What could change this answer?", "Unknowns register", "Assumptions register",
                         "Research Receipt"):
            self.assertIn(expected, text)

    def test_claims_have_a_pill_and_a_text_label(self):
        page = self.html(self.PATH)
        table = re.search(r"<caption>Claims.*?</table>", page, re.S).group(0)
        rows = re.findall(r"<tr><th scope=\"row\">.*?</tr>", table, re.S)
        self.assertGreaterEqual(len(rows), 5)
        for row in rows:
            self.assertRegex(row, r'class="pill pill-(well|partial|uncertain|contradicted)"')
            self.assertRegex(row, r"(Well supported|Partially supported|Uncertain|Contradicted)")

    def test_contradiction_detail(self):
        text = self.doc(self.PATH).visible_text()
        for expected in ("Claim A", "Claim B", "SCOPE_DEPENDENT", "Contradiction type", "Why the evidence differs",
                         "Impact on the decision", "Evidence that would resolve it"):
            self.assertIn(expected, text)

    def test_registers_have_the_required_columns(self):
        page = self.html(self.PATH)
        for caption in ("Unknowns register (demo)", "Assumptions register (demo)"):
            table = re.search(rf"<caption>{re.escape(caption)}</caption>.*?</table>", page, re.S).group(0)
            for column in ("Materiality", "Affects", "How to resolve", "Status"):
                self.assertIn(f">{column}</th>", table)

    def test_four_stage_progress(self):
        page = self.html(self.PATH)
        progress = re.search(r'<ol class="progress".*?</ol>', page, re.S).group(0)
        stages = re.findall(r"<strong>\d\. (\w+)</strong>", progress)
        self.assertEqual(stages, ["Research", "Verify", "Discover", "Receipt"])

    def test_tabs_follow_the_aria_tablist_pattern_and_work_without_js(self):
        doc = self.doc(self.PATH)
        lists = [a for t, a in doc.tags if a.get("role") == "tablist"]
        self.assertEqual(len(lists), 1)
        self.assertTrue(lists[0].get("aria-label"))
        tabs = [(t, a) for t, a in doc.tags if a.get("role") == "tab"]
        panels = {a["id"]: a for t, a in doc.tags if a.get("role") == "tabpanel"}
        self.assertEqual([a["aria-controls"] for _, a in tabs],
                         [f"panel-{k}" for k in ("evidence", "contradictions", "unknowns", "gaps", "recommendations")])
        self.assertEqual(sum(a.get("aria-selected") == "true" for _, a in tabs), 1)
        for tag, attrs in tabs:
            self.assertEqual(tag, "a")
            # Without JS each tab is an in-page link to its (visible) panel.
            self.assertEqual(attrs["href"], "#" + attrs["aria-controls"])
            panel = panels[attrs["aria-controls"]]
            self.assertEqual(panel["aria-labelledby"], attrs["id"])
            self.assertNotIn("hidden", panel)

    def test_tab_script_supports_arrow_keys(self):
        script = site.static_bytes("site.js").decode("utf-8")
        for key in ("ArrowRight", "ArrowLeft", "Home", "End", "aria-selected", "tabindex"):
            self.assertIn(key, script)


class NewResearchPrototypeTests(WebShellTestCase):
    PATH = "/app/research/new"

    def test_prototype_note_and_no_backend(self):
        page = self.html(self.PATH)
        text = parse(page).visible_text()
        self.assertIn("Prototype — the live clarification interview is in development.", text)
        self.assertNotIn("<form", page)
        for tag, attrs in parse(page).tags:
            if tag == "button":
                self.assertEqual(attrs.get("type"), "button")
        script = site.static_bytes("site.js").decode("utf-8")
        for call in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource"):
            self.assertNotIn(call, script)

    def test_objective_input(self):
        doc = self.doc(self.PATH)
        textareas = [a for t, a in doc.tags if t == "textarea"]
        self.assertEqual(textareas[0]["id"], "objective")
        self.assertIn("objective", doc.labels_for)

    def test_clarification_questions(self):
        page = self.html(self.PATH)
        questions = re.findall(r'<li class="question">.*?</li>', page, re.S)
        self.assertTrue(3 <= len(questions) <= 7, len(questions))
        for q in questions:
            text = parse(q).visible_text()
            self.assertIn("Why we ask:", text)
            for choice in ("Answer", "Unknown", "Skip", "Use a reasonable default", "Ask me later"):
                self.assertIn(choice, text)

    def test_case_charter_is_ready_but_not_approved(self):
        text = self.doc(self.PATH).visible_text()
        self.assertIn("Case Charter", text)
        self.assertIn("READY_FOR_SCOPE_APPROVAL", text)
        self.assertIn("Not approved", text)
        self.assertIsNone(re.search(r"(?i)\bstatus\W+approved\b", text))

    def test_scope_builder_fields(self):
        text = self.doc(self.PATH).visible_text()
        for field in ("Outcome type", "Location", "Time range", "Allowed source classes", "Depth", "Budget",
                      "Time limit", "Out-of-scope actions"):
            self.assertIn(field, text)


class AccessibilityTests(WebShellTestCase):
    def test_landmarks_skip_link_and_language(self):
        for path in ALL_PAGES:
            with self.subTest(path=path):
                doc = self.doc(path)
                tags = [t for t, _ in doc.tags]
                for landmark in ("header", "nav", "main", "footer"):
                    self.assertIn(landmark, tags)
                self.assertEqual(tags.count("main"), 1)
                html_tag = next(a for t, a in doc.tags if t == "html")
                self.assertEqual(html_tag.get("lang"), "en")
                first_link = next(a for t, a in doc.tags if t == "a")
                self.assertEqual(first_link.get("href"), "#main")
                self.assertEqual(first_link.get("class"), "skip-link")
                self.assertIn(("main", {"id": "main", "tabindex": "-1"}), doc.tags)
                navs = [a for t, a in doc.tags if t == "nav"]
                for nav in navs:
                    self.assertTrue(nav.get("aria-label"))

    def test_every_form_control_is_labelled(self):
        for path in ALL_PAGES:
            doc = self.doc(path)
            for index, (tag, attrs) in enumerate(doc.tags):
                if tag not in ("input", "select", "textarea"):
                    continue
                with self.subTest(path=path, control=attrs.get("id") or attrs):
                    labelled = (attrs.get("id") in doc.labels_for or index in doc.controls_in_label
                                or attrs.get("aria-label") or attrs.get("aria-labelledby"))
                    self.assertTrue(labelled)

    def test_images_have_alt_and_svgs_are_decorative_or_named(self):
        for path in ALL_PAGES:
            doc = self.doc(path)
            for tag, attrs in doc.tags:
                with self.subTest(path=path, tag=tag):
                    if tag == "img":
                        self.assertIn("alt", attrs)
                    if tag == "svg":
                        self.assertTrue(attrs.get("aria-hidden") == "true" or attrs.get("aria-label")
                                        or attrs.get("role") == "img")

    def test_no_inline_handlers_styles_or_external_assets(self):
        for path in ALL_PAGES:
            page = self.html(path)
            with self.subTest(path=path):
                self.assertIsNone(re.search(r"<[^>]+\son[a-z]+\s*=", page, re.I))
                self.assertIsNone(re.search(r"<[^>]+\sstyle\s*=", page, re.I))
                self.assertIsNone(re.search(r"javascript:", page, re.I))
                for tag, attrs in parse(page).tags:
                    for attr in ("src", "href"):
                        value = attrs.get(attr, "")
                        if tag in ("script", "link", "img"):
                            self.assertFalse(value.startswith(("http:", "https:", "//")), value)

    def test_visible_focus_and_responsive_rules(self):
        css = site.static_bytes("site.css").decode("utf-8")
        self.assertIn(":focus-visible", css)
        self.assertIn("outline: 3px solid", css)
        self.assertIn("@media (max-width: 600px)", css)
        self.assertIn("--gutter: 16px", css)
        self.assertIn(".js .collapsible:not(.is-open)", css)  # sidebar/menu collapse only with JS
        for path in ALL_PAGES:
            self.assertIn('name="viewport" content="width=device-width,initial-scale=1"', self.html(path))

    # WCAG 2.x contrast for the palette in static/site.css (text >= 4.5, UI >= 3).
    PALETTE = (
        ("#0B1B3F", "#F6F8FC", 4.5), ("#0B1B3F", "#FFFFFF", 4.5), ("#0B1B3F", "#EEF3FA", 4.5),
        ("#4A5878", "#FFFFFF", 4.5), ("#4A5878", "#F6F8FC", 4.5), ("#4A5878", "#EEF3FA", 4.5),
        ("#1D4ED8", "#FFFFFF", 4.5), ("#1D4ED8", "#EEF3FA", 4.5), ("#FFFFFF", "#1D4ED8", 4.5),
        ("#FFFFFF", "#1E40AF", 4.5), ("#065F46", "#D1FAE5", 4.5), ("#1E40AF", "#DBEAFE", 4.5),
        ("#7C4A03", "#FEF3C7", 4.5), ("#9F1239", "#FFE4E6", 4.5), ("#5B21B6", "#EDE9FE", 4.5),
        ("#115E59", "#CCFBF1", 4.5), ("#374151", "#E5E7EB", 4.5), ("#5C3B00", "#FFF4D6", 4.5),
        ("#E6ECF7", "#0B1B3F", 4.5), ("#A9B7D0", "#0B1B3F", 4.5), ("#BFDBFE", "#0B1B3F", 4.5),
        ("#0F766E", "#FFFFFF", 4.5), ("#3B1585", "#EDE9FE", 4.5),
        ("#1D4ED8", "#FFFFFF", 3.0), ("#7DD3FC", "#0B1B3F", 3.0), ("#6B7A99", "#FFFFFF", 3.0),
    )

    @staticmethod
    def _ratio(fg: str, bg: str) -> float:
        def lum(value: str) -> float:
            channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def test_palette_meets_wcag_aa(self):
        css = site.static_bytes("site.css").decode("utf-8").upper()
        for fg, bg, minimum in self.PALETTE:
            with self.subTest(fg=fg, bg=bg):
                self.assertIn(fg, css)
                self.assertIn(bg, css)
                self.assertGreaterEqual(self._ratio(fg, bg), minimum)


class StaticAssetTests(WebShellTestCase):
    def test_static_assets_are_served(self):
        for name, ctype in site.STATIC_TYPES.items():
            with self.subTest(name=name):
                response = self.client.get(f"/static/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], ctype)
                self.assertEqual(response.content, site.static_bytes(name))
                self.assertIn("nosniff", response.headers.get("x-content-type-options", ""))

    def test_unknown_or_traversal_paths_are_refused(self):
        for path in ("/static/missing.css", "/static/..%2Fsite.py", "/static/site.py", "/static/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_static_files_ship_as_package_data(self):
        root = resources.files("lofgren_intelligence.hosted").joinpath("static")
        for name in site.STATIC_TYPES:
            self.assertTrue(root.joinpath(name).is_file(), name)
        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.setuptools.package-data]", pyproject)
        self.assertIn('"lofgren_intelligence.hosted" = ["static/*.css", "static/*.js", "static/*.svg"]', pyproject)

    def test_console_routes_are_static(self):
        self.assertEqual(set(console.PAGES), set(CONSOLE_PAGES))
        import inspect
        source = inspect.getsource(console)
        for forbidden in ("SupabaseStore", "PublicService", "import os", "urllib", "requests", "fetch("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
