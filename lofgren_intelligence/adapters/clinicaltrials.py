"""ClinicalTrials.gov registry search (capability: "text"; source class: trial registry).

    GET https://clinicaltrials.gov/api/v2/studies
        ?query.term=<q>&format=json&pageSize=<n>&countTotal=true&fields=<list>[&pageToken=<next>]

Free, keyless API v2. Results are paged with ``nextPageToken``; pagination stops at the record or page
bound or when no token is returned. ``fields`` limits each study to the registry facts used here, which
keeps every page far below the public-fetch size limit.

Every record carries ``evidence_label = "registered_trial_not_efficacy_evidence"``: a registration says a
trial exists (status, phase, design, enrollment, dates), not that an intervention works. ``results_posted``
is True only when the registry sets ``hasResults``; this adapter never fetches the results themselves
(``results_fetched`` is always False). The sponsor's free-text summary is used only to match records to
questions, never as evidence, because it states aims, not findings.
"""

from __future__ import annotations

import re
from typing import Any

from ..evidence.types import Evidence, EvidenceKind, Source, SourceKind
from .public_api import (
    ProviderUnavailable,
    PublicHTTPClient,
    PublicRecordAdapter,
    Retrieval,
    text_list,
)

ENDPOINT = "https://clinicaltrials.gov/api/v2/studies"
SOURCE_CLASS_TRIAL_REGISTRY = "trial_registry"
EVIDENCE_LABEL = "registered_trial_not_efficacy_evidence"
FIELDS = ("NCTId,BriefTitle,OfficialTitle,OverallStatus,Phase,StudyType,Condition,InterventionName,"
          "InterventionType,EnrollmentCount,EnrollmentType,StartDate,PrimaryCompletionDate,CompletionDate,"
          "HasResults,LeadSponsorName,BriefSummary")
_NCT = re.compile(r"^NCT\d{8}$")
_PARTIAL_DATE = re.compile(r"^\d{4}(-\d{2}){0,2}$")


def _date(struct: Any) -> str | None:
    if isinstance(struct, dict) and isinstance(struct.get("date"), str) and _PARTIAL_DATE.match(struct["date"]):
        return struct["date"]
    return None


def _module(protocol: dict[str, Any], name: str) -> dict[str, Any]:
    value = protocol.get(name)
    return value if isinstance(value, dict) else {}


def parse_study(study: Any) -> dict[str, Any] | None:
    """One API v2 study as a labelled registry record, or None without a valid NCT id and a title."""
    if not isinstance(study, dict) or not isinstance(study.get("protocolSection"), dict):
        return None
    p = study["protocolSection"]
    ident, status = _module(p, "identificationModule"), _module(p, "statusModule")
    design, conditions = _module(p, "designModule"), _module(p, "conditionsModule")
    arms, sponsor = _module(p, "armsInterventionsModule"), _module(p, "sponsorCollaboratorsModule")
    nct = str(ident.get("nctId") or "").strip().upper()
    title = str(ident.get("briefTitle") or ident.get("officialTitle") or "").strip()
    if not _NCT.match(nct) or not title:
        return None
    interventions = []
    for i in arms.get("interventions") or []:
        if isinstance(i, dict) and isinstance(i.get("name"), str) and i["name"].strip():
            interventions.append({"type": str(i.get("type") or "")[:40], "name": i["name"].strip()[:200]})
    enrollment = design.get("enrollmentInfo") if isinstance(design.get("enrollmentInfo"), dict) else {}
    count = enrollment.get("count")
    lead = sponsor.get("leadSponsor") if isinstance(sponsor.get("leadSponsor"), dict) else {}
    has_results = study.get("hasResults") is True
    return {
        "nct_id": nct, "label": nct,
        "title": title[:500], "official_title": str(ident.get("officialTitle") or "")[:1000] or None,
        "overall_status": str(status.get("overallStatus") or "")[:40] or None,
        "phases": text_list(design.get("phases"), cap=5, item_chars=20),
        "study_type": str(design.get("studyType") or "")[:40] or None,
        "conditions": text_list(conditions.get("conditions"), cap=30, item_chars=200),
        "interventions": interventions[:30],
        "enrollment": count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None,
        "enrollment_type": str(enrollment.get("type") or "")[:20] or None,
        "start_date": _date(status.get("startDateStruct")),
        "primary_completion_date": _date(status.get("primaryCompletionDateStruct")),
        "completion_date": _date(status.get("completionDateStruct")),
        "lead_sponsor": str(lead.get("name") or "")[:200] or None,
        "results_posted": has_results,
        "results_fetched": False,
        "evidence_label": EVIDENCE_LABEL,
        "results_label": "results_posted_not_fetched" if has_results else "no_results_posted",
        "source_class": SOURCE_CLASS_TRIAL_REGISTRY,
        "source_url": f"https://clinicaltrials.gov/study/{nct}",
        # matching text only; never evidence content
        "summary_for_matching": str(_module(p, "descriptionModule").get("briefSummary") or "")[:3000],
    }


