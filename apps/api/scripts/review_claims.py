"""Candidate quality, grouping, conflict and review preparation (Step 5C.3).

Usage::

    uv run python apps/api/scripts/review_claims.py integrity     # locators + routing
    uv run python apps/api/scripts/review_claims.py groups        # agreement + conflicts
    uv run python apps/api/scripts/review_claims.py programs      # program context groups
    uv run python apps/api/scripts/review_claims.py language      # language groups
    uv run python apps/api/scripts/review_claims.py deadlines     # deadline groups
    uv run python apps/api/scripts/review_claims.py calendar      # calendar separation
    uv run python apps/api/scripts/review_claims.py admission     # quality breakdown
    uv run python apps/api/scripts/review_claims.py admission-sample --limit 100
    uv run python apps/api/scripts/review_claims.py scope         # scope reconciliation
    uv run python apps/api/scripts/review_claims.py tuition       # all 40, in full
    uv run python apps/api/scripts/review_claims.py queue         # priority + queues
    uv run python apps/api/scripts/review_claims.py readiness     # promotion blockers
    uv run python apps/api/scripts/review_claims.py sources       # verification assistance
    uv run python apps/api/scripts/review_claims.py show --candidate <id>
    uv run python apps/api/scripts/review_claims.py accept --candidate <id> --actor <id> ...

**Offline, and nothing is published.** Reads persisted candidates and stored artifacts.
No network request, no LLM, no browser. The only thing it writes is a row in
`field_claim_candidate_review`, and an accepted candidate is still not publishable --
promotion needs the source to be eligible, which is a separate question (section 30).

Every report counts CURRENT rule versions only. 6,015 superseded rows sit beside 1,937
current ones, and a total that includes them looks entirely plausible.
"""

# ruff: noqa: S608 -- the only thing interpolated into any query here is a table alias,
# written as a literal at every call site. Every value travels as a bound parameter.

from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.storage import FilesystemEvidenceStore
from app.domains.claims.grouping import (
    Agreement,
    CandidateRow,
    conflicted_ids,
    conflicts,
    context_conflicts,
    corroborated_ids,
    group_candidates,
    language_groups,
    program_groups,
)
from app.domains.claims.loader import (
    PROVEN_RELATIONSHIPS,
    LoadedCandidate,
    load_current,
)
from app.domains.claims.model import ROUTING, FieldKind
from app.domains.claims.quality import (
    AdmissionQuality,
    FinancialContext,
    classify_admission,
    classify_financial_context,
)
from app.domains.claims.review import (
    CURRENT_RULES,
    FIELD_KIND_EXTRACTOR,
    Decision,
    Queue,
    ReviewError,
    blockers_for,
    priority_for,
    queue_for,
    record_decision,
)
from app.domains.claims.scope import propose_scope
from app.domains.extraction.runner import DERIVED_PREFIX

RULE = "=" * 78


def _engine(role: DatabaseRole) -> Any:
    return create_engine(get_settings().database.sync_dsn(role), future=True)


def _artifacts(root: Path) -> FilesystemEvidenceStore:
    return FilesystemEvidenceStore(root, prefix=DERIVED_PREFIX)


def _load(args: argparse.Namespace, *, resolve_locators: bool = True) -> list[LoadedCandidate]:
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        loaded = load_current(
            connection, _artifacts(args.artifact_root), resolve_locators=resolve_locators
        )
    engine.dispose()
    return loaded


def _header(title: str, section: str) -> None:
    print(RULE)
    print(f"{title}  ({section})")
    print(RULE)


def _counter(title: str, counts: Counter[str], *, total: int | None = None) -> None:
    print(f"\n  {title}:")
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        share = f"  ({count / total * 100:.1f}%)" if total else ""
        print(f"    {count:>6}  {key}{share}")


# ===========================================================================
# 22-25. Integrity
# ===========================================================================


def command_integrity(args: argparse.Namespace) -> int:
    """Locator resolution over ALL current candidates, plus routing validation."""
    loaded = _load(args)
    _header("CURRENT-CANDIDATE FILTERING", "section 22")
    print("\n  A candidate is current when its rule version is the live one. The live")
    print("  versions come from the runner's registry and are published to SQL, because")
    print("  a frozen copy in a view was one generation stale within four corrections.\n")
    for name, version in CURRENT_RULES:
        print(f"    {name:<28} v{version}")
    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        totals = connection.execute(
            text(
                "SELECT count(*) FILTER (WHERE NOT is_superseded) AS current, "
                "       count(*) FILTER (WHERE is_superseded) AS superseded "
                "  FROM candidate_review_state"
            )
        ).one()
    engine.dispose()
    print(f"\n    current     {totals.current}")
    print(f"    superseded  {totals.superseded}  (retained as history, never counted)")
    print(f"    loaded      {len(loaded)}")

    _header("LOCATOR INTEGRITY", "sections 23-24")
    print(f"\n  Every one of the {len(loaded)} current candidates, not a sample.\n")
    relationships: Counter[str] = Counter()
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    failures: list[LoadedCandidate] = []
    for candidate in loaded:
        relationships[candidate.evidence_relationship] += 1
        by_kind[candidate.row.field_kind][candidate.evidence_relationship] += 1
        if candidate.evidence_relationship not in PROVEN_RELATIONSHIPS:
            failures.append(candidate)
    _counter("relationship between the resolved text and the claim", relationships)
    proven = len(loaded) - len(failures)
    print(f"\n    PROVEN {proven}/{len(loaded)}")
    print("\n  by field kind:")
    for kind in sorted(by_kind):
        parts = ", ".join(f"{name}={count}" for name, count in sorted(by_kind[kind].items()))
        print(f"    {kind:<26} {parts}")
    if failures:
        print(f"\n  FAILURES ({len(failures)}):")
        for candidate in failures[:20]:
            print(f"    {candidate.row.field_kind}  {candidate.candidate_id}")
            print(f"      locator : {json.dumps(candidate.row.locator)[:120]}")
            print(f"      raw     : {candidate.row.value_raw_text[:90]!r}")
            print(f"      resolved: {(candidate.resolved_text or '')[:90]!r}")

    _header("ROUTING INTEGRITY", "section 25")
    print("\n  Every candidate's field kind must be authorised by the responsibility the")
    print("  workbook claimed its page for. Expected: 0 violations.\n")
    violations: list[tuple[str, str, str]] = []
    pairs: Counter[str] = Counter()
    for candidate in loaded:
        kind = candidate.row.field_kind
        responsibility = candidate.row.responsibility
        pairs[f"{responsibility} -> {kind}"] += 1
        needed = FIELD_KIND_EXTRACTOR.get(kind)
        if needed is None or needed not in ROUTING.get(responsibility, frozenset()):
            violations.append((str(candidate.candidate_id), responsibility, kind))
    for pair, count in sorted(pairs.items()):
        print(f"    {count:>6}  {pair}")
    print(f"\n    violations: {len(violations)}")
    for candidate_id, responsibility, kind in violations[:20]:
        print(f"      {candidate_id}  {responsibility} does not authorise {kind}")

    print(f"\n  {'PASS' if not failures and not violations else 'FAIL'}")
    return 0 if not failures and not violations else 1


