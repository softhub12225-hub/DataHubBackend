"""The pilot data-collection workbook: an .xlsx template generated from the target list.

WHAT THIS IS FOR
================
The client selects the pilot institutions by hand and collects official information
for them. This produces the workbook they fill in. It is generated, never
hand-maintained, and the client's own supplied workbook is never modified.

THE MATCHING KEY IS THE POINT
=============================
Every row on every sheet carries `target_institution_id` -- the UUID primary key of
the `target_institution` row. That is what lets a returned workbook be matched back
to our records unambiguously.

It is deliberately *not* the QS name. QS naming in the supplied list is inconsistent
(`Essex, University of` beside `University of Bradford`; `UCL` as a three-character
string), a university may legitimately rename itself between list versions, and two
institutions in the same list can have names that differ by a single token. Matching
on a name would therefore be a guess. See `pilot_matching.py` for the rule that
applies when the id is missing: manual resolution, never a silent fuzzy match.

The id column is the first column on every sheet, frozen, grey-filled and carries a
"do not edit" comment. If it is edited, the affected rows require manual resolution;
nothing is guessed.

"NOT CHECKED" IS NOT THE SAME AS "NOT PUBLISHED"
================================================
The platform's central distinction (D2 / `FieldStatus`) is that "nobody has looked
yet" and "we looked and the university publishes nothing" are different facts.
Collapsing them into a blank cell would destroy the distinction the whole 未知可见
rule depends on.

So the template carries a status column **exactly where the schema carries one** --
`tuition.amount_field_status`, `application_deadline.deadline_field_status`,
`admission_requirement.requirement_field_status`,
`language_requirement.requirement_field_status`,
`program.lifecycle_field_status`. Each is a dropdown:

  PUBLISHED                 - the value in this row is what the official page states
  OFFICIALLY_NOT_PUBLISHED  - we looked at the official page; it states nothing here
  NOT_CHECKED               - nobody has looked yet   (also what a blank cell means)

A blank status means `NOT_CHECKED`, so an operator who runs out of time leaves rows
blank and nothing is misrepresented. That is also why no field is mandatory.

NOTHING HERE PUBLISHES ANYTHING
===============================
This module reads `target_institution` and the reference vocabularies and writes a
file. It creates no canonical row, contacts no network, and starts no collection. A
returned workbook will be imported as *claims requiring review*, not as published
facts -- see `docs/PILOT_COLLECTION.md`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Connection, text

from app.core.logging import get_logger
from app.db.enums import DegreeScope, FieldStatus, SourceCategory

if TYPE_CHECKING:  # pragma: no cover - import only for annotations
    from openpyxl.worksheet.worksheet import Worksheet

logger = get_logger(__name__)

#: The shape of the workbook this module produces.
#:
#: Stored on every `pilot_submission` so a returned file can be told apart from one
#: filled in against an older export. Without it, a workbook completed before a
#: column was renamed imports as a file with missing columns and no explanation of
#: why. Bump this whenever a sheet gains, loses or renames a collected column.
TEMPLATE_VERSION = "2026.09-u14"

#: The column that matches a returned workbook back to our records. Named once.
MATCH_KEY_COLUMN = "target_institution_id"

#: Status vocabulary offered in the template. `WITHDRAWN` is deliberately absent: it
#: describes a fact the university has taken down, which is a change-detection
#: outcome rather than something a first collection pass can observe.
TEMPLATE_FIELD_STATUSES: tuple[str, ...] = (
    FieldStatus.PUBLISHED.value,
    FieldStatus.OFFICIALLY_NOT_PUBLISHED.value,
    FieldStatus.NOT_CHECKED.value,
)

#: Answer vocabulary for the pilot selection column. `UNDECIDED` exists so a blank
#: cell and a deliberate "not this one" are distinguishable, for the same reason the
#: field statuses are.
PILOT_SELECTION_VALUES: tuple[str, ...] = ("YES", "NO", "UNDECIDED")

ColumnKind = Literal["reference", "collected"]
Requirement = Literal["REQUIRED", "CONDITIONAL", "OPTIONAL"]


@dataclass(frozen=True, slots=True)
class TemplateColumn:
    """One spreadsheet column, and what it feeds."""

    name: str
    kind: ColumnKind
    #: `table.column` this feeds on import, or a note for non-schema columns. Kept so
    #: the template and the schema can be checked against each other by a test.
    feeds: str
    help_text: str
    #: Dropdown values, or the name of a reference vocabulary resolved at build time.
    choices: tuple[str, ...] | None = None
    vocabulary: str | None = None
    width: int = 22
    #: Whether a collector must fill this in. `CONDITIONAL` columns carry the
    #: condition in `requirement_note`, and the README lists both -- "which columns
    #: are mandatory" is the first question anyone filling this in will ask.
    requirement: Requirement = "OPTIONAL"
    requirement_note: str = ""


@dataclass(frozen=True, slots=True)
class TemplateSheet:
    """One sheet of the collection workbook."""

    name: str
    purpose: str
    columns: tuple[TemplateColumn, ...]
    #: True when the sheet holds one row per institution, pre-filled from the target
    #: list. False when the operator adds as many rows as they need.
    one_row_per_institution: bool = False
    example_row: tuple[str, ...] = field(default=())


#: Format of a workbook-local programme reference. Short, typed by hand, and
#: deliberately not a UUID: a collector copies these between sheets all day.
#: U14. The five shapes a published fee can take. Offered as a dropdown, because a
#: free-text kind is a typo away from a row the importer has to reject.
#:
#: Deliberately NOT a status: "the university publishes no fee" is the status column's
#: OFFICIALLY_NOT_PUBLISHED, while VARIABLE means the page does address fees and just
#: does not give a number.
TUITION_AMOUNT_KINDS: tuple[str, ...] = ("EXACT", "RANGE", "FROM", "UP_TO", "VARIABLE")

PROGRAM_REF_PATTERN = r"^P\d{4}$"
SOURCE_REF_PATTERN = r"^S\d{4}$"


def _match_key_column() -> TemplateColumn:
    return TemplateColumn(
        name=MATCH_KEY_COLUMN,
        kind="reference",
        feeds="matching key -> target_institution.id",
        help_text=(
            "DO NOT EDIT. This is how your workbook is matched back to our records. "
            "Copy it exactly when you add a row."
        ),
        width=38,
        requirement="REQUIRED",
    )


def _program_ref_column(*, owner: bool) -> TemplateColumn:
    """`program_ref` on the Programs sheet defines; elsewhere it references.

    Programmes are NOT joined by name text. Two programmes at one university can be
    called "MSc Computer Science" (one full-time, one part-time), a name gets retyped
    with a different dash on the next sheet, and Excel's autocomplete silently
    "helps". A short local code is the only thing a person can copy reliably and a
    machine can check exactly.
    """
    if owner:
        return TemplateColumn(
            "program_ref",
            "collected",
            "workbook-local programme id (NOT the canonical program UUID)",
            "Give every programme its own code: P0001, P0002, P0003... You will use "
            "this code on the other sheets. Any code is fine as long as it is used "
            "once here and spelled the same everywhere.",
            width=12,
            requirement="REQUIRED",
        )
    return TemplateColumn(
        "program_ref",
        "collected",
        "-> Programs.program_ref",
        "The programme this row is about, using the code from the Programs sheet "
        "(P0001...). Leave blank ONLY if this applies to the whole university rather "
        "than one programme.",
        width=12,
        requirement="CONDITIONAL",
        requirement_note="unless the row applies to the whole university",
    )


def _source_ref_column(*, required: bool = True) -> TemplateColumn:
    return TemplateColumn(
        "source_ref",
        "collected",
        "-> Official_Sources.source_ref",
        "The official page this fact came from, using the code from the "
        "Official_Sources sheet (S0001...). This is how we prove the fact; a page "
        "address typed here instead will not be matched.",
        width=12,
        requirement="CONDITIONAL" if required else "OPTIONAL",
        requirement_note="whenever the status is PUBLISHED or OFFICIALLY_NOT_PUBLISHED",
    )


def _status_column(name: str, feeds: str, extra: str = "") -> TemplateColumn:
    return TemplateColumn(
        name,
        "collected",
        feeds,
        "PUBLISHED = the page states it, and you have filled the value in. "
        "OFFICIALLY_NOT_PUBLISHED = you read the page and it states nothing here; "
        "leave the value blank. Blank or NOT_CHECKED = nobody has looked yet. " + extra,
        choices=TEMPLATE_FIELD_STATUSES,
        width=26,
        requirement="CONDITIONAL",
        requirement_note="whenever you have looked at the page",
    )


def _official_text_column(feeds: str) -> TemplateColumn:
    return TemplateColumn(
        "official_text",
        "collected",
        feeds,
        "Paste the wording from the official page, exactly as written. Do not "
        "summarise, translate or tidy it. This exact wording is what a reviewer "
        "checks the value against.",
        width=64,
        requirement="CONDITIONAL",
        requirement_note="whenever the status is PUBLISHED",
    )


def _source_url_column() -> TemplateColumn:
    return TemplateColumn(
        "source_url",
        "collected",
        "readability only -- source_ref is the link that is used",
        "Optional. The page address, so you can see at a glance where the row came "
        "from. We match on source_ref, never on this.",
        width=48,
    )


def _scope_columns() -> tuple[TemplateColumn, ...]:
    """Applicant scope, collected as hints rather than as a taxonomy decision.

    `admission_requirement.applicant_scope_id` is NOT NULL and the only seeded scope
    is UNIVERSAL, so a naive importer would map everything to "all applicants" -- and
    "IELTS 7.0, or 6.5 for holders of a Chinese bachelor degree from a 985/211
    institution" would silently become a requirement on everybody. That is a wrong
    published fact, not a modelling shortcut.
    """
    return (
        TemplateColumn(
            "applicant_scope_hint",
            "collected",
            "collection hint -> applicant_scope (mapped later by a human)",
            "Who does this apply to? Write UNIVERSAL if the page states one rule for "
            "everyone. Otherwise copy the university's own words: 'Mainland China', "
            "'international students', 'holders of a Chinese bachelor degree', "
            "'985/211 institutions', 'IB', 'A-level'. Do not write UNIVERSAL just "
            "because no group is obvious -- leave it blank and we will ask.",
            width=32,
            requirement="CONDITIONAL",
            requirement_note="whenever the page states a rule for a particular group",
        ),
        TemplateColumn(
            "applicant_country_code",
            "collected",
            "collection hint -> applicant_scope_criterion (nationality dimension)",
            "If the rule is about one country, its two-letter code: CN for mainland "
            "China, HK, TW, MO, GB, US. Leave blank if the rule is not about "
            "nationality.",
            width=14,
        ),
        TemplateColumn(
            "qualification_hint",
            "collected",
            "collection hint -> qualification_group (mapped later by a human)",
            "The qualification the rule names, in the university's words: 'Chinese "
            "bachelor degree', '985/211', 'IB Diploma', 'A-level', 'Gaokao'. Leave "
            "blank if the rule names no qualification.",
            width=32,
        ),
    )


# ===========================================================================
# Sheet definitions
#
# Declarative on purpose: the exporter renders them, the validator reads the same
# definitions rather than re-describing the file, and a test asserts every `feeds`
# target is a real schema column.
#
# No sheet contains example data. Fabricated rows in a collection sheet get
# submitted back as if they were real; every example lives in the README.
# ===========================================================================

PILOT_UNIVERSITIES = TemplateSheet(
    name="Pilot_Universities",
    purpose=(
        "Every candidate institution, one row each. Mark the ones in the pilot, and "
        "give each marked institution its official name and homepage."
    ),
    one_row_per_institution=True,
    columns=(
        _match_key_column(),
        TemplateColumn(
            "qs_name",
            "reference",
            "target_list_entry.qs_name (reference only -- NOT a publishable name)",
            "The name as the supplied list wrote it, so you can recognise the row. We "
            "do not publish this; put the institution's own official name in "
            "official_name_en.",
            width=44,
        ),
        TemplateColumn(
            "qs_rank",
            "reference",
            "target_list_entry.qs_rank (reference only)",
            "Rank from the supplied list, for orientation only. Not published, and it "
            "does not decide which institutions are in the pilot.",
            width=10,
        ),
        TemplateColumn(
            "region",
            "reference",
            "target_list_entry.region_label (reference only)",
            "Region as the supplied list wrote it.",
            width=14,
        ),
        TemplateColumn(
            "destination_code",
            "reference",
            "target_institution.destination_code",
            "Our destination code for this institution.",
            width=10,
        ),
        TemplateColumn(
            "selected",
            "collected",
            "target_institution.pilot_wave (YES -> wave 1)",
            "YES puts this institution in the pilot. Leave blank or write UNDECIDED "
            "otherwise -- an unselected institution stays a valid future target and "
            "nothing about it is lost.",
            choices=PILOT_SELECTION_VALUES,
            width=12,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "official_name_en",
            "collected",
            "university.name_en",
            "The institution's own official English name, copied from its official "
            "site. Not the name in the qs_name column.",
            width=44,
            requirement="CONDITIONAL",
            requirement_note="when selected = YES",
        ),
        TemplateColumn(
            "official_name_zh",
            "collected",
            "university.name_zh",
            "Official Chinese name IF the institution publishes one itself. Leave "
            "blank rather than translating it yourself.",
            width=28,
        ),
        TemplateColumn(
            "official_homepage",
            "collected",
            "source_mapping.url (category UNIVERSITY_HOME)",
            "The institution's official homepage. Must start with http:// or https://.",
            width=40,
            requirement="CONDITIONAL",
            requirement_note="when selected = YES",
        ),
        TemplateColumn(
            "city",
            "collected",
            "university.city",
            "Main city, as the official site states it.",
            width=18,
        ),
        TemplateColumn(
            "notes",
            "collected",
            "target_institution.notes",
            "Anything a reviewer should know: two institutions with similar names, a "
            "merger, a site that will not load.",
            width=40,
        ),
    ),
)

OFFICIAL_SOURCES = TemplateSheet(
    name="Official_Sources",
    purpose=(
        "Every official page you used, once each, with a code you reference from the "
        "fact sheets. Add as many rows per institution as you need."
    ),
    columns=(
        _match_key_column(),
        TemplateColumn(
            "source_ref",
            "collected",
            "workbook-local source id",
            "Give every page its own code: S0001, S0002, S0003... You will use this "
            "code on the fact sheets instead of pasting the address again.",
            width=12,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "source_type",
            "collected",
            "source_mapping.source_category",
            "What kind of page this is. Pick from the list.",
            choices=tuple(category.value for category in SourceCategory),
            width=28,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "degree_level",
            "collected",
            "source_degree_scope.degree_scope",
            "Which applicants this page serves. If it serves more than one group, add "
            "one row per group -- a blank here means we do not know that it covers "
            "anyone, NOT that it covers everyone. A taught-Masters page is "
            "TAUGHT_POSTGRADUATE; choose RESEARCH_POSTGRADUATE only if the page "
            "really covers PhD study.",
            choices=tuple(scope.value for scope in DegreeScope),
            width=24,
        ),
        TemplateColumn(
            "official_url",
            "collected",
            "source_mapping.url",
            "The exact page address. http:// or https:// only, and not a page that "
            "needs a login.",
            width=56,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "checked_at",
            "collected",
            "source_mapping.notes (recorded as the check date)",
            "The date you looked at this page, written as YYYY-MM-DD.",
            width=14,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "is_third_party",
            "collected",
            "official_domain.verification_status (AUTHORIZED_EXTERNAL)",
            "YES if this page is NOT on the university's own web address -- for "
            "example an application portal run by another company. Then say in notes "
            "what made you conclude the university authorised it.",
            choices=("YES", "NO"),
            width=14,
        ),
        TemplateColumn(
            "notes",
            "collected",
            "source_mapping.notes",
            "How you found it, and for a third-party page, the authorisation you " "relied on.",
            width=40,
        ),
    ),
)

PROGRAMS = TemplateSheet(
    name="Programs",
    purpose=(
        "Programmes in the pilot subjects. One row per programme. This sheet defines "
        "the program_ref codes the other sheets use."
    ),
    columns=(
        _match_key_column(),
        _program_ref_column(owner=True),
        TemplateColumn(
            "program_name_en",
            "collected",
            "program.name_en",
            "Official programme title, exactly as the official page writes it.",
            width=48,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "degree_level_code",
            "collected",
            "program.degree_level_code",
            "Award level. Pick from the list.",
            vocabulary="degree_level",
            width=16,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "discipline_code",
            "collected",
            "program_discipline.discipline_id",
            "Which of the three pilot subjects this belongs to. If it genuinely spans "
            "two, pick the closer one and say so in notes.",
            vocabulary="discipline",
            width=22,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "discipline_hint",
            "collected",
            "kept verbatim for later mapping",
            "What the university calls the subject, in their words: 'Computer "
            "Science', 'Artificial Intelligence', 'Data Science', 'Finance', "
            "'Mechanical Engineering'. We keep this exactly as you write it.",
            width=30,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "faculty_or_school",
            "collected",
            "faculty.name_en",
            "The faculty, school or department that runs it.",
            width=36,
        ),
        TemplateColumn(
            "duration_value",
            "collected",
            "program_offering.duration_value",
            "Length as a number: 1, 2, 12.",
            width=12,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "duration_unit",
            "collected",
            "program_offering.duration_unit",
            "Unit for the length above.",
            choices=("YEAR", "MONTH", "SEMESTER", "TERM"),
            width=14,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "study_mode",
            "collected",
            "program_offering.study_mode",
            "Full-time or part-time, as offered. If the university offers both and "
            "the fees or entry rules differ, use one row per mode.",
            choices=("FULL_TIME", "PART_TIME"),
            width=14,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "delivery_mode",
            "collected",
            "program_offering.delivery_mode",
            "On campus, online or a mix. Please answer rather than leaving it to us: "
            "together with study mode and duration this is what tells two otherwise "
            "identical offerings apart.",
            choices=("ON_CAMPUS", "ONLINE", "BLENDED", "DISTANCE"),
            width=16,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "campus_name",
            "collected",
            "campus.name_en",
            "Which campus, if the university runs this programme at more than one and "
            "the details differ.",
            width=28,
        ),
        TemplateColumn(
            "lifecycle_status",
            "collected",
            "program.lifecycle_status + program.lifecycle_field_status",
            "Whether it is currently on offer. Leave blank unless the official page "
            "actually says -- we must not assume a programme is open.",
            choices=("ACTIVE", "SUSPENDED", "WITHDRAWN", "NOT_OFFERED_THIS_CYCLE"),
            width=24,
        ),
        _source_ref_column(),
        _source_url_column(),
        TemplateColumn("notes", "collected", "program notes", "Anything unclear.", width=32),
    ),
)

ADMISSIONS = TemplateSheet(
    name="Admissions",
    purpose=(
        "Entry requirements. One row per requirement per programme. If a requirement "
        "differs by applicant group, use one row per group."
    ),
    columns=(
        _match_key_column(),
        _program_ref_column(owner=False),
        TemplateColumn(
            "requirement_kind",
            "collected",
            "admission_requirement.requirement_kind",
            "What the requirement is about.",
            choices=(
                "ACADEMIC_QUALIFICATION",
                "MINIMUM_GRADE",
                "WORK_EXPERIENCE",
                "PORTFOLIO",
                "INTERVIEW",
                "PREREQUISITE_SUBJECT",
                "OTHER",
            ),
            width=26,
            requirement="REQUIRED",
        ),
        *_scope_columns(),
        _official_text_column("admission_requirement.official_text"),
        _status_column("requirement_status", "admission_requirement.requirement_field_status"),
        _source_ref_column(),
        _source_url_column(),
        TemplateColumn("notes", "collected", "review notes", "Anything unclear.", width=32),
    ),
)

LANGUAGE_REQUIREMENTS = TemplateSheet(
    name="Language_Requirements",
    purpose=(
        "English-language requirements. One row per test per programme, and one row "
        "per applicant group where the university sets different minimums."
    ),
    columns=(
        _match_key_column(),
        _program_ref_column(owner=False),
        TemplateColumn(
            "test_type_code",
            "collected",
            "language_requirement.test_type_code",
            "Which test. Pick from the list.",
            vocabulary="test_type",
            width=18,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "overall_score",
            "collected",
            "language_requirement.overall_score",
            "Overall minimum, e.g. 6.5 or 92. Leave blank if the page lists the test "
            "but states no minimum -- and set the status to OFFICIALLY_NOT_PUBLISHED.",
            width=14,
        ),
        TemplateColumn(
            "subscore_minimums",
            "collected",
            "language_requirement.subscore_minimums",
            "Per-section minimums as written, e.g. 'writing 6.0, speaking 5.5'.",
            width=36,
        ),
        *_scope_columns(),
        _official_text_column("language_requirement.official_text"),
        _status_column("requirement_status", "language_requirement.requirement_field_status"),
        _source_ref_column(),
        _source_url_column(),
        TemplateColumn("notes", "collected", "review notes", "Anything unclear.", width=32),
    ),
)

TUITION = TemplateSheet(
    name="Tuition",
    purpose=(
        "Fees. One row per fee per programme per student category. If the fee differs "
        "by campus, add one row per campus and say which in notes."
    ),
    columns=(
        _match_key_column(),
        _program_ref_column(owner=False),
        TemplateColumn(
            "academic_year",
            "collected",
            "tuition.academic_year",
            "The year the fee applies to, as the page writes it: 2027/28 or 2027.",
            width=14,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "student_category_code",
            "collected",
            "tuition.student_category_code",
            "Which fee this is. The UK uses HOME and INTERNATIONAL; Hong Kong and "
            "Macau use LOCAL and NON_LOCAL.",
            vocabulary="student_category",
            width=22,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "amount_kind",
            "collected",
            "tuition.amount_kind",
            "What shape the page gives the fee in. EXACT for one figure; RANGE when "
            "it gives a low and a high; FROM for 'from GBP 24,500'; UP_TO for 'up to "
            "GBP 9,250'; VARIABLE when it says fees vary and gives no figure -- then "
            "paste their wording into official_text.",
            choices=TUITION_AMOUNT_KINDS,
            width=14,
            requirement="CONDITIONAL",
            requirement_note="whenever the status is PUBLISHED",
        ),
        TemplateColumn(
            "amount_min",
            "collected",
            "tuition.amount_min",
            "The figure, or the LOW end of a range. Numbers only -- no currency "
            "symbol and no thousands separator. For EXACT, put the same figure in "
            "amount_min and amount_max. Leave both blank for VARIABLE. Never write 0 "
            "to mean unknown.",
            width=14,
            requirement="CONDITIONAL",
            requirement_note="for EXACT, RANGE and FROM",
        ),
        TemplateColumn(
            "amount_max",
            "collected",
            "tuition.amount_max",
            "The HIGH end of a range, or the figure for UP_TO. Leave blank for FROM "
            "and VARIABLE. Do not work out an average of a range -- we keep both "
            "ends, because the average is a number no university published.",
            width=14,
            requirement="CONDITIONAL",
            requirement_note="for EXACT, RANGE and UP_TO",
        ),
        TemplateColumn(
            "currency_code",
            "collected",
            "tuition.currency_code",
            "Currency of the amount.",
            vocabulary="currency",
            width=14,
            requirement="CONDITIONAL",
            requirement_note="whenever amount_min or amount_max is filled in",
        ),
        TemplateColumn(
            "billing_unit_code",
            "collected",
            "tuition.billing_unit_code",
            "What the amount covers: per year, total for the programme, per credit.",
            vocabulary="billing_unit",
            width=18,
            requirement="CONDITIONAL",
            requirement_note="whenever amount_min or amount_max is filled in",
        ),
        *_scope_columns(),
        _official_text_column("tuition.official_text"),
        _status_column("amount_status", "tuition.amount_field_status"),
        _source_ref_column(),
        _source_url_column(),
        TemplateColumn("notes", "collected", "review notes", "Anything unclear.", width=32),
    ),
)

DEADLINES = TemplateSheet(
    name="Deadlines",
    purpose=(
        "Application deadlines. One row per deadline per intake. Read the date rules "
        "on the README sheet before filling this in."
    ),
    columns=(
        _match_key_column(),
        _program_ref_column(owner=False),
        TemplateColumn(
            "academic_year",
            "collected",
            "intake.academic_year",
            "e.g. 2027/28.",
            width=14,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "intake_season_code",
            "collected",
            "intake.intake_season_code",
            "Which intake this deadline belongs to.",
            vocabulary="intake_season",
            width=18,
            requirement="REQUIRED",
        ),
        TemplateColumn(
            "round_code",
            "collected",
            "application_round.round_code",
            "Which kind of round this is. Choose INSTITUTION_DEFINED when the "
            "university uses its own naming, and put their wording in round_label.",
            vocabulary="application_round_type",
            width=22,
            requirement="CONDITIONAL",
            requirement_note="whenever the status is PUBLISHED",
        ),
        TemplateColumn(
            "round_label",
            "collected",
            "application_round.round_label",
            "What the page calls this round: 'Round 2', 'Main round'. Copy their wording.",
            width=24,
            requirement="CONDITIONAL",
            requirement_note="when round_code is INSTITUTION_DEFINED",
        ),
        TemplateColumn(
            "deadline_kind",
            "collected",
            "application_deadline.deadline_kind",
            "FIXED_DATE with a date below. Otherwise ROLLING, UNTIL_FILLED, "
            "NO_FIXED_DEADLINE or NOT_CURRENTLY_ACCEPTING, when that is what the page "
            "says instead of giving a date.",
            choices=(
                "FIXED_DATE",
                "ROLLING",
                "UNTIL_FILLED",
                "NO_FIXED_DEADLINE",
                "NOT_CURRENTLY_ACCEPTING",
            ),
            width=26,
            requirement="CONDITIONAL",
            requirement_note="whenever the status is PUBLISHED",
        ),
        TemplateColumn(
            "deadline_text_verbatim",
            "collected",
            "application_deadline.deadline_text",
            "The deadline EXACTLY as the page writes it: '15 January 2027', 'mid "
            "March', 'late January, 23:59 GMT'. Write this even when you also fill in "
            "the separate parts below.",
            width=48,
            requirement="CONDITIONAL",
            requirement_note="whenever the status is PUBLISHED",
        ),
        TemplateColumn(
            "year",
            "collected",
            "application_deadline.deadline_year",
            "Year only, if the page gives one.",
            width=10,
        ),
        TemplateColumn(
            "month",
            "collected",
            "application_deadline.deadline_month",
            "Month as a number 1-12, ONLY if the page gives a month.",
            width=10,
        ),
        TemplateColumn(
            "day",
            "collected",
            "application_deadline.deadline_day",
            "Day 1-31, ONLY if the page gives an exact day. Leave blank for 'mid " "March'.",
            width=10,
        ),
        TemplateColumn(
            "month_part",
            "collected",
            "application_deadline.deadline_month_part",
            "EARLY, MID or LATE -- only when the page says 'early March' and gives no "
            "day. Do not turn this into a day yourself.",
            choices=("EARLY", "MID", "LATE"),
            width=14,
        ),
        TemplateColumn(
            "time_of_day",
            "collected",
            "application_deadline.deadline_time",
            "A clock time ONLY if the page states one, as HH:MM. Leave blank "
            "otherwise -- do not write 23:59 yourself.",
            width=14,
        ),
        TemplateColumn(
            "timezone",
            "collected",
            "application_deadline.deadline_timezone",
            "A timezone ONLY if the page states one: GMT, BST, HKT. Leave blank "
            "otherwise -- we must not invent one.",
            width=14,
        ),
        *_scope_columns(),
        _official_text_column("application_deadline.deadline_text"),
        _status_column("deadline_status", "application_deadline.deadline_field_status"),
        _source_ref_column(),
        _source_url_column(),
        TemplateColumn("notes", "collected", "review notes", "Anything unclear.", width=32),
    ),
)

TEMPLATE_SHEETS: tuple[TemplateSheet, ...] = (
    PILOT_UNIVERSITIES,
    OFFICIAL_SOURCES,
    PROGRAMS,
    ADMISSIONS,
    LANGUAGE_REQUIREMENTS,
    TUITION,
    DEADLINES,
)

#: Sheets whose facts are high-risk and therefore unpublishable without a source and
#: a reviewer (invariant I3). The README calls these out explicitly.
HIGH_RISK_SHEETS: frozenset[str] = frozenset(
    {ADMISSIONS.name, LANGUAGE_REQUIREMENTS.name, TUITION.name, DEADLINES.name}
)

#: Reference vocabularies loaded from the database for dropdowns, as
#: `vocabulary name -> (table, code column, label column, order by)`.
_VOCABULARY_QUERIES: dict[str, str] = {
    "degree_level": "SELECT code, name_en FROM degree_level ORDER BY sort_order",
    "intake_season": "SELECT code, name_en FROM intake_season ORDER BY sort_order",
    "currency": "SELECT code, name_en FROM currency ORDER BY code",
    "billing_unit": "SELECT code, name_en FROM billing_unit ORDER BY code",
    "test_type": "SELECT code, name_en FROM test_type ORDER BY code",
    "student_category": ("SELECT DISTINCT code, code FROM student_category ORDER BY code"),
    "applicant_scope": "SELECT code, name_en FROM applicant_scope ORDER BY code",
    "application_round_type": (
        "SELECT code, name_en FROM application_round_type ORDER BY sort_order, code"
    ),
    # Three top-level pilot subjects, seeded by revision 0018. Children are
    # deliberately absent: the real subject lives in `discipline_hint` until a
    # human maps it against the collected corpus.
    "discipline": "SELECT code, name_en FROM discipline WHERE parent_id IS NULL ORDER BY code",
}


#: Institutions to pre-fill, with the naming from the most recently imported list.
#:
#: The only interpolation is ``{where}``, assembled in `load_target_rows` from two
#: literal fragments; the caller's destination codes travel as a bound ``:codes``
#: parameter. Kept at module level so the interpolation is visible in one place.
_TARGET_ROWS_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (e.target_institution_id)
           e.target_institution_id, e.qs_name, e.qs_rank, e.region_label
      FROM target_list_entry e
      JOIN target_list l ON l.id = e.target_list_id
     ORDER BY e.target_institution_id, l.imported_at DESC, e.recorded_at DESC
)
SELECT ti.id, la.qs_name, la.qs_rank, la.region_label,
       ti.destination_code, ti.pilot_wave
  FROM target_institution ti
  LEFT JOIN latest la ON la.target_institution_id = ti.id
 {where}
 ORDER BY la.qs_rank NULLS LAST, la.qs_name
"""