def registry_text(r: dict[str, Any]) -> str:
    """The registry facts as evidence text: attributions to the registry ("ClinicalTrials.gov reports ..."),
    so V1 treats each as what the registry says about one trial, never as a finding that anything works."""
    who = f"ClinicalTrials.gov reports that trial {r['nct_id']}"
    parts = [f"{who} is registered as {r['title'].rstrip('.')}."]
    if r["overall_status"]:
        parts.append(f"{who} has overall status {r['overall_status']}.")
    if r["phases"]:
        parts.append(f"{who} is registered as phase {', '.join(r['phases'])}.")
    if r["conditions"]:
        parts.append(f"{who} studies the conditions {', '.join(r['conditions'][:10])}.")
    if r["interventions"]:
        parts.append(f"{who} tests the interventions {', '.join(i['name'] for i in r['interventions'][:10])}.")
    if r["enrollment"] is not None:
        parts.append(f"{who} has {(r['enrollment_type'] or 'unspecified').lower()} enrollment of "
                     f"{r['enrollment']} participants.")
    if r["start_date"] or r["completion_date"]:
        parts.append(f"{who} has start date {r['start_date'] or 'not given'} and completion date "
                     f"{r['completion_date'] or 'not given'}.")
    parts.append(f"{who} has results posted (not retrieved here)." if r["results_posted"]
                 else f"{who} has no results posted.")
    return " ".join(parts)


class ClinicalTrialsAdapter(PublicRecordAdapter):
    id = "clinicaltrials"
    license = "ClinicalTrials.gov public registry data (U.S. National Library of Medicine terms)"
    description = ("Registered clinical trials from ClinicalTrials.gov API v2 (registry facts only: a "
                   "registration is not evidence of efficacy; free, keyless API).")
    endpoint = ENDPOINT
    default_page_size = 20
    page_size_ceiling = 50

    def _default_client(self) -> PublicHTTPClient:
        # The registry asks for about 50 requests a minute at most per client.
        return PublicHTTPClient(self.id, min_interval_s=1.2)

    @staticmethod
    def _search_text(record: dict[str, Any]) -> str:
        return " ".join([record["title"], record["official_title"] or "", " ".join(record["conditions"]),
                         " ".join(i["name"] for i in record["interventions"]), record["summary_for_matching"]])

    def _retrieve(self, retrieval: Retrieval) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        token: str | None = None
        while len(records) < self.max_records and retrieval.pages < self.max_pages:
            params: dict[str, Any] = {"query.term": self.query, "format": "json", "pageSize": self.page_size,
                                      "fields": FIELDS}
            if token:
                params["pageToken"] = token
            else:
                params["countTotal"] = "true"
            try:
                data = self.client.get_json(self.url(params))
            except ProviderUnavailable as exc:
                if retrieval.pages == 0:
                    raise
                retrieval.errors.append(f"page {retrieval.pages + 1}: {exc.reason}")
                break
            if not isinstance(data, dict) or not isinstance(data.get("studies", []), list):
                retrieval.errors.append(f"page {retrieval.pages + 1}: response is not a ClinicalTrials.gov "
                                        "study list")
                break
            retrieval.pages += 1
            if retrieval.hit_count is None and isinstance(data.get("totalCount"), int):
                retrieval.hit_count = data["totalCount"]
            page = data.get("studies") or []
            for raw in page:
                if len(records) >= self.max_records:
                    break
                rec = parse_study(raw)
                if rec is None:
                    retrieval.malformed += 1
                    continue
                if rec["nct_id"] in seen:
                    continue
                seen.add(rec["nct_id"])
                records.append(rec)
            nxt = data.get("nextPageToken")
            if not page or not isinstance(nxt, str) or not nxt or nxt == token:
                if retrieval.hit_count is None and isinstance(nxt, str) and nxt:
                    retrieval.truncated = True
                break
            token = nxt
        else:
            if token and retrieval.hit_count is None:
                retrieval.truncated = True
        if retrieval.malformed:
            retrieval.errors.append(f"{retrieval.malformed} malformed record(s) skipped")
        if retrieval.hit_count is not None and retrieval.hit_count > len(records) + retrieval.malformed:
            retrieval.truncated = True
        return records

    def _to_evidence(self, record: dict[str, Any], retrieval: Retrieval) -> tuple[Source, Evidence]:
        src = Source(kind=SourceKind.DATASET, title=f"{record['nct_id']}: {record['title']}",
                     uri=record["source_url"], publisher="ClinicalTrials.gov", published_at=None,
                     retrieved_at=retrieval.retrieved_at, license=self.license, quality=0.6,
                     independence_group=f"trial:{record['nct_id']}")
        data = {k: v for k, v in record.items() if k not in ("label", "summary_for_matching")}
        data["claim_subject"] = f"trial:{record['nct_id']}"
        data["retrieval"] = retrieval.as_data()
        ev = Evidence(source_id=src.id, kind=EvidenceKind.DOCUMENT, content=registry_text(record), data=data,
                      observed_at=None,
                      transformations=[f"retrieved from ClinicalTrials.gov search '{retrieval.query}' at "
                                       f"{retrieval.retrieved_at} (outcome={retrieval.outcome})",
                                       "trial_registry: registered trial is not evidence of efficacy",
                                       "results posted, not retrieved" if record["results_posted"]
                                       else "no results posted"])
        return src, ev