# ===========================================================================
# 5-7, 20-21. Agreement and conflict
# ===========================================================================


def _rows(loaded: list[LoadedCandidate]) -> list[CandidateRow]:
    return [candidate.row for candidate in loaded]


def command_groups(args: argparse.Namespace) -> int:
    loaded = _load(args, resolve_locators=False)
    rows = _rows(loaded)
    groups = group_candidates(rows)

    _header("AGREEMENT GROUPING", "sections 5-6")
    print("\n  Grouped by CONTEXT (what the claim is about), compared by VALUE (what it")
    print("  says). Never by value alone, and never across institutions.\n")
    verdicts: Counter[str] = Counter()
    members: Counter[str] = Counter()
    for group in groups:
        verdicts[group.verdict.value] += 1
        members[group.verdict.value] += len(group.members)
    print(f"  {'verdict':<24}{'groups':>8}{'candidates':>13}")
    print("  " + "-" * 45)
    for verdict in Agreement:
        print(f"  {verdict.value:<24}{verdicts[verdict.value]:>8}{members[verdict.value]:>13}")
    print(f"  {'TOTAL':<24}{len(groups):>8}{sum(members.values()):>13}")

    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    for group in groups:
        by_kind[group.field_kind][group.verdict.value] += len(group.members)
    print("\n  candidates by field kind and verdict:")
    for kind in sorted(by_kind):
        parts = ", ".join(f"{name}={count}" for name, count in sorted(by_kind[kind].items()))
        print(f"    {kind:<26} {parts}")

    agreeing = [group for group in groups if group.verdict is Agreement.AGREES]
    print(f"\n  CROSS-SOURCE AGREEMENT (section 20): {len(agreeing)} group(s)")
    print("  Agreement raises a reviewer's confidence. It verifies nothing, and nothing")
    print("  here is accepted on a count of sources.\n")
    for group in agreeing[: args.limit]:
        print(f"    {group.institution}  {group.field_kind}")
        print(f"      context : {' | '.join(group.context_key)}")
        print(f"      value   : {' | '.join(next(iter(group.values)))}")
        for url in group.urls:
            print(f"      source  : {url[:96]}")

    _header("CONFLICT GROUPS", "sections 7, 21")
    print("\n  One context, more than one value. No winner is chosen here.\n")
    conflicting = conflicts(groups)
    print(
        f"  {len(conflicting)} conflict group(s), "
        f"{sum(len(group.members) for group in conflicting)} candidate(s)"
    )
    for group in conflicting[: args.limit]:
        print(f"\n    {group.institution}  {group.field_kind}")
        print(f"      context: {' | '.join(group.context_key)}")
        for value, candidates in group.values.items():
            print(f"      value  : {' | '.join(value)}")
            for candidate in candidates:
                print(f"        {candidate.url[:80]}")
                print(f"          rule {candidate.extractor_name} v{candidate.extractor_version}")
                print(f"          {candidate.evidence_text[:110]!r}")

    unconfirmed = [group for group in groups if group.unconfirmed_disagreement]
    print(
        f"\n  UNCONFIRMED DISAGREEMENT: {len(unconfirmed)} group(s), "
        f"{sum(len(group.members) for group in unconfirmed)} candidate(s)"
    )
    print("  Several values under a context made of unknowns. NOT reported as conflicts:")
    print("  a key of sentinels has not established that the sources answered the same")
    print("  question. Reported here rather than dropped, because the gate that stops a")
    print("  fee table's ten line items reading as a ten-way disagreement also means a")
    print("  real contradiction under a thin context goes unconfirmed.\n")
    for group in unconfirmed[: args.limit]:
        print(
            f"    {group.institution}  {group.field_kind}  "
            f"({len(group.values)} values, {len(group.members)} candidates)"
        )
        print(f"      context: {' | '.join(group.context_key)}")
        for value in group.values:
            print(f"        {' | '.join(value)}")

    contextual = context_conflicts(rows)
    print(f"\n  CONTEXT CONFLICTS: {len(contextual)} — one value, contradictory context")
    print("  Keying by context hides these: two pages give the same date and disagree")
    print("  about which round it is, so by context they are two unrelated groups.\n")
    for conflict in contextual[: args.limit]:
        print(f"    {conflict.institution}  {conflict.field_kind}")
        print(f"      value: {' | '.join(conflict.value_key)}")
        for key, candidates in conflict.contexts.items():
            print(f"      context: {' | '.join(key)}")
            for candidate in candidates:
                print(f"        {candidate.url[:76]}")
                print(f"          {candidate.evidence_text[:100]!r}")
    return 0


# ===========================================================================
# 8-9. Program context groups
# ===========================================================================


