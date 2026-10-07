"""Europe PMC literature search (capability: "text"; source class: peer-reviewed literature).

    GET https://www.ebi.ac.uk/europepmc/webservices/rest/search
        ?query=<q>&format=json&resultType=core&pageSize=<n>&cursorMark=<*|next>

Free, keyless REST API. Results are paged with cursorMark: the first request sends ``*`` and each response
carries ``nextCursorMark``; pagination stops at the record or page bound, when the cursor stops moving or
when a page comes back empty.

Labels on every record (evidence ``data``):

- ``text_scope`` is ``abstract_only`` (or ``title_only`` when there is no abstract). Full text is never
  fetched by this adapter, so ``full_text_fetched`` is always False; ``open_access`` and
  ``full_text_in_europepmc`` only say that full text exists.
- ``retracted`` / ``corrected`` / ``expression_of_concern`` are True only when Europe PMC says so, from the
  ``pubTypeList`` ("Retracted Publication") or the ``commentCorrectionList`` ("Retraction in", "Erratum in",
  ...). ``correction_signals_checked`` lists which of those fields the response actually carried; an
  absent field is not evidence that a paper is clean. Retracted publications are excluded from the
  evidence graph and reported as notes.
- ``source_class`` is ``peer_reviewed_literature`` for journal records and ``preprint`` for preprints
  (source PPR), which also get a lower quality prior.
"""

from __future__ import annotations

import re
from typing import Any

from ..evidence.types import Evidence, EvidenceKind, Source, SourceKind
from .documents import strip_html
from .public_api import (
    ProviderUnavailable,
    PublicHTTPClient,
    PublicRecordAdapter,
    Retrieval,
    iso_date,
    text_list,
)

ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SOURCE_CLASS_LITERATURE = "peer_reviewed_literature"
SOURCE_CLASS_PREPRINT = "preprint"
MAX_ABSTRACT_CHARS = 4000
MAX_EVIDENCE_CHARS = 1500

_DESIGNS = (  # (label, pubType text it is read from) — most specific first
    ("systematic_review", ("systematic review", "systematic-review")),
    ("meta_analysis", ("meta-analysis",)),
    ("randomized_controlled_trial", ("randomized controlled trial",)),
    ("clinical_trial", ("clinical trial", "clinical trial, phase i", "clinical trial, phase ii",
                        "clinical trial, phase iii", "clinical trial, phase iv", "controlled clinical trial")),
    ("review", ("review", "review-article")),
    ("case_report", ("case reports", "case-report")),
    ("preprint", ("preprint",)),
    ("retraction_notice", ("retraction of publication",)),
    ("erratum", ("published erratum", "correction")),
)


def _designs(pub_types: list[str], source: str) -> list[str]:
    lowered = {p.lower() for p in pub_types}
    out = [label for label, names in _DESIGNS if any(n in lowered for n in names)]
    if any(p.startswith("clinical trial") for p in lowered) and "clinical_trial" not in out:
        out.append("clinical_trial")
    if source == "PPR" and "preprint" not in out:
        out.append("preprint")
    return out


def _corrections(record: dict[str, Any]) -> tuple[list[dict[str, str]], bool]:
    block = record.get("commentCorrectionList")
    if not isinstance(block, dict):
        return [], False
    items = block.get("commentCorrection")
    if isinstance(items, dict):
        items = [items]
    out = []
    for c in items if isinstance(items, list) else []:
        if isinstance(c, dict) and isinstance(c.get("type"), str):
            out.append({"type": c["type"][:80], "id": str(c.get("id") or "")[:40],
                        "source": str(c.get("source") or "")[:10], "reference": str(c.get("reference") or "")[:300]})
    return out[:50], True