@dataclass(frozen=True, slots=True)
class TargetRow:
    """One institution as the template shows it."""

    target_institution_id: uuid.UUID
    qs_name: str | None
    qs_rank: int | None
    region_label: str | None
    destination_code: str | None
    pilot_wave: int | None


def load_target_rows(
    connection: Connection,
    *,
    destination_codes: Sequence[str] | None = None,
    only_current: bool = True,
) -> list[TargetRow]:
    """The institutions to pre-fill, newest list version's naming, rank order.

    Ordered by QS rank so the client's own sense of the list is preserved, which
    makes the sheet easy to work down. Rank is presentation here and nothing else.
    """
    clauses = []
    params: dict[str, Any] = {}
    if only_current:
        clauses.append("ti.is_in_current_list")
    if destination_codes:
        clauses.append("ti.destination_code = ANY(:codes)")
        params["codes"] = list(destination_codes)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    sql = _TARGET_ROWS_SQL.format(where=where)
    rows = connection.execute(text(sql), params).all()

    return [
        TargetRow(
            target_institution_id=row[0],
            qs_name=row[1],
            qs_rank=row[2],
            region_label=row[3],
            destination_code=row[4],
            pilot_wave=row[5],
        )
        for row in rows
    ]


def _load_vocabularies(connection: Connection) -> dict[str, list[tuple[str, str]]]:
    loaded: dict[str, list[tuple[str, str]]] = {}
    for name, query in _VOCABULARY_QUERIES.items():
        loaded[name] = [(row[0], row[1]) for row in connection.execute(text(query)).all()]
    return loaded