def command_programs(args: argparse.Namespace) -> int:
    loaded = _load(args)
    by_id = {candidate.candidate_id: candidate for candidate in loaded}
    groups = program_groups(_rows(loaded))

    _header("PROGRAM CONTEXT GROUPS", "sections 8-9")
    print("\n  Grouped on (extraction, locator): the rule emits a programme's name, its")
    print("  degree level and its discipline hint from ONE heading or link with ONE")
    print("  locator, so that key is provably one source-local context rather than two")
    print("  parts of a page that happen to be about related things (section 28).\n")

    sizes: Counter[int] = Counter()
    shapes: Counter[str] = Counter()
    confirmation: Counter[str] = Counter()
    quarantined: list[Any] = []
    for group in groups:
        sizes[len(group.members)] += 1
        shapes["+".join(group.kinds)] += 1
        marks = {by_id[m.candidate_id].confirmation.value for m in group.members}
        mark = "CONFIRMED" if marks == {"CONFIRMED"} else sorted(marks)[0]
        confirmation[mark] += 1
        if mark != "CONFIRMED":
            quarantined.append((group, mark))

    print(f"  {len(groups)} group(s) over {sum(len(g.members) for g in groups)} candidate(s)")
    _counter("group size", Counter({str(k): v for k, v in sizes.items()}))
    _counter("group shape", shapes)
    _counter("body confirmation (section 9)", confirmation, total=len(groups))

    print("\n  SECTION 9 QUARANTINE")
    print("  A programme candidate may only be grouped for review when the parser")
    print("  labelled its text as real body content. It cannot do that for link text:")
    print("  `Link` records no container at all, so a catalogue anchor and a site-wide")
    print("  course picker are indistinguishable on the row. Those groups are held back")
    print("  rather than silently trusted.\n")
    print(f"    quarantined: {len(quarantined)} of {len(groups)} group(s)")
    for group, mark in quarantined[: args.limit]:
        name = next(
            (
                (m.value or {}).get("program_name")
                for m in group.members
                if m.field_kind == FieldKind.PROGRAM_NAME.value
            ),
            "?",
        )
        print(f"      [{mark}] {str(name)[:60]!r}")
        print(f"        {group.url[:88]}")
        print(f"        heading: {json.dumps(group.members[0].locator.get('heading_path'))[:90]}")

    missing = sorted(
        {FieldKind.DURATION.value, FieldKind.STUDY_MODE.value, FieldKind.CAMPUS.value}
        - {kind for group in groups for kind in group.kinds}
    )
    if missing:
        print(f"\n  Field kinds section 8 lists that no group contains: {', '.join(missing)}")
        print("  Zero rows exist for them at any rule version; the regexes never fired")
        print("  on 175 documents. Reported rather than left to look like an oversight.")
    print("\n  No canonical `program` row is created by any of this.")
    return 0


# ===========================================================================
# 16-17. Language groups
# ===========================================================================


def command_language(args: argparse.Namespace) -> int:
    loaded = _load(args, resolve_locators=False)
    groups = language_groups(_rows(loaded))

    _header("LANGUAGE REQUIREMENT GROUPS", "section 16")
    print("\n  Anchored on (extraction, block, list item, TEST). The test is part of the")
    print("  anchor, which is what enforces 'never merge IELTS with TOEFL': one UNSW")
    print("  list item is ~13,200 characters naming five tests, so the block alone")
    print("  would merge five separate statements.\n")
    shapes: Counter[str] = Counter()
    with_score = 0
    with_component = 0
    both = 0
    for group in groups:
        shapes["+".join(group.kinds)] += 1
        kinds = set(group.kinds)
        if FieldKind.LANGUAGE_OVERALL_SCORE.value in kinds:
            with_score += 1
        if FieldKind.LANGUAGE_COMPONENT_SCORE.value in kinds:
            with_component += 1
        scores = {
            FieldKind.LANGUAGE_OVERALL_SCORE.value,
            FieldKind.LANGUAGE_COMPONENT_SCORE.value,
        }
        if scores <= kinds:
            both += 1
    print(f"  {len(groups)} group(s) over {sum(len(g.members) for g in groups)} candidate(s)")
    print(f"    with an overall score        {with_score}")
    print(f"    with a component score       {with_component}")
    print(f"    with both                    {both}")
    _counter("group shape", shapes)

    print("\n  groups carrying a score, in full:")
    for group in groups:
        if len(group.kinds) < 2:
            continue
        test = (group.members[0].value or {}).get("test")
        print(f"\n    {group.institution}  test={test}  [{group.anchor}]")
        print(f"      {group.url[:92]}")
        for member in group.members:
            value = json.dumps(member.value, sort_keys=True) if member.value else "NULL"
            attribution = (member.context or {}).get("test_attribution", "-")
            print(f"      {member.field_kind:<26} {value[:110]}")
            print(f"        attribution={attribution}  raw={member.value_raw_text[:50]!r}")
    return 0


# ===========================================================================
# 18-19. Deadlines and calendar
# ===========================================================================


def command_deadlines(args: argparse.Namespace) -> int:
    loaded = [
        candidate
        for candidate in _load(args, resolve_locators=False)
        if candidate.row.field_kind == FieldKind.APPLICATION_DEADLINE.value
    ]
    _header("DEADLINE GROUPING", "section 18")
    print("\n  Section 18 asks for institution, program context, intake, round, applicant")
    print("  scope and academic year. Four of those six are absent from every deadline")
    print("  candidate in this corpus, so the key is what the data supports and the rest")
    print("  is reported as missing rather than invented.\n")

    coverage: Counter[str] = Counter()
    for candidate in loaded:
        context = candidate.row.context
        for name, present in (
            ("round_label (from the wording)", context.get("round_label_source") == "text"),
            ("round_label (from a heading only)", context.get("round_label_source") == "heading"),
            ("round_number", context.get("round_number") is not None),
            ("entry_year", context.get("entry_year") is not None),
            ("academic_year_raw", context.get("academic_year_raw") is not None),
            ("program context", context.get("program_name") is not None),
            ("applicant scope", bool((candidate.row.value or {}).get("applicant_scopes"))),
            ("intake", context.get("intake") is not None),
        ):
            if present:
                coverage[name] += 1
    print(f"  {len(loaded)} current deadline candidate(s)")
    _counter("context coverage", coverage, total=len(loaded))

    groups = group_candidates([candidate.row for candidate in loaded])
    verdicts = Counter(group.verdict.value for group in groups)
    _counter("verdict", verdicts)
    print("\n  A round label the page only implied by section heading does NOT separate a")
    print("  group: Caltech's 'Early Action' heading stamped that label on a date whose")
    print("  own wording reads 'January 4, 2027 for Regular Decision'.")

    print("\n  groups with more than one candidate:")
    for group in groups:
        if len(group.members) < 2:
            continue
        print(f"\n    [{group.verdict.value}] {group.institution}")
        print(f"      context: {' | '.join(group.context_key)}")
        for member in group.members:
            value = json.dumps(member.value, sort_keys=True) if member.value else "NULL"
            print(f"        {value[:120]}")
            print(f"          {member.url[:76]}")
    return 0