def parse_record(r: Any) -> dict[str, Any] | None:
    """One Europe PMC `core` result as a labelled record, or None when it lacks an id or a title."""
    if not isinstance(r, dict):
        return None
    epmc_id, source = str(r.get("id") or "").strip(), str(r.get("source") or "").strip().upper()
    title = re.sub(r"\s+", " ", strip_html(str(r.get("title") or ""))).strip()
    if not epmc_id or not source or not title:
        return None
    pmid = str(r.get("pmid") or "").strip() or None
    pmcid = str(r.get("pmcid") or "").strip() or None
    doi = str(r.get("doi") or "").strip().lower() or None
    authors = [a.get("fullName", "").strip() for a in ((r.get("authorList") or {}).get("author") or [])
               if isinstance(a, dict) and isinstance(a.get("fullName"), str) and a.get("fullName", "").strip()]
    if not authors and isinstance(r.get("authorString"), str):
        authors = [a.strip() for a in r["authorString"].rstrip(".").split(",") if a.strip()]
    journal_info = r.get("journalInfo") if isinstance(r.get("journalInfo"), dict) else {}
    journal = ((journal_info.get("journal") or {}).get("title") if isinstance(journal_info.get("journal"), dict)
               else None) or ((r.get("bookOrReportDetails") or {}).get("publisher")
                              if isinstance(r.get("bookOrReportDetails"), dict) else None) or ""
    pub_list = r.get("pubTypeList") if isinstance(r.get("pubTypeList"), dict) else {}
    pub_types = text_list(pub_list.get("pubType"), cap=30, item_chars=80)
    corrections, has_corrections_field = _corrections(r)
    ctypes = [c["type"].lower() for c in corrections]
    lowered_types = {p.lower() for p in pub_types}
    retracted = "retracted publication" in lowered_types or any(t.startswith("retraction in") for t in ctypes)
    corrected = any(t.startswith(("erratum in", "correction in", "corrected and republished in")) for t in ctypes)
    concern = any(t.startswith("expression of concern in") for t in ctypes)
    abstract = re.sub(r"\s+", " ", strip_html(str(r.get("abstractText") or ""))).strip()[:MAX_ABSTRACT_CHARS]
    designs = _designs(pub_types, source)
    preprint = "preprint" in designs
    published = iso_date(r.get("firstPublicationDate")) or iso_date(r.get("electronicPublicationDate"))
    label = f"PMID {pmid}" if pmid else (f"DOI {doi}" if doi else f"{source} {epmc_id}")
    checked = (["pubTypeList"] if pub_list else []) + (["commentCorrectionList"] if has_corrections_field else [])
    return {
        "provider_id": f"{source}:{epmc_id}", "label": label,
        "pmid": pmid, "pmcid": pmcid, "doi": doi,
        "title": title[:500], "authors": authors[:50], "author_count": len(authors),
        "journal": str(journal).strip()[:300],
        "publication_date": published, "pub_year": str(r.get("pubYear") or "")[:4] or None,
        "publication_types": pub_types, "study_designs": designs,
        "abstract": abstract,
        "text_scope": "abstract_only" if abstract else "title_only",
        "full_text_fetched": False,
        "open_access": r.get("isOpenAccess") == "Y",
        "full_text_in_europepmc": r.get("inEPMC") == "Y",
        "license": str(r.get("license") or "")[:40] or None,
        "retracted": retracted, "corrected": corrected, "expression_of_concern": concern,
        "comment_corrections": corrections,
        "correction_signals_checked": checked,
        "source_class": SOURCE_CLASS_PREPRINT if preprint else SOURCE_CLASS_LITERATURE,
        "source_url": f"https://europepmc.org/article/{source}/{epmc_id}",
    }


def quality_prior(record: dict[str, Any]) -> float:
    """A prior only; the verifier decides. Study design raises it, a preprint or a correction lowers it."""
    designs = set(record["study_designs"])
    if record["source_class"] == SOURCE_CLASS_PREPRINT:
        q = 0.4
    elif designs & {"systematic_review", "meta_analysis"}:
        q = 0.8
    elif "randomized_controlled_trial" in designs:
        q = 0.75
    else:
        q = 0.65
    if record["corrected"]:
        q -= 0.1
    if record["expression_of_concern"]:
        q -= 0.2
    return round(max(0.05, q), 3)


def _evidence_text(record: dict[str, Any]) -> str:
    """The abstract (the title is kept on the source, not read as a claim); the title only when there is none."""
    text = record["abstract"] or f"{record['title'].rstrip('.')}."
    if len(text) > MAX_EVIDENCE_CHARS:
        cut = text.rfind(". ", 0, MAX_EVIDENCE_CHARS)
        text = text[: cut + 1 if cut > 200 else MAX_EVIDENCE_CHARS]
    return text