def build_pilot_template(
    connection: Connection,
    path: str | Path,
    *,
    destination_codes: Sequence[str] | None = None,
    only_current: bool = True,
    pilot_target: int | None = None,
) -> Path:
    """Write the collection workbook. Returns the path written.

    `pilot_target` is written into the README as a reminder of how many institutions
    the client intends to select. It is a note to a human, not a constraint: the
    importer counts whatever the returned workbook marks.
    """
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    out_path = Path(path)
    targets = load_target_rows(
        connection, destination_codes=destination_codes, only_current=only_current
    )
    if not targets:
        raise ValueError(
            "no target institutions to export; import a target list first "
            "(make import-target-list f=...)"
        )
    vocabularies = _load_vocabularies(connection)

    header_font = Font(bold=True, size=10)
    reference_fill = PatternFill("solid", fgColor="E8E8E8")
    collected_fill = PatternFill("solid", fgColor="D9EAD3")
    high_risk_fill = PatternFill("solid", fgColor="FCE5CD")

    workbook = Workbook()
    readme = workbook.active
    assert readme is not None
    readme.title = "README"

    # Every dropdown's values live on one hidden sheet and are referenced by range.
    # An inline Excel list is capped at 255 characters, and `source_type` alone
    # exceeds that with its fifteen categories -- so an inline list would have left
    # the most important dropdown in the workbook silently absent.
    lists_sheet = workbook.create_sheet("_Lists")
    ranges = _write_lists_sheet(lists_sheet, vocabularies, column_letter=get_column_letter)
    lists_sheet.sheet_state = "hidden"

    _write_readme(readme, targets, vocabularies, pilot_target=pilot_target)

    for spec in TEMPLATE_SHEETS:
        sheet = workbook.create_sheet(spec.name)
        _write_header(
            sheet,
            spec,
            vocabularies,
            ranges,
            header_font=header_font,
            reference_fill=reference_fill,
            collected_fill=collected_fill,
            high_risk_fill=high_risk_fill,
            comment_cls=Comment,
            alignment_cls=Alignment,
            column_letter=get_column_letter,
            validation_cls=DataValidation,
        )
        if spec.one_row_per_institution:
            _prefill_institutions(sheet, spec, targets)

    workbook.save(out_path)
    logger.info(
        "pilot_template_exported",
        path=str(out_path),
        institutions=len(targets),
        sheets=[spec.name for spec in TEMPLATE_SHEETS],
    )
    return out_path