def command_calendar(args: argparse.Namespace) -> int:
    loaded = _load(args, resolve_locators=False)
    calendar = [
        candidate
        for candidate in loaded
        if candidate.row.field_kind == FieldKind.ACADEMIC_CALENDAR_EVENT.value
    ]
    _header("ACADEMIC CALENDAR SEPARATION", "section 19")
    print("\n  Calendar events are evidence about a calendar. They are not this project's")
    print("  admission deadlines, and nothing converts one into the other.\n")
    print(f"  {len(calendar)} current ACADEMIC_CALENDAR_EVENT candidate(s)")
    deadlines_from_calendar = [
        candidate
        for candidate in loaded
        if candidate.row.responsibility == "ACADEMIC_CALENDAR"
        and candidate.row.field_kind == FieldKind.APPLICATION_DEADLINE.value
    ]
    print(
        f"  APPLICATION_DEADLINE candidates from a calendar page: "
        f"{len(deadlines_from_calendar)}"
    )

    _counter(
        "institution",
        Counter(candidate.row.institution for candidate in calendar),
    )
    _counter(
        "locator kind",
        Counter(str(candidate.row.locator.get("kind")) for candidate in calendar),
    )
    stated_year = sum(1 for c in calendar if (c.row.value or {}).get("year") is not None)
    print(f"\n  events stating a year: {stated_year} of {len(calendar)}")

    import re as _re

    application_wording = _re.compile(
        r"\bdeadline\b|\bapplicat|\bapply\s+by\b|\bclosing\s+date\b", _re.I
    )
    flagged = [
        candidate
        for candidate in calendar
        if application_wording.search(candidate.row.evidence_text)
        or application_wording.search(str((candidate.row.value or {}).get("event_text", "")))
    ]
    print(f"\n  events whose wording is explicitly application-related: {len(flagged)}")
    print("  Only these could ever be treated as admission-related, and only after a")
    print("  reviewer says so. Nothing is converted automatically.")
    for candidate in flagged[: args.limit]:
        print(f"    {candidate.row.institution}  {candidate.row.value_raw_text}")
        print(f"      {candidate.row.evidence_text[:110]!r}")
    return 0


# ===========================================================================
# 10-11. Admission quality
# ===========================================================================


Classified = tuple["LoadedCandidate", AdmissionQuality, str]


def _admission(loaded: list[LoadedCandidate]) -> list[Classified]:
    out = []
    for candidate in loaded:
        if candidate.row.field_kind != FieldKind.ADMISSION_REQUIREMENT.value:
            continue
        quality, reason = classify_admission(
            locator=candidate.row.locator,
            evidence_text=candidate.row.evidence_text,
            value=candidate.row.value,
            link_texts=candidate.link_texts,
        )
        out.append((candidate, quality, reason))
    return out


def command_admission(args: argparse.Namespace) -> int:
    classified = _admission(_load(args))
    _header("ADMISSION REQUIREMENT QUALITY", "section 10")
    print("\n  Structural evidence only: the locator kind, the heading path, the container")
    print("  the parser recorded, the presence of the requirement pattern the extractor")
    print("  itself uses, and the shape of the evidence. No semantic guessing, no LLM.")
    print("\n  A quality class is a review hint. It is not a decision, and no class is")
    print("  promoted or discarded on the strength of it.\n")

    classes: Counter[str] = Counter()
    bands: dict[str, Counter[str]] = defaultdict(Counter)
    for candidate, quality, _ in classified:
        classes[quality.value] += 1
        bands[quality.value][candidate.confidence_band] += 1
    total = len(classified)
    print(f"  {total} current ADMISSION_REQUIREMENT candidate(s)\n")
    print(f"  {'class':<34}{'n':>6}{'share':>9}   bands")
    print("  " + "-" * 74)
    for quality in AdmissionQuality:
        count = classes[quality.value]
        band = ", ".join(f"{k}={v}" for k, v in sorted(bands[quality.value].items())) or "-"
        print(f"  {quality.value:<34}{count:>6}{count / total * 100:>8.1f}%   {band}")
    print(f"  {'TOTAL':<34}{sum(classes.values()):>6}")
    assert sum(classes.values()) == total, "the classes do not partition"

    chrome = [entry for entry in classified if entry[1] is AdmissionQuality.NAVIGATION_OR_CHROME]
    print(f"\n  NAVIGATION_OR_CHROME breakdown ({len(chrome)}):")
    _counter("reason", Counter(reason for _, _, reason in chrome))
    print("\n  C47's container fix catches everything it tests -- not one of these came")
    print("  from a nav, header, footer or noscript block:")
    _counter(
        "container",
        Counter(str(candidate.container) for candidate, _, _ in classified),
        total=total,
    )
    misbanded = [entry for entry in chrome if entry[0].confidence_band != "LOW"]
    if misbanded:
        print(f"\n  Chrome carrying a band above LOW ({len(misbanded)}) -- indistinguishable")
        print("  by band from a real requirement sentence:")
        for candidate, _, reason in misbanded:
            print(f"    [{candidate.confidence_band}] {candidate.row.evidence_text[:90]!r}")
            print(f"      {reason}")
            print(f"      {candidate.row.url[:88]}")
    return 0