class EuropePMCAdapter(PublicRecordAdapter):
    id = "europepmc"
    license = "Europe PMC metadata (free API); per-article licences apply"
    description = ("Peer-reviewed literature and preprints from Europe PMC (abstracts only; retracted papers "
                   "excluded; free, keyless API).")
    endpoint = ENDPOINT

    def _default_client(self) -> PublicHTTPClient:
        return PublicHTTPClient(self.id, min_interval_s=0.2)

    def _exclude(self, record: dict[str, Any]) -> str | None:
        if record["retracted"]:
            return "retracted publication (flagged by Europe PMC); not used as evidence"
        if "retraction_notice" in record["study_designs"]:
            return "retraction notice, not a study; not used as evidence"
        return None

    def _retrieve(self, retrieval: Retrieval) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor = "*"
        while len(records) < self.max_records and retrieval.pages < self.max_pages:
            params = {"query": self.query, "format": "json", "resultType": "core",
                      "pageSize": self.page_size, "cursorMark": cursor}
            try:
                data = self.client.get_json(self.url(params))
            except ProviderUnavailable as exc:
                if retrieval.pages == 0:
                    raise
                retrieval.errors.append(f"page {retrieval.pages + 1}: {exc.reason}")
                break
            if not isinstance(data, dict) or not isinstance((data.get("resultList") or {}).get("result", []), list):
                retrieval.errors.append(f"page {retrieval.pages + 1}: response is not a Europe PMC result list")
                break
            retrieval.pages += 1
            if retrieval.hit_count is None and isinstance(data.get("hitCount"), int):
                retrieval.hit_count = data["hitCount"]
            page = data["resultList"].get("result") or []
            for raw in page:
                if len(records) >= self.max_records:
                    break
                rec = parse_record(raw)
                if rec is None:
                    retrieval.malformed += 1
                    continue
                if rec["provider_id"] in seen:
                    continue
                seen.add(rec["provider_id"])
                records.append(rec)
            nxt = data.get("nextCursorMark")
            if not page or not isinstance(nxt, str) or not nxt or nxt == cursor:
                break
            cursor = nxt
        if retrieval.malformed:
            retrieval.errors.append(f"{retrieval.malformed} malformed record(s) skipped")
        if retrieval.hit_count is not None and retrieval.hit_count > len(records) + retrieval.malformed:
            retrieval.truncated = True
        return records

    def _to_evidence(self, record: dict[str, Any], retrieval: Retrieval) -> tuple[Source, Evidence]:
        work = (f"doi:{record['doi']}" if record["doi"] else
                f"pmid:{record['pmid']}" if record["pmid"] else record["provider_id"])
        src = Source(kind=SourceKind.DOCUMENT, title=record["title"], uri=record["source_url"],
                     publisher=record["journal"] or "Europe PMC", published_at=record["publication_date"],
                     retrieved_at=retrieval.retrieved_at, license=record["license"] or self.license,
                     quality=quality_prior(record), independence_group=f"work:{work}")
        data = {k: record[k] for k in (
            "source_class", "provider_id", "pmid", "pmcid", "doi", "authors", "author_count", "journal",
            "publication_date", "pub_year", "publication_types", "study_designs", "text_scope",
            "full_text_fetched", "open_access", "full_text_in_europepmc", "retracted", "corrected",
            "expression_of_concern", "comment_corrections", "correction_signals_checked", "source_url")}
        data["retrieval"] = retrieval.as_data()
        how = ("abstract only (full text not fetched)" if record["text_scope"] == "abstract_only"
               else "title only (no abstract in Europe PMC)")
        ev = Evidence(source_id=src.id, kind=EvidenceKind.DOCUMENT, content=_evidence_text(record), data=data,
                      observed_at=record["publication_date"],
                      transformations=[f"retrieved from Europe PMC search '{retrieval.query}' at "
                                       f"{retrieval.retrieved_at} (outcome={retrieval.outcome})",
                                       f"{record['source_class']}: {record['label']}", how])
        if record["corrected"] or record["expression_of_concern"]:
            ev.transformations.append("flagged by Europe PMC: " + ", ".join(
                c["type"] for c in record["comment_corrections"]))
        return src, ev