def choices_for(
    column: TemplateColumn, vocabularies: dict[str, list[tuple[str, str]]]
) -> tuple[str, ...]:
    """The accepted values for a column: literal choices or a loaded vocabulary."""
    if column.choices is not None:
        return column.choices
    if column.vocabulary:
        return tuple(code for code, _label in vocabularies.get(column.vocabulary, []))
    return ()


def _write_lists_sheet(
    sheet: Worksheet,
    vocabularies: dict[str, list[tuple[str, str]]],
    *,
    column_letter: Any,
) -> dict[tuple[str, ...], str]:
    """Write each distinct dropdown vocabulary to its own column; return the ranges.

    Keyed by the values themselves rather than by column name, so two columns
    offering the same vocabulary share one list instead of duplicating it.
    """
    ranges: dict[tuple[str, ...], str] = {}
    next_column = 1

    for spec in TEMPLATE_SHEETS:
        for column in spec.columns:
            values = choices_for(column, vocabularies)
            if not values:
                continue
            key = tuple(values)
            if key in ranges:
                continue
            letter = column_letter(next_column)
            sheet.cell(row=1, column=next_column, value=column.name)
            for offset, value in enumerate(values, start=2):
                sheet.cell(row=offset, column=next_column, value=value)
            # Absolute, sheet-qualified and quoted: the sheet name starts with an
            # underscore, which Excel requires quoting for in a formula.
            ranges[key] = f"'_Lists'!${letter}$2:${letter}${len(values) + 1}"
            next_column += 1

    return ranges


