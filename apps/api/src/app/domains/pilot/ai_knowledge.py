"""ChatGPT-style university knowledge collection via OpenAI.

Asks the model what it knows about each registered institution's responsibilities
and stages every answer as ``NEEDS_REVIEW`` in ``pilot_collected_fact`` for a human
to compare against the official URL. No crawler, no page fetch, no local artifacts.

Designed to run on the deployed API / worker, not on a developer laptop.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from app.core.config import DatabaseRole, Settings, get_settings
from app.db.enums import (
    FieldStatus,
    PilotFactValidationState,
    PilotImportStatus,
    PilotSubmissionKind,
    SourceCategory,
)
from app.domains.pilot.models import PILOT_FACT_TYPES

SHEET_NAME = "ai_knowledge"
COST_GATE = 10
CONSECUTIVE_FAILURE_LIMIT = 3
EXTRACTOR_NAME = "openai_knowledge"
EXTRACTOR_VERSION = "1"

#: Responsibility → fact type(s) we ask the model for.
ASK_FOR: dict[str, list[tuple[str, str, str]]] = {
    SourceCategory.LANGUAGE_REQUIREMENTS.value: [
        ("LANGUAGE_REQUIREMENT", "language.test", "English language test name (IELTS/TOEFL/etc)"),
        ("LANGUAGE_REQUIREMENT", "language.overall_score", "Overall minimum score"),
        ("LANGUAGE_REQUIREMENT", "language.component_score", "Component/minimum band scores if stated"),
    ],
    SourceCategory.TUITION_FEES.value: [
        ("TUITION", "tuition.amount", "International student tuition / fees (amount + currency + period)"),
    ],
    SourceCategory.APPLICATION_DEADLINES.value: [
        ("DEADLINE", "deadline.application", "Application deadline(s) for international applicants"),
    ],
    SourceCategory.ENTRY_REQUIREMENTS.value: [
        ("ADMISSION_REQUIREMENT", "admission.requirement", "Key entry / admission requirements"),
    ],
    SourceCategory.UNDERGRADUATE_ADMISSIONS.value: [
        ("ADMISSION_REQUIREMENT", "admission.requirement", "Undergraduate admission requirements summary"),
        ("DEADLINE", "deadline.application", "Undergraduate application deadline if commonly published"),
    ],
    SourceCategory.POSTGRADUATE_ADMISSIONS.value: [
        ("ADMISSION_REQUIREMENT", "admission.requirement", "Postgraduate admission requirements summary"),
        ("DEADLINE", "deadline.application", "Postgraduate application deadline if commonly published"),
    ],
    SourceCategory.PROGRAM_CATALOG.value: [
        ("PROGRAM", "program.name", "Notable programme names or catalogue summary"),
    ],
    SourceCategory.ACADEMIC_CALENDAR.value: [
        ("DEADLINE", "deadline.application", "Key academic calendar dates relevant to applicants"),
    ],
}

_LATEST_SOURCE_LIST = text(
    "SELECT id FROM pilot_submission "
    " WHERE submission_kind = 'OFFICIAL_SOURCE_LIST' "
    " ORDER BY imported_at DESC, id LIMIT 1"
)


def assert_ask_for_is_valid() -> None:
    """Raise if ASK_FOR names a fact_type the DB CHECK would reject."""
    allowed = set(PILOT_FACT_TYPES)
    for asks in ASK_FOR.values():
        for fact_type, _field, _q in asks:
            if fact_type not in allowed:
                raise ValueError(f"ASK_FOR fact_type {fact_type!r} is not in PILOT_FACT_TYPES")


def engine_for(role: DatabaseRole = DatabaseRole.API) -> Engine:
    return create_engine(
        get_settings().database.sync_dsn(role),
        future=True,
        connect_args={"connect_timeout": 20},
    )


def list_institutions(connection: Connection, limit: int | None) -> list[dict[str, Any]]:
    """One group per institution with its physical workbook URLs."""
    source_list = connection.execute(_LATEST_SOURCE_LIST).scalar_one_or_none()
    if source_list is None:
        return []
    rows = connection.execute(
        text(
            "SELECT ti.id AS institution_id, ti.match_key AS institution_name, "
            "       pcs.source_ref, pcs.source_type::text AS source_type, "
            "       pcs.degree_scope, pcs.workbook_column, pcs.official_url, "
            "       pcs.normalized_url, pcs.url_sha256, pcs.host, pcs.is_third_party, "
            "       pcs.sheet_row_no "
            "  FROM pilot_collected_source pcs "
            "  JOIN target_institution ti ON ti.id = pcs.target_institution_id "
            " WHERE pcs.submission_id = :submission "
            "   AND pcs.duplicate_of_source_ref IS NULL "
            " ORDER BY ti.match_key, pcs.source_ref"
        ),
        {"submission": source_list},
    ).all()
    grouped: dict[Any, dict[str, Any]] = {}
    for row in rows:
        key = row.institution_id
        if key not in grouped:
            grouped[key] = {
                "institution_id": row.institution_id,
                "institution_name": row.institution_name,
                "sources": [],
            }
        grouped[key]["sources"].append(dict(row._mapping))
    out = list(grouped.values())
    if limit is not None:
        out = out[:limit]
    return out


def open_submission(
    connection: Connection, *, model: str, institutions: list[dict[str, Any]]
) -> uuid.UUID:
    sources = [s for inst in institutions for s in inst["sources"]]
    submission_id = uuid.uuid4()
    stamp = datetime.now(UTC).isoformat(timespec="microseconds")
    digest = hashlib.sha256(
        "|".join([SHEET_NAME, model, stamp, *(s["source_ref"] for s in sources)]).encode()
    ).hexdigest()
    connection.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename, "
            "  file_byte_size, template_version, submission_kind, defines_pilot_scope, "
            "  imported_at, selected_university_count, import_status, notes) "
            "VALUES (:id, :sha, :name, 1, :template, :kind, false, now(), :count, "
            "  :status, :notes)"
        ),
        {
            "id": submission_id,
            "sha": digest,
            "name": f"{SHEET_NAME}-{stamp}",
            "template": f"{EXTRACTOR_NAME}-{EXTRACTOR_VERSION}",
            "kind": PilotSubmissionKind.COLLECTION_WORKBOOK.value,
            "count": len(institutions),
            "status": PilotImportStatus.VALIDATED.value,
            "notes": (
                f"Collected by {SHEET_NAME} using {model} over {len(institutions)} "
                "institution(s). Values are model knowledge for human review against "
                "the named official URLs — not page fetches, not snapshots."
            ),
        },
    )
    for source in sources:
        connection.execute(
            text(
                "INSERT INTO pilot_collected_source (submission_id, source_ref, "
                "  target_institution_id, sheet_row_no, source_type, degree_scope, "
                "  workbook_column, official_url, normalized_url, url_sha256, host, "
                "  is_third_party, collector_notes) "
                "VALUES (:submission, :ref, :institution, :row, :type, :scope, "
                "  :column, :url, :normalized, :sha, :host, :third_party, :notes)"
            ),
            {
                "submission": submission_id,
                "ref": source["source_ref"],
                "institution": source["institution_id"],
                "row": source["sheet_row_no"],
                "type": source["source_type"],
                "scope": source["degree_scope"],
                "column": source["workbook_column"],
                "url": source["official_url"],
                "normalized": source["normalized_url"],
                "sha": source["url_sha256"],
                "host": source["host"],
                "third_party": source["is_third_party"],
                "notes": f"Cited by {SHEET_NAME} for reviewer verification; not fetched.",
            },
        )
    return submission_id


def ask_institution(
    *,
    institution_name: str,
    sources: list[dict[str, Any]],
    model: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    """One ChatGPT-style call for one university."""
    from openai import OpenAI

    asks: list[dict[str, str]] = []
    for source in sources:
        for fact_type, field_path, question in ASK_FOR.get(source["source_type"], []):
            asks.append(
                {
                    "source_ref": source["source_ref"],
                    "source_type": source["source_type"],
                    "official_url": source["official_url"],
                    "fact_type": fact_type,
                    "field_path": field_path,
                    "question": question,
                }
            )
    if not asks:
        return []

    client = OpenAI(
        api_key=settings.openai.api_key.get_secret_value(),
        base_url=settings.openai.base_url,
        timeout=settings.openai.request_timeout_seconds,
    )
    catalog = json.dumps(asks, ensure_ascii=False, indent=2)
    system = (
        "You collect university admissions facts for a human reviewer. "
        "Answer from your knowledge the way ChatGPT would, for the named institution. "
        "Return JSON only: {\"facts\":[...]} where each fact has "
        "source_ref, fact_type, field_path, value_text, evidence_text, confidence "
        "(HIGH|MEDIUM|LOW), and optional not_published (boolean). "
        "value_text is the short answer; evidence_text is a fuller sentence. "
        "If you are not confident a precise figure/date is current, omit that fact "
        "or set not_published true with a short reason in evidence_text. "
        "Only use source_ref / fact_type / field_path values from the supplied catalog. "
        "Do not invent source_ref values."
    )
    user = (
        f"Institution: {institution_name}\n\n"
        f"Catalog of questions (answer only these):\n{catalog}\n"
    )
    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=settings.openai.max_output_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = response.choices[0].message.content or "{}"
    payload = json.loads(content)
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return []
    allowed = {(a["source_ref"], a["fact_type"], a["field_path"]) for a in asks}
    urls = {s["source_ref"]: s["official_url"] for s in sources}
    out: list[dict[str, Any]] = []
    for item in facts:
        if not isinstance(item, dict):
            continue
        source_ref = str(item.get("source_ref") or "")
        fact_type = str(item.get("fact_type") or "")
        field_path = str(item.get("field_path") or "")
        if (source_ref, fact_type, field_path) not in allowed:
            continue
        value_text = str(item.get("value_text") or "").strip()
        evidence = str(item.get("evidence_text") or value_text).strip()
        not_published = bool(item.get("not_published"))
        if not not_published and not value_text:
            continue
        conf = str(item.get("confidence") or "MEDIUM").upper()
        if conf not in {"HIGH", "MEDIUM", "LOW"}:
            conf = "MEDIUM"
        out.append(
            {
                "source_ref": source_ref,
                "fact_type": fact_type,
                "field_path": field_path,
                "value_text": value_text or "(not published / unknown)",
                "evidence_text": evidence or value_text,
                "not_published": not_published,
                "confidence": conf,
                "official_url": urls[source_ref],
                "institution_id": sources[0]["institution_id"],
            }
        )
    return out


def insert_fact(
    connection: Connection,
    *,
    submission_id: uuid.UUID,
    row_no: int,
    institution_id: Any,
    fact: dict[str, Any],
    model: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO pilot_collected_fact (submission_id, sheet_name, sheet_row_no, "
            "  target_institution_id, source_ref, fact_type, field_path, field_status, "
            "  collected_values, official_text, source_url, collector_notes, "
            "  validation_state) "
            "VALUES (:submission, :sheet, :row, :institution, :ref, :fact_type, "
            "  :field_path, :status, CAST(:collected AS jsonb), :official_text, :url, "
            "  :notes, :state)"
        ),
        {
            "submission": submission_id,
            "sheet": SHEET_NAME,
            "row": row_no,
            "institution": institution_id,
            "ref": fact["source_ref"],
            "fact_type": fact["fact_type"],
            "field_path": fact["field_path"],
            "status": (
                FieldStatus.OFFICIALLY_NOT_PUBLISHED.value
                if fact["not_published"]
                else FieldStatus.PUBLISHED.value
            ),
            "collected": json.dumps(
                {
                    "mode": SHEET_NAME,
                    "confidence": fact["confidence"],
                    "value_text": fact["value_text"],
                    "model": model,
                    "verify_against": fact["official_url"],
                }
            ),
            "official_text": fact["evidence_text"][:4000],
            "url": fact["official_url"],
            "notes": (
                f"{SHEET_NAME} / {model} / {EXTRACTOR_NAME} {EXTRACTOR_VERSION}. "
                "MODEL KNOWLEDGE — not fetched from the page. "
                f"Reviewer must verify against {fact['official_url']} before accepting."
            ),
            "state": PilotFactValidationState.NEEDS_REVIEW.value,
        },
    )


def preflight(engine: Engine) -> dict[str, Any]:
    settings = get_settings()
    with engine.connect() as connection:
        institutions = list_institutions(connection, None)
        staged = connection.execute(
            text("SELECT count(*) FROM pilot_collected_fact WHERE sheet_name = :s"),
            {"s": SHEET_NAME},
        ).scalar_one()
        runs = connection.execute(
            text("SELECT count(*) FROM pilot_submission WHERE template_version LIKE :t"),
            {"t": f"{EXTRACTOR_NAME}-%"},
        ).scalar_one()
    key = settings.openai.api_key.get_secret_value()
    return {
        "institutions": len(institutions),
        "workbook_urls": sum(len(i["sources"]) for i in institutions),
        "previous_runs": int(runs),
        "facts_staged": int(staged),
        "model": settings.openai.model,
        "api_key_set": bool(key and key != "change-me"),
        "sheet_name": SHEET_NAME,
    }


def report(engine: Engine, *, limit: int = 20) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT ps.id, ps.imported_at, ps.selected_university_count, "
                "       count(f.id) AS facts "
                "  FROM pilot_submission ps "
                "  LEFT JOIN pilot_collected_fact f ON f.submission_id = ps.id "
                " WHERE ps.template_version LIKE :t "
                " GROUP BY ps.id "
                " ORDER BY ps.imported_at DESC LIMIT :limit"
            ),
            {"t": f"{EXTRACTOR_NAME}-%", "limit": limit},
        ).all()
    return [
        {
            "id": str(row.id),
            "imported_at": row.imported_at.isoformat() if row.imported_at else None,
            "universities": int(row.selected_university_count or 0),
            "facts": int(row.facts or 0),
        }
        for row in rows
    ]


def run_collection(
    engine: Engine,
    *,
    limit: int | None = None,
    acknowledged: bool = False,
    progress: Any | None = None,
) -> dict[str, Any]:
    """Query OpenAI per institution; stage NEEDS_REVIEW facts. Returns a summary."""
    assert_ask_for_is_valid()
    settings = get_settings()
    key = settings.openai.api_key.get_secret_value()
    if not key or key == "change-me":
        raise RuntimeError("OPENAI_API_KEY is not set")

    with engine.connect() as connection:
        institutions = list_institutions(connection, limit)
    if not institutions:
        return {
            "status": "empty",
            "institutions": 0,
            "facts_staged": 0,
            "failures": 0,
            "submission_id": None,
            "message": "no institutions registered; import the official-source list first",
        }
    if len(institutions) > COST_GATE and not acknowledged:
        raise RuntimeError(
            f"{len(institutions)} institutions exceeds cost gate {COST_GATE}; "
            "pass acknowledged=True or a smaller limit"
        )

    model = settings.openai.model
    with engine.begin() as connection:
        submission_id = open_submission(connection, model=model, institutions=institutions)

    row_no = 1
    staged = failed = 0
    consecutive_failures = 0
    details: list[dict[str, Any]] = []

    for inst in institutions:
        name = inst["institution_name"]
        try:
            facts = ask_institution(
                institution_name=name,
                sources=inst["sources"],
                model=model,
                settings=settings,
            )
        except Exception as exc:
            failed += 1
            consecutive_failures += 1
            details.append({"institution": name, "facts": 0, "error": f"{type(exc).__name__}: {exc}"})
            if progress is not None:
                progress(name, 0, str(exc))
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                break
            continue
        consecutive_failures = 0
        kept = 0
        with engine.begin() as connection:
            for fact in facts:
                row_no += 1
                insert_fact(
                    connection,
                    submission_id=submission_id,
                    row_no=row_no,
                    institution_id=inst["institution_id"],
                    fact=fact,
                    model=model,
                )
                kept += 1
                staged += 1
        details.append({"institution": name, "facts": kept, "error": None})
        if progress is not None:
            progress(name, kept, None)

    return {
        "status": "ok" if staged or not failed else "failed",
        "submission_id": str(submission_id),
        "institutions": len(institutions),
        "facts_staged": staged,
        "failures": failed,
        "model": model,
        "sheet_name": SHEET_NAME,
        "details": details,
        "message": (
            "All rows are NEEDS_REVIEW. Compare each against its official_url, "
            "then approve or reject."
        ),
    }


__all__ = [
    "ASK_FOR",
    "COST_GATE",
    "EXTRACTOR_NAME",
    "EXTRACTOR_VERSION",
    "SHEET_NAME",
    "assert_ask_for_is_valid",
    "ask_institution",
    "engine_for",
    "insert_fact",
    "list_institutions",
    "open_submission",
    "preflight",
    "report",
    "run_collection",
]