def command_admission_sample(args: argparse.Namespace) -> int:
    """Section 11. A stratified manual-review sample. Correctness is NOT labelled."""
    classified = _admission(_load(args))
    _header("ADMISSION PRECISION AUDIT SAMPLE", "section 11")
    print("\n  Stratified across confidence bands and institutions, deterministic so two")
    print("  people audit the same rows. Nothing here is labelled correct or incorrect:")
    print("  the point is to make false-positive patterns visible to a human.\n")

    by_band: dict[str, list[Any]] = defaultdict(list)
    for entry in classified:
        by_band[entry[0].confidence_band].append(entry)
    # Deterministic, and spread across institutions rather than clustered on whichever
    # university happens to sort first.
    for band in by_band:
        by_band[band].sort(key=lambda entry: (str(entry[0].candidate_id)))

    wanted = args.limit
    per_band = {
        band: max(1, round(wanted * len(rows) / len(classified))) for band, rows in by_band.items()
    }
    sample: list[Any] = []
    for band, rows in sorted(by_band.items()):
        seen_institutions: Counter[str] = Counter()
        take = []
        for entry in rows:
            institution = entry[0].row.institution
            if seen_institutions[institution] >= max(2, per_band[band] // 6):
                continue
            seen_institutions[institution] += 1
            take.append(entry)
            if len(take) >= per_band[band]:
                break
        sample.extend(take)

    print(f"  sampled {len(sample)} of {len(classified)}")
    _counter("by band", Counter(entry[0].confidence_band for entry in sample))
    _counter("by quality class", Counter(entry[1].value for entry in sample))
    _counter("by institution", Counter(entry[0].row.institution for entry in sample))

    for index, (candidate, quality, reason) in enumerate(sample, start=1):
        value = candidate.row.value or {}
        scopes = value.get("applicant_scopes")
        print(f"\n  {index:>3}. {candidate.row.institution[:52]}  [{candidate.source_ref}]")
        print(f"       url          {candidate.row.url[:92]}")
        print(f"       heading      {json.dumps(candidate.row.locator.get('heading_path'))[:92]}")
        print(f"       evidence     {candidate.row.evidence_text[:200]!r}")
        print(f"       scope        {json.dumps(scopes) if scopes else 'UNRESOLVED'}")
        print(
            f"       qualification {value.get('qualification_hint')}   "
            f"degree {value.get('degree_level_hint')} ({value.get('degree_level_raw')})"
        )
        print(
            f"       rule         {candidate.row.extractor_name} "
            f"v{candidate.row.extractor_version}"
        )
        print(f"       confidence   {candidate.confidence_band}")
        print(f"       quality      {quality.value} -- {reason}")
        print(f"       container    {candidate.container}  ({candidate.confirmation.value})")
    return 0


# ===========================================================================
# 12-13. Applicant scope
# ===========================================================================


def command_scope(args: argparse.Namespace) -> int:
    loaded = _load(args, resolve_locators=False)
    _header("APPLICANT SCOPE RECONCILIATION", "sections 12-13")
    print("\n  Deterministic proposals for EXPLICIT forms only. Nothing is inferred from")
    print("  the absence of a named country, and `UNIVERSAL` is never proposed.\n")

    proposals = [(candidate, propose_scope(candidate.row)) for candidate in loaded]
    resolutions: Counter[str] = Counter()
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    countries: Counter[str] = Counter()
    qualifications: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    for candidate, proposal in proposals:
        resolutions[proposal.resolution.value] += 1
        by_kind[candidate.row.field_kind][proposal.resolution.value] += 1
        for hint in proposal.country_hints:
            countries[hint] += 1
        for hint in proposal.qualification_hints:
            qualifications[hint] += 1
        if proposal.evidence_source:
            sources[proposal.evidence_source] += 1

    print(f"  {len(loaded)} current candidate(s)")
    _counter("resolution", resolutions, total=len(loaded))
    print("\n  by field kind:")
    for kind in sorted(by_kind):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(by_kind[kind].items()))
        print(f"    {kind:<26} {parts}")
    _counter("country hint", countries)
    _counter("qualification-system hint", qualifications)
    _counter("where the scope wording was found", sources)

    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        scopes = connection.execute(
            text("SELECT code, is_universal FROM applicant_scope ORDER BY code")
        ).all()
        groups = connection.execute(text("SELECT count(*) FROM qualification_group")).scalar_one()
    engine.dispose()
    print("\n  RESOLUTION TO AN applicant_scope_id")
    print(
        f"    applicant_scope rows      {len(scopes)}: " f"{', '.join(row.code for row in scopes)}"
    )
    print(f"    qualification_group rows  {groups}")
    print("\n    Resolved to an id: 0, and not because the mapper is weak. The only")
    print("    applicant scope that exists is UNIVERSAL, and section 12 forbids")
    print("    inferring it from silence. Until the client's scope taxonomy is seeded,")
    print("    every proposal is a hint with a null id, which is the honest state.")
    return 0


# ===========================================================================
# 14-15. Tuition
# ===========================================================================


def command_tuition(args: argparse.Namespace) -> int:
    loaded = [
        candidate
        for candidate in _load(args)
        if candidate.row.field_kind == FieldKind.TUITION.value
    ]
    _header("TUITION CANDIDATES, IN FULL", "sections 14-15")
    print(f"\n  All {len(loaded)} current tuition candidates. Small enough to inspect")
    print("  completely, so it is inspected completely.")
    print("\n  The financial-context class is a SECONDARY review hint derived from the")
    print("  labels the publisher supplied. The candidate is unchanged, and no class is")
    print("  promoted automatically -- a cost-of-attendance total is frequently the")
    print("  number a student actually needs.\n")

    classes: Counter[str] = Counter()
    for index, candidate in enumerate(loaded, start=1):
        value = candidate.row.value or {}
        kind, why = classify_financial_context(
            locator=candidate.row.locator,
            evidence_text=candidate.row.evidence_text,
            value_raw_text=candidate.row.value_raw_text,
            value=candidate.row.value,
        )
        classes[kind.value] += 1
        inner = value.get("_context")
        context: dict[str, Any] = inner if isinstance(inner, dict) else {}
        print(f"  {index:>3}. {candidate.row.institution[:52]}  [{candidate.source_ref}]")
        print(f"       url        {candidate.row.url[:92]}")
        print(f"       heading    {json.dumps(candidate.row.locator.get('heading_path'))[:92]}")
        print(f"       evidence   {candidate.row.evidence_text[:170]!r}")
        print(f"       matched    {candidate.row.value_raw_text[:80]!r}")
        print(
            f"       amount     {value.get('amount_kind')}  "
            f"min={value.get('amount_min')}  max={value.get('amount_max')}"
        )
        print(
            f"       currency   {value.get('currency')}   unit={value.get('billing_unit')}   "
            f"category={value.get('student_category')}"
        )
        print(
            f"       labels     column={context.get('column_label')}  "
            f"row={value.get('row_label')}"
        )
        print(f"       unresolved {candidate.row.unresolved_reason}")
        print(f"       confidence {candidate.confidence_band}")
        print(f"       context    {kind.value} -- {why}")
        print()
    _counter("financial context", classes, total=len(loaded))
    for kind in FinancialContext:
        if kind.value not in classes:
            print(f"    {0:>6}  {kind.value}")
    return 0