def _write_header(
    sheet: Worksheet,
    spec: TemplateSheet,
    vocabularies: dict[str, list[tuple[str, str]]],
    ranges: dict[tuple[str, ...], str],
    *,
    header_font: Any,
    reference_fill: Any,
    collected_fill: Any,
    high_risk_fill: Any,
    comment_cls: Any,
    alignment_cls: Any,
    column_letter: Any,
    validation_cls: Any,
) -> None:
    """Header row, colours, per-column help comments and dropdowns."""
    sheet.append([column.name for column in spec.columns])

    for index, column in enumerate(spec.columns, start=1):
        letter = column_letter(index)
        cell = sheet.cell(row=1, column=index)
        cell.font = header_font
        cell.alignment = alignment_cls(vertical="center", wrap_text=False)
        if column.kind == "reference":
            cell.fill = reference_fill
        elif column.name in {"source_url", "official_text"} and spec.name in HIGH_RISK_SHEETS:
            cell.fill = high_risk_fill
        else:
            cell.fill = collected_fill
        cell.comment = comment_cls(column.help_text, "datahub")
        sheet.column_dimensions[letter].width = column.width

        choices = choices_for(column, vocabularies)
        if choices:
            # Referenced from the hidden _Lists sheet, so the list length is not
            # capped and every vocabulary gets a real dropdown. `allow_blank` is
            # essential: a blank cell is a meaningful answer here (NOT_CHECKED), so
            # the validation must never force a value.
            source = ranges.get(tuple(choices))
            if source is None:  # pragma: no cover - ranges cover every spec column
                raise RuntimeError(f"no list range for {spec.name}.{column.name}")
            validation = validation_cls(type="list", formula1=source, allow_blank=True)
            validation.errorTitle = "Not an accepted value"
            validation.error = f"Choose one of: {', '.join(choices)}"[:250]
            validation.promptTitle = column.name
            validation.prompt = column.help_text[:200]
            sheet.add_data_validation(validation)
            validation.add(f"{letter}2:{letter}2000")

    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = f"A1:{column_letter(len(spec.columns))}1"