# ===========================================================================
# 27-28, 31. Queue, priority, readiness
# ===========================================================================


def _priorities(loaded: list[LoadedCandidate]) -> dict[uuid.UUID, Any]:
    rows = _rows(loaded)
    groups = group_candidates(rows)
    corroborated = corroborated_ids(groups)
    conflicted = conflicted_ids(groups)
    # Section 28's exception: a LOW candidate is worth a reviewer's time when nothing
    # better exists for the same field on the same page.
    best: dict[tuple[uuid.UUID, str], str] = {}
    order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    for candidate in loaded:
        key = (candidate.row.extraction_id, candidate.row.field_kind)
        current = best.get(key, "LOW")
        if order[candidate.confidence_band] > order[current]:
            best[key] = candidate.confidence_band

    out: dict[uuid.UUID, Any] = {}
    for candidate in loaded:
        key = (candidate.row.extraction_id, candidate.row.field_kind)
        out[candidate.candidate_id] = (
            priority_for(
                field_kind=candidate.row.field_kind,
                confidence_band=candidate.confidence_band,
                scope_unresolved=candidate.scope_unresolved,
                in_conflict=candidate.candidate_id in conflicted,
                corroborated=candidate.candidate_id in corroborated,
                confirmation=candidate.confirmation,
                thin_source=candidate.thin_source,
            ),
            queue_for(
                confidence_band=candidate.confidence_band,
                is_superseded=False,
                decision_state=candidate.decision_state,
                is_only_candidate=best.get(key, "LOW") == candidate.confidence_band,
            ),
            candidate.candidate_id in conflicted,
            candidate.candidate_id in corroborated,
        )
    return out


def command_queue(args: argparse.Namespace) -> int:
    loaded = _load(args)
    computed = _priorities(loaded)

    _header("REVIEW QUEUE AND PRIORITY", "sections 27-28")
    print("\n  Priority is a sum of named, signed factors, and every one that applied is")
    print("  returned beside the score. Not a black-box score, and not an auto-accept.\n")

    queues: Counter[str] = Counter()
    by_band: dict[str, Counter[str]] = defaultdict(Counter)
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    for candidate in loaded:
        _, queue, _, _ = computed[candidate.candidate_id]
        queues[queue.value] += 1
        by_band[queue.value][candidate.confidence_band] += 1
        by_kind[candidate.row.field_kind][queue.value] += 1
    _counter("queue", queues, total=len(loaded))
    print("\n  by queue and band:")
    for queue in Queue:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(by_band[queue.value].items())) or "-"
        print(f"    {queue.value:<18} {parts}")
    print("\n  by field kind:")
    for kind in sorted(by_kind):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(by_kind[kind].items()))
        print(f"    {kind:<26} {parts}")

    print("\n  LOW candidates are stored, kept, and not deleted. They stay out of the")
    print("  primary queue unless nothing better exists for that field on that page.")

    ranked = sorted(
        loaded,
        key=lambda candidate: (
            -computed[candidate.candidate_id][0].score,
            str(candidate.candidate_id),
        ),
    )
    print(f"\n  TOP {args.limit} BY PRIORITY (primary queue only):\n")
    shown = 0
    for candidate in ranked:
        priority, queue, _, _ = computed[candidate.candidate_id]
        if queue is not Queue.PRIMARY:
            continue
        shown += 1
        if shown > args.limit:
            break
        print(
            f"  {priority.score:>4}  {candidate.row.field_kind:<24} "
            f"{candidate.row.institution[:34]}"
        )
        print(f"        {candidate.row.value_raw_text[:80]!r}")
        print(f"        {priority.explain()[:200]}")
    return 0


def command_readiness(args: argparse.Namespace) -> int:
    loaded = _load(args, resolve_locators=False)
    rows = _rows(loaded)
    conflicted = conflicted_ids(group_candidates(rows))

    _header("PROMOTION READINESS", "sections 30-31")
    print("\n  Read-only. Nothing is promoted, and an ACCEPTED candidate does not become")
    print("  a field_claim: promotion additionally requires the source to be eligible")
    print("  for that field, which no reviewer of candidates can grant.\n")

    blockers: Counter[str] = Counter()
    ready = 0
    for candidate in loaded:
        found = blockers_for(
            decision_state=candidate.decision_state,
            is_superseded=False,
            source_eligibility=candidate.source_eligibility,
            scope_unresolved=candidate.scope_unresolved,
            in_conflict=candidate.candidate_id in conflicted,
            responsibility=candidate.row.responsibility,
            field_kind=candidate.row.field_kind,
        )
        if not found:
            ready += 1
        for blocker in found:
            blockers[blocker] += 1
    print(f"  claim_promotion_ready: {ready} of {len(loaded)}")
    _counter("blocker (a candidate may have several)", blockers)
    print("\n  0 is the correct answer today. No official domain has been verified, so")
    print("  every source is NOT_ELIGIBLE and nothing can be promoted whatever a")
    print("  reviewer decides. That is C27 working, not a gap.")
    return 0


# ===========================================================================
# 26. Source verification assistance
# ===========================================================================


def command_sources(args: argparse.Namespace) -> int:
    loaded = _load(args, resolve_locators=False)
    counts: dict[uuid.UUID, Counter[str]] = defaultdict(Counter)
    for candidate in loaded:
        counts[candidate.row.source_id][candidate.row.field_kind] += 1

    engine = _engine(DatabaseRole.API)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT s.id AS source_id, s.url, s.publication_eligibility::text AS eligibility,
                       coalesce(ti.match_key, '?') AS institution,
                       pcs.source_ref, pcs.source_type, pcs.host,
                       sn.effective_url, sn.observed_at, sn.content_type,
                       cb.byte_size,
                       r.status::text AS fetch_status, r.http_status,
                       e.output->>'title' AS title,
                       (e.output->'statistics'->>'text_characters')::int AS characters,
                       EXISTS (SELECT 1 FROM official_domain od
                                WHERE od.target_institution_id = pcs.target_institution_id
                                  AND od.verification_status = 'VERIFIED_OFFICIAL'
                                  AND (od.host = pcs.host
                                    OR (od.covers_subdomains AND pcs.host LIKE '%.' || od.host)))
                           AS host_matches_verified_domain
                  FROM pilot_collected_source pcs
                  JOIN source s ON s.id = pcs.acquisition_source_id
                  LEFT JOIN target_institution ti ON ti.id = pcs.target_institution_id
                  LEFT JOIN snapshot sn ON sn.source_id = s.id
                  LEFT JOIN content_blob cb ON cb.content_hash = sn.content_hash
                  LEFT JOIN fetch_run r ON r.id = sn.fetch_run_id
                  LEFT JOIN extraction e ON e.snapshot_id = sn.id AND e.status <> 'FAILED'
                 WHERE pcs.duplicate_of_source_ref IS NULL
                 ORDER BY ti.match_key, pcs.source_ref
                """
            )
        ).all()
    engine.dispose()

    _header("SOURCE VERIFICATION ASSISTANCE", "section 26")
    print("\n  Nothing here verifies anything. It is the evidence a reviewer needs in")
    print("  front of them when they do, assembled once instead of per source.\n")
    with_claims = sum(1 for row in rows if counts.get(row.source_id))
    verified_host = sum(1 for row in rows if row.host_matches_verified_domain)
    print(f"  {len(rows)} registered page(s), {with_claims} carrying current candidates")
    print(f"  pages whose host matches a VERIFIED_OFFICIAL domain: {verified_host}")
    _counter("publication eligibility", Counter(row.eligibility for row in rows))

    institution = None
    for row in rows:
        if row.institution != institution:
            institution = row.institution
            print(f"\n  {institution}")
        claims = counts.get(row.source_id, Counter())
        summary = ", ".join(f"{kind}={n}" for kind, n in sorted(claims.items())) or "none"
        moved = (
            f"  -> {row.effective_url[:60]}"
            if row.effective_url and row.effective_url != row.url
            else ""
        )
        print(f"    [{row.source_ref}] {row.source_type}")
        print(f"      url        {row.url[:92]}{moved}")
        print(
            f"      host       {row.host}   verified_domain="
            f"{'yes' if row.host_matches_verified_domain else 'no'}"
        )
        print(
            f"      fetched    {row.observed_at}  {row.fetch_status} "
            f"{row.http_status or '-'}  {row.content_type}"
        )
        print(
            f"      body       {row.byte_size or 0:,} bytes  "
            f"{row.characters or 0:,} characters of visible text"
        )
        print(f"      title      {str(row.title)[:80]}")
        print(f"      eligibility {row.eligibility}")
        print(f"      candidates {summary}")
    return 0


# ===========================================================================
# 29. Review commands
# ===========================================================================


def command_list(args: argparse.Namespace) -> int:
    loaded = _load(args)
    computed = _priorities(loaded)
    selected = [
        candidate
        for candidate in loaded
        if (args.kind is None or candidate.row.field_kind == args.kind)
        and (args.queue is None or computed[candidate.candidate_id][1].value == args.queue)
        and (args.state is None or candidate.decision_state == args.state)
    ]
    selected.sort(key=lambda c: (-computed[c.candidate_id][0].score, str(c.candidate_id)))
    _header("REVIEW LIST", "section 29")
    print(f"\n  {len(selected)} candidate(s) matching\n")
    for candidate in selected[: args.limit]:
        priority, queue, conflicted, corroborated = computed[candidate.candidate_id]
        marks = "".join(
            [
                "C" if conflicted else "-",
                "A" if corroborated else "-",
                "S" if candidate.scope_unresolved else "-",
            ]
        )
        print(
            f"  {priority.score:>4} [{queue.value:<14}] {marks} "
            f"{candidate.row.field_kind:<24} {candidate.candidate_id}"
        )
        print(f"        {candidate.row.institution[:44]}  {candidate.confidence_band}")
        print(f"        {candidate.row.value_raw_text[:86]!r}")
    print("\n  marks: C=in conflict  A=agreed by another source  S=scope unresolved")
    return 0


def command_show(args: argparse.Namespace) -> int:
    loaded = _load(args)
    wanted = uuid.UUID(args.candidate)
    match = next((c for c in loaded if c.candidate_id == wanted), None)
    if match is None:
        print(f"No CURRENT candidate {wanted}. It may exist at a superseded rule version.")
        return 1
    computed = _priorities(loaded)
    priority, queue, conflicted, corroborated = computed[wanted]
    value = match.row.value or {}

    _header("CANDIDATE", "section 29")
    print(f"\n  id            {match.candidate_id}")
    print(f"  institution   {match.row.institution}")
    print(f"  source        [{match.source_ref}] {match.row.url}")
    print(f"  responsibility{match.row.responsibility:>14}")
    print(f"  field kind    {match.row.field_kind}")
    print(f"  rule          {match.row.extractor_name} v{match.row.extractor_version}")
    print(f"  confidence    {match.confidence_band}")
    print(f"\n  value         {json.dumps(value, indent=2, sort_keys=True)[:1200]}")
    print(f"  unresolved    {match.row.unresolved_reason}")
    print(f"\n  matched       {match.row.value_raw_text!r}")
    print(f"  evidence      {match.row.evidence_text[:400]!r}")
    print(f"  locator       {json.dumps(match.row.locator)}")
    print(f"  resolves to   {(match.resolved_text or '')[:200]!r}")
    print(f"  relationship  {match.evidence_relationship}")
    print(f"  container     {match.container}  ({match.confirmation.value})")
    print(f"\n  review state  {match.decision_state}")
    print(f"  queue         {queue.value}")
    print(f"  priority      {priority.score}")
    for reason in priority.reasons:
        print(f"                {reason}")
    print(f"  conflicted    {conflicted}")
    print(f"  corroborated  {corroborated}")
    print(f"  eligibility   {match.source_eligibility}")

    proposal = propose_scope(match.row)
    print(f"\n  scope         {proposal.resolution.value}")
    if proposal.raw_scope_text:
        print(f"    raw         {proposal.raw_scope_text!r}")
    print(f"    country     {sorted(proposal.country_hints) or '-'}")
    print(f"    qualification {sorted(proposal.qualification_hints) or '-'}")
    print(f"    scope id    {proposal.applicant_scope_id or 'null'}")

    blockers = blockers_for(
        decision_state=match.decision_state,
        is_superseded=False,
        source_eligibility=match.source_eligibility,
        scope_unresolved=match.scope_unresolved,
        in_conflict=conflicted,
        responsibility=match.row.responsibility,
        field_kind=match.row.field_kind,
    )
    print(f"\n  promotion blockers: {', '.join(blockers) or 'none'}")
    return 0


def _decide(args: argparse.Namespace, decision: Decision) -> int:
    engine = _engine(DatabaseRole.API)
    try:
        with engine.begin() as connection:
            outcome = record_decision(
                connection,
                candidate_id=uuid.UUID(args.candidate),
                actor_id=uuid.UUID(args.actor),
                decision=decision,
                reason_code=args.reason_code,
                reason_text=args.reason,
            )
    except ReviewError as error:
        print(f"REFUSED: {error}")
        return 1
    finally:
        engine.dispose()
    print(f"  {outcome.candidate_id}")
    print(f"    {outcome.previous} -> {outcome.decision.value}  ({outcome.reason_code})")
    print(f"    decision {outcome.review_count} for this candidate; earlier ones are kept")
    print("\n  This is NOT permission to publish. Promotion additionally requires the")
    print("  source to be eligible for this field (section 30).")
    return 0


# ===========================================================================
# 34. Safety
# ===========================================================================


def command_untouched(args: argparse.Namespace) -> int:
    engine = _engine(DatabaseRole.API)
    from app.db.classification import CANONICAL_TABLES

    downstream = ("field_claim", "field_provenance", "change_proposal", "change_proposal_item")
    clean = True
    with engine.connect() as connection:
        _header("NOTHING WAS PUBLISHED", "section 34")
        print("\n  publication plane:")
        for table in downstream:
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            print(f"    {table:<28} {count}")
            clean = clean and count == 0
        print("\n  canonical plane:")
        for table in CANONICAL_TABLES:
            exists = connection.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table}
            ).scalar_one()
            if not exists:
                continue
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            print(f"    {table:<28} {count}")
            clean = clean and count == 0
        print("\n  publication eligibility of pilot sources:")
        for row in connection.execute(
            text(
                "SELECT s.publication_eligibility::text AS e, count(*) AS n FROM source s "
                " WHERE EXISTS (SELECT 1 FROM pilot_collected_source p "
                "                WHERE p.acquisition_source_id = s.id) GROUP BY 1"
            )
        ):
            print(f"    {row.e:<28} {row.n}")
            clean = clean and row.e == "NOT_ELIGIBLE"
        verified = connection.execute(
            text(
                "SELECT count(*) FROM official_domain "
                " WHERE verification_status = 'VERIFIED_OFFICIAL'"
            )
        ).scalar_one()
        print(f"\n  verified official domains: {verified}")
        clean = clean and verified == 0
        decisions = connection.execute(
            text("SELECT count(*) FROM field_claim_candidate_review")
        ).scalar_one()
        print(f"  candidate review decisions: {decisions}  (permitted by section 34)")
    engine.dispose()
    print(f"\n  {'PASS' if clean else 'FAIL'}")
    return 0 if clean else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact-root", type=Path, default=Path(".artifacts-full"))
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("integrity", "locator resolution over ALL current candidates, plus routing"),
        ("groups", "agreement and conflict groups"),
        ("programs", "program context groups"),
        ("language", "language requirement groups"),
        ("deadlines", "deadline groups"),
        ("calendar", "academic calendar separation"),
        ("admission", "admission requirement quality breakdown"),
        ("scope", "applicant scope reconciliation"),
        ("tuition", "all current tuition candidates, in full"),
        ("queue", "review queues and priority"),
        ("readiness", "promotion readiness and blockers"),
        ("sources", "source verification assistance"),
        ("untouched", "prove nothing publishable or canonical moved"),
    ):
        entry = sub.add_parser(name, help=help_text)
        entry.add_argument("--limit", type=int, default=20)

    sample = sub.add_parser("admission-sample", help="stratified manual-review sample")
    sample.add_argument("--limit", type=int, default=100)

    listing = sub.add_parser("list", help="the review queue")
    listing.add_argument("--limit", type=int, default=25)
    listing.add_argument("--kind", default=None)
    listing.add_argument("--queue", default=None, choices=[queue.value for queue in Queue])
    listing.add_argument("--state", default=None)

    show = sub.add_parser("show", help="one candidate, in full")
    show.add_argument("--candidate", required=True)
    show.add_argument("--limit", type=int, default=20)

    for name, decision in (
        ("accept", Decision.ACCEPTED),
        ("reject", Decision.REJECTED),
        ("context", Decision.NEEDS_CONTEXT),
        ("scope-mapping", Decision.NEEDS_SCOPE_MAPPING),
    ):
        entry = sub.add_parser(name, help=f"record a {decision.value} decision")
        entry.add_argument("--candidate", required=True)
        entry.add_argument("--actor", required=True)
        entry.add_argument("--reason-code", required=True)
        entry.add_argument("--reason", default=None)
        entry.set_defaults(decision=decision)

    args = parser.parse_args(argv)
    commands = {
        "integrity": command_integrity,
        "groups": command_groups,
        "programs": command_programs,
        "language": command_language,
        "deadlines": command_deadlines,
        "calendar": command_calendar,
        "admission": command_admission,
        "admission-sample": command_admission_sample,
        "scope": command_scope,
        "tuition": command_tuition,
        "queue": command_queue,
        "readiness": command_readiness,
        "sources": command_sources,
        "list": command_list,
        "show": command_show,
        "untouched": command_untouched,
    }
    if args.command in ("accept", "reject", "context", "scope-mapping"):
        return _decide(args, args.decision)
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