def _prefill_institutions(
    sheet: Worksheet, spec: TemplateSheet, targets: Sequence[TargetRow]
) -> None:
    """One pre-filled row per institution, reference columns populated."""
    for target in targets:
        row: list[object] = []
        for column in spec.columns:
            if column.name == MATCH_KEY_COLUMN:
                row.append(str(target.target_institution_id))
            elif column.name == "qs_name":
                row.append(target.qs_name)
            elif column.name == "qs_rank":
                row.append(target.qs_rank)
            elif column.name == "region":
                row.append(target.region_label)
            elif column.name == "destination_code":
                row.append(target.destination_code)
            elif column.name == "selected":
                # Pre-filled only where the client has already assigned a wave, so a
                # re-export never silently un-selects earlier work.
                row.append("YES" if target.pilot_wave is not None else None)
            else:
                row.append(None)
        sheet.append(row)


def _write_readme(
    sheet: Worksheet,
    targets: Sequence[TargetRow],
    vocabularies: dict[str, list[tuple[str, str]]],
    *,
    pilot_target: int | None,
) -> None:
    """Instructions, written for the person filling the workbook in.

    Short imperative sentences, and every rule stated as what to DO rather than what
    the system does. The mandatory-column table is generated from the sheet specs, so
    it cannot drift from the file it describes.

    Every example here uses invented institutions. Real-looking sample rows in a
    collection sheet get returned as if they were collected, so they live here only.
    """
    from openpyxl.styles import Font

    lines: list[tuple[str, bool]] = [
        ("Pilot data collection workbook", True),
        ("", False),
        (f"This workbook contains {len(targets)} candidate institutions.", False),
    ]
    if pilot_target:
        lines.append(
            (
                f"Please mark exactly {pilot_target} of them as selected = YES. "
                f"We will check the count when you return the file; if it is not "
                f"{pilot_target} we will come back to you rather than choose for you.",
                False,
            )
        )
    lines += [
        ("", False),
        ("THE FIVE RULES", True),
        (
            "1. Use only official sources: the university's own website, an official "
            "PDF, or a government or regulator page. Never a rankings site, an agent "
            "site, a forum, Wikipedia, or the reference columns in this workbook.",
            False,
        ),
        ("2. Do not guess. If you do not know, leave it blank.", False),
        ("3. Copy official wording exactly. Do not summarise, translate or tidy it.", False),
        ("4. Never edit column A. It is how we match your rows to our records.", False),
        (
            "5. Tell us when something is unclear, in the notes column. We would much "
            "rather answer a question than receive a guess.",
            False,
        ),
        ("", False),
        ("HOW TO MARK THE PILOT UNIVERSITIES", True),
        (
            "On the Pilot_Universities sheet, put YES in the `selected` column for each "
            "institution in the pilot. Leave the rest blank, or write UNDECIDED.",
            False,
        ),
        (
            "Nothing is pre-selected. The order of the rows means nothing -- it is just "
            "the order of the supplied list -- and the rank column does not decide "
            "anything.",
            False,
        ),
        (
            "An institution you do not select is not lost. It stays on our list as a "
            "future candidate.",
            False,
        ),
        (
            "For each institution you mark YES, please also fill in official_name_en and "
            "official_homepage.",
            False,
        ),
        ("", False),
        ("PROGRAMME CODES (program_ref)", True),
        (
            "On the Programs sheet, give every programme a short code in the "
            "`program_ref` column: P0001, P0002, P0003 and so on.",
            False,
        ),
        (
            "On the Admissions, Language_Requirements, Tuition and Deadlines sheets, put "
            "that same code in the `program_ref` column instead of typing the programme "
            "name again.",
            False,
        ),
        (
            "Example: if Programs row 2 is P0001 = 'MSc Computer Science', then a fee for "
            "that programme goes on the Tuition sheet with program_ref = P0001.",
            False,
        ),
        (
            "Use each code once on the Programs sheet. If you copy a row, remember to "
            "change its code.",
            False,
        ),
        (
            "Leave program_ref blank only when the row applies to the whole university "
            "rather than one programme -- for example an institution-wide English "
            "requirement.",
            False,
        ),
        ("", False),
        ("SOURCE CODES (source_ref)", True),
        (
            "Every official page you use goes on the Official_Sources sheet once, with a "
            "code in the `source_ref` column: S0001, S0002, S0003 and so on.",
            False,
        ),
        (
            "On the fact sheets, put that code in the `source_ref` column. That is how we "
            "know which page a fact came from.",
            False,
        ),
        (
            "Example: S0001 = the university's tuition-fees page. Every fee you take from "
            "that page gets source_ref = S0001.",
            False,
        ),
        (
            "There is also an optional `source_url` column on the fact sheets. It is only "
            "so you can see at a glance where a row came from. We match on source_ref, "
            "never on the address, so please fill in source_ref.",
            False,
        ),
        ("", False),
        ("BLANK IS NOT THE SAME AS 'NOT PUBLISHED'", True),
        ("This is the most important distinction in the whole workbook.", False),
        (
            "Leave a cell BLANK when nobody has looked yet. That is a perfectly good "
            "answer and nothing is misrepresented.",
            False,
        ),
        (
            "When you DID look and the university publishes nothing, that is different "
            "and much more useful. Set the row's status column to "
            "OFFICIALLY_NOT_PUBLISHED, leave the value blank, and still give the "
            "source_ref for the page you checked.",
            False,
        ),
        (
            "Set the status to PUBLISHED only when you have filled in the value and "
            "pasted the official wording.",
            False,
        ),
        (
            "Please do not write 0, N/A, TBC, - or 'unknown' to mean 'no information'. A "
            "fee of 0 is a published fact and we cannot tell the difference afterwards.",
            False,
        ),
        ("", False),
        ("WHEN A RULE APPLIES ONLY TO SOME APPLICANTS", True),
        (
            "Many universities set one requirement for everyone and a different one for "
            "particular applicants. Please do not flatten that.",
            False,
        ),
        (
            "If the page states one rule for everyone, write UNIVERSAL in "
            "`applicant_scope_hint`.",
            False,
        ),
        (
            "If the rule is for a particular group, use one row per group and copy the "
            "university's own words into `applicant_scope_hint` -- for example 'Mainland "
            "China', 'international students', 'holders of a Chinese bachelor degree', "
            "'985/211 institutions'.",
            False,
        ),
        (
            "Add `applicant_country_code` when the rule is about one country (CN, HK, TW, "
            "MO, GB, US), and `qualification_hint` when it names a qualification "
            "('Chinese bachelor degree', '985/211', 'IB Diploma', 'A-level', 'Gaokao').",
            False,
        ),
        (
            "If you are not sure who a rule applies to, leave applicant_scope_hint blank "
            "and say so in notes. Do NOT write UNIVERSAL to fill the gap -- we would "
            "publish the rule as applying to everybody, which would be wrong.",
            False,
        ),
        ("", False),
        ("DATES: WRITE WHAT THE PAGE SAYS, AND NOTHING MORE", True),
        (
            "Always fill in deadline_text_verbatim with the page's exact wording. Then "
            "fill in only the parts the page actually gives:",
            False,
        ),
        ("   '15 January 2027'          -> year 2027, month 1, day 15", False),
        ("   'mid March 2027'           -> year 2027, month 3, month_part MID, day blank", False),
        ("   'January 2027'             -> year 2027, month 1, day blank", False),
        ("   '15 Jan 2027, 23:59 GMT'   -> also time_of_day 23:59 and timezone GMT", False),
        (
            "Never add a time or a timezone the page does not state. '15 January' does "
            "not mean '15 January at 23:59 local time' unless the page says so.",
            False,
        ),
        (
            "If the page says there is no fixed deadline, that is a published fact: set "
            "deadline_kind to NO_FIXED_DEADLINE and the status to PUBLISHED. That is not "
            "the same as the page saying nothing about deadlines.",
            False,
        ),
        ("", False),
        ("ADD AS MANY ROWS AS YOU NEED", True),
        (
            "Only Pilot_Universities is one row per institution, and it is already "
            "filled in. Every other sheet takes as many rows as the facts require.",
            False,
        ),
        (
            "One programme with two fees (home and international) is two rows on Tuition. "
            "One programme with two entry routes is two rows on Admissions.",
            False,
        ),
        ("", False),
        ("WHICH COLUMNS ARE MANDATORY", True),
        (
            "Required = please always fill this in. Conditional = required only in the "
            "case described. Everything else is optional.",
            False,
        ),
    ]

    for spec in TEMPLATE_SHEETS:
        required = [c for c in spec.columns if c.requirement == "REQUIRED"]
        conditional = [c for c in spec.columns if c.requirement == "CONDITIONAL"]
        if not required and not conditional:
            continue
        lines.append((f"  {spec.name}", True))
        if required:
            lines.append((f"    Required: {', '.join(c.name for c in required)}", False))
        for column in conditional:
            lines.append((f"    {column.name}: required {column.requirement_note}", False))

    lines += [
        ("", False),
        ("ACCEPTED VALUES", True),
        (
            "Columns with a fixed list have a dropdown. The lists are repeated here so "
            "you can read them without clicking, and so this file still explains itself "
            "if the dropdowns are lost by another editor.",
            False,
        ),
    ]

    for spec in TEMPLATE_SHEETS:
        listed = [
            (column.name, choices_for(column, vocabularies))
            for column in spec.columns
            if choices_for(column, vocabularies)
        ]
        if not listed:
            continue
        lines.append((f"  {spec.name}", True))
        for column_name, values in listed:
            lines.append((f"    {column_name}: {', '.join(values)}", False))

    lines += [
        ("", False),
        ("A NOTE ON SUBJECTS", True),
        (
            "`discipline_code` has only three values, because those are the pilot "
            "subjects. `discipline_hint` is where the real subject goes, in the "
            "university's own words -- 'Artificial Intelligence', 'Business Analytics', "
            "'Mechanical Engineering'. We keep your wording exactly and map it later.",
            False,
        ),
        ("", False),
        ("IF SOMETHING DOES NOT FIT", True),
        (
            "Write it in the notes column of the nearest row and carry on. A note we can "
            "read is worth far more than a value that looks tidy and is wrong.",
            False,
        ),
        (
            "If two institutions could match one row, or a university has merged or "
            "renamed itself, please say so rather than picking one.",
            False,
        ),
    ]

    for text_value, is_heading in lines:
        sheet.append([text_value])
        if is_heading:
            sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True, size=11)
    sheet.column_dimensions["A"].width = 100


__all__ = [
    "HIGH_RISK_SHEETS",
    "MATCH_KEY_COLUMN",
    "PILOT_SELECTION_VALUES",
    "PROGRAM_REF_PATTERN",
    "SOURCE_REF_PATTERN",
    "TEMPLATE_FIELD_STATUSES",
    "TEMPLATE_SHEETS",
    "TargetRow",
    "TemplateColumn",
    "TemplateSheet",
    "build_pilot_template",
    "load_target_rows",
]
