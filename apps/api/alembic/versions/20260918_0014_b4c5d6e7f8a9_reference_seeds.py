"""Domain group 12: reference vocabulary seeds

**Vocabulary only.** No university, program, fee or deadline is seeded — not even a
plausible-looking example. Every such fact must arrive through
source → claim → proposal → review → publish, and a seeded one would be a published
fact with no provenance, which is precisely what this platform exists to prevent.
Test fixtures create fictional institutions inside the test suite instead.

Seeded here: destinations (the pilot three plus the roadmap waves, so a scope change
is configuration), degree levels, intake seasons, currencies, billing units, language
test types with their subscore vocabularies, destination-scoped student categories
(B5), applicant-scope dimensions (B1), the universal applicant scope, application
round types (C5), and the RBAC roles, permissions and grants.

Every insert is ``ON CONFLICT DO NOTHING``, so re-running on a partially seeded
database is harmless.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DESTINATIONS = [
    ("GB", "United Kingdom", "英国", True, 1),
    ("HK", "Hong Kong SAR", "香港", True, 1),
    ("MO", "Macao SAR", "澳门", True, 1),
    ("AU", "Australia", "澳大利亚", False, 2),
    ("SG", "Singapore", "新加坡", False, 2),
    ("NZ", "New Zealand", "新西兰", False, 2),
    ("IE", "Ireland", "爱尔兰", False, 2),
    ("US", "United States", "美国", False, 3),
    ("CA", "Canada", "加拿大", False, 3),
    ("MY", "Malaysia", "马来西亚", False, 4),
]

# Doctorate is present but out of scope: the pilot covers bachelor and master, and
# `is_in_scope` lets that change without a migration.
DEGREE_LEVELS = [
    ("BACHELOR", "Bachelor", "本科", 10, True),
    ("MASTER", "Master", "硕士", 20, True),
    ("DOCTORATE", "Doctorate", "博士", 30, False),
]

INTAKE_SEASONS = [
    ("SEPTEMBER", "September", "九月", 9, 10),
    ("JANUARY", "January", "一月", 1, 20),
    ("FEBRUARY", "February", "二月", 2, 25),
    ("APRIL", "April", "四月", 4, 30),
    ("JULY", "July", "七月", 7, 40),
]

CURRENCIES = [
    ("GBP", "Pound Sterling", 2),
    ("HKD", "Hong Kong Dollar", 2),
    ("MOP", "Macanese Pataca", 2),
    ("USD", "US Dollar", 2),
    ("EUR", "Euro", 2),
    ("CNY", "Chinese Yuan", 2),
    ("AUD", "Australian Dollar", 2),
    ("SGD", "Singapore Dollar", 2),
    ("NZD", "New Zealand Dollar", 2),
    ("CAD", "Canadian Dollar", 2),
]

BILLING_UNITS = [
    ("PER_YEAR", "Per academic year", "每学年"),
    ("TOTAL_PROGRAM", "Total for the programme", "全程总额"),
    ("PER_CREDIT", "Per credit", "每学分"),
    ("PER_MODULE", "Per module", "每模块"),
    ("PER_SEMESTER", "Per semester", "每学期"),
]

TEST_TYPES = [
    (
        "IELTS",
        "IELTS Academic",
        "雅思学术类",
        '["listening", "reading", "writing", "speaking"]',
        9.0,
    ),
    (
        "TOEFL_IBT",
        "TOEFL iBT",
        "托福网考",
        '["listening", "reading", "writing", "speaking"]',
        120.0,
    ),
    (
        "PTE_ACADEMIC",
        "PTE Academic",
        "培生学术英语",
        '["listening", "reading", "writing", "speaking"]',
        90.0,
    ),
    (
        "DUOLINGO",
        "Duolingo English Test",
        "多邻国英语测试",
        '["literacy", "comprehension", "conversation", "production"]',
        160.0,
    ),
    ("CET", "College English Test", "大学英语考试", "null", 710.0),
]

# Destination-scoped, because UK and Hong Kong genuinely use different vocabularies
# and forcing one on both would misrepresent both (B5).
STUDENT_CATEGORIES = [
    ("HOME", "GB", "Home", "本地学生"),
    ("INTERNATIONAL", "GB", "International", "国际学生"),
    ("LOCAL", "HK", "Local", "本地学生"),
    ("NON_LOCAL", "HK", "Non-local", "非本地学生"),
    ("LOCAL", "MO", "Local", "本地学生"),
    ("NON_LOCAL", "MO", "Non-local", "非本地学生"),
]

SCOPE_DIMENSIONS = [
    (
        "applicant_country",
        "Applicant country or region",
        "申请人国家或地区",
        "Country or region of the applicant's citizenship or residence",
        False,
    ),
    (
        "qualification_country",
        "Country of prior qualification",
        "前置学历国家",
        "Where the applicant's qualifying award was issued",
        False,
    ),
    (
        "qualification_type",
        "Type of prior qualification",
        "前置学历类型",
        "Bachelor degree, diploma, foundation year, and so on",
        False,
    ),
    (
        "qualification_group",
        "Named qualification or institution group",
        "指定院校或学历名单",
        "Points at a source-published list. This is how an institution's own tier list "
        "is represented: as data, never as a rule in the schema",
        True,
    ),
    (
        "residency_status",
        "Residency status",
        "居留身份",
        "Residency or fee status as the institution defines it",
        False,
    ),
]

ROUND_TYPES = [
    ("ROUND_1", "Round 1", "第一轮", 10, False),
    ("ROUND_2", "Round 2", "第二轮", 20, False),
    ("ROUND_3", "Round 3", "第三轮", 30, False),
    ("PRIORITY", "Priority round", "优先轮", 5, False),
    ("EARLY_ACTION", "Early action", "提前批（非绑定）", 3, False),
    ("EARLY_DECISION", "Early decision", "提前批（绑定）", 4, False),
    ("MAIN", "Main round", "主轮", 15, False),
    ("ROLLING", "Rolling admission", "滚动录取", 50, False),
    ("CLEARING", "Clearing", "补录", 60, False),
    ("LATE", "Late round", "后期轮", 55, False),
    # The escape hatch for wording that fits no controlled code. Rounds using it are
    # identified by their normalised institution label instead (C10).
    ("INSTITUTION_DEFINED", "Institution-defined round", "院校自定义轮次", 90, True),
]

ROLES = [
    (
        "consultant_readonly",
        "Consultant (read only)",
        "Search, filter, compare, view sources and history. No write access.",
        False,
    ),
    (
        "data_editor",
        "Data editor",
        "Registers sources, creates drafts, submits changes, handles returns. Cannot "
        "review own changes or publish.",
        False,
    ),
    (
        "reviewer",
        "Reviewer",
        "Reviews, returns, corrects and publishes. Cannot approve own edits, nor own "
        "high-risk corrections (D7).",
        True,
    ),
    (
        "ops",
        "Data operations",
        "Health dashboard, alert triage and assignment, refetch. No field edits, no " "publishing.",
        False,
    ),
    (
        "admin",
        "Administrator",
        "Roles, source rules, task rules, feature gates, vocabularies. Cannot bypass "
        "audit or delete published history.",
        False,
    ),
    (
        "service_client",
        "Service client",
        "Machine reader of published data through the internal API, within scopes.",
        False,
    ),
]

PERMISSIONS = [
    ("catalog:read", "Read published catalog data"),
    ("catalog:compare", "Use the side-by-side comparison view"),
    ("provenance:read", "View field provenance, snapshots and version history"),
    ("proposal:create", "Create a change proposal"),
    ("proposal:review", "Review a proposal: approve, return or correct"),
    ("proposal:publish", "Execute the publication transaction"),
    ("source:manage", "Register and configure sources and field bindings"),
    ("source:refetch", "Trigger a scoped re-fetch of a single source"),
    ("alert:manage", "Triage, assign and close operational alerts"),
    ("audit:read", "Read the audit log"),
    ("admin:roles", "Manage roles and role assignments"),
    ("admin:vocabulary", "Extend controlled vocabularies (N1)"),
    ("admin:feature_flags", "Change feature gates, including the ranking gate (D8)"),
    ("api:read_published", "Read published data through the internal API"),
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "consultant_readonly": ["catalog:read", "catalog:compare", "provenance:read"],
    "data_editor": [
        "catalog:read",
        "catalog:compare",
        "provenance:read",
        "proposal:create",
        "source:manage",
        "source:refetch",
    ],
    "reviewer": [
        "catalog:read",
        "catalog:compare",
        "provenance:read",
        "proposal:create",
        "proposal:review",
        "proposal:publish",
        "audit:read",
    ],
    "ops": ["catalog:read", "provenance:read", "alert:manage", "source:refetch", "audit:read"],
    "admin": [
        "catalog:read",
        "catalog:compare",
        "provenance:read",
        "audit:read",
        "source:manage",
        "admin:roles",
        "admin:vocabulary",
        "admin:feature_flags",
    ],
    "service_client": ["api:read_published"],
}


def _insert(table: str, columns: str, rows: list[tuple[Any, ...]], conflict: str) -> None:
    """Insert seed rows idempotently."""
    if not rows:
        return
    placeholders = ", ".join(
        "(" + ", ".join(f":p{r}_{c}" for c in range(len(row))) + ")" for r, row in enumerate(rows)
    )
    params = {f"p{r}_{c}": value for r, row in enumerate(rows) for c, value in enumerate(row)}
    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {table} ({columns}) VALUES {placeholders} "
            f"ON CONFLICT {conflict} DO NOTHING"
        ),
        params,
    )


def upgrade() -> None:
    _insert("destination", "code, name_en, name_zh, is_pilot, rollout_wave", DESTINATIONS, "(code)")
    _insert(
        "degree_level", "code, name_en, name_zh, sort_order, is_in_scope", DEGREE_LEVELS, "(code)"
    )
    _insert(
        "intake_season",
        "code, name_en, name_zh, typical_start_month, sort_order",
        INTAKE_SEASONS,
        "(code)",
    )
    _insert("currency", "code, name_en, minor_units", CURRENCIES, "(code)")
    _insert("billing_unit", "code, name_en, name_zh", BILLING_UNITS, "(code)")

    # subscore_keys is JSONB, so the seeded text needs an explicit cast.
    for code, name_en, name_zh, subscores, max_overall in TEST_TYPES:
        op.get_bind().execute(
            sa.text(
                "INSERT INTO test_type (code, name_en, name_zh, subscore_keys, max_overall) "
                "VALUES (:code, :name_en, :name_zh, CAST(:subscores AS jsonb), :max_overall) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {
                "code": code,
                "name_en": name_en,
                "name_zh": name_zh,
                "subscores": subscores,
                "max_overall": max_overall,
            },
        )

    _insert(
        "student_category",
        "code, destination_code, name_en, name_zh",
        STUDENT_CATEGORIES,
        "(destination_code, code)",
    )
    _insert(
        "scope_dimension",
        "code, name_en, name_zh, description, expects_group_ref",
        SCOPE_DIMENSIONS,
        "(code)",
    )
    _insert(
        "application_round_type",
        "code, name_en, name_zh, sort_order, is_institution_defined",
        ROUND_TYPES,
        "(code)",
    )

    # The universal scope: zero criteria, so it matches every applicant. Requirements
    # and deadlines that apply to everyone point here rather than leaving the scope
    # nullable, which keeps their natural keys total.
    op.get_bind().execute(
        sa.text(
            "INSERT INTO applicant_scope (code, name_en, name_zh, is_universal, precedence, "
            "notes) VALUES ('UNIVERSAL', 'All applicants', '所有申请人', true, 0, "
            "'Zero criteria: matches every applicant. Lowest precedence, so any more "
            "specific scope wins (U1).') ON CONFLICT (code) DO NOTHING"
        )
    )

    _insert("role", "code, name_en, description, is_reviewer_role", ROLES, "(code)")
    _insert("permission", "code, description", PERMISSIONS, "(code)")
    _insert(
        "role_permission",
        "role_code, permission_code",
        [
            (role, permission)
            for role, permissions in ROLE_PERMISSIONS.items()
            for permission in permissions
        ],
        "(role_code, permission_code)",
    )


def downgrade() -> None:
    # Reverse dependency order. Reference data that a published fact depends on will
    # refuse to delete (RESTRICT), which is the correct outcome: a currency cannot be
    # un-seeded while a fee is denominated in it.
    bind = op.get_bind()
    for statement in (
        "DELETE FROM role_permission",
        "DELETE FROM permission",
        "DELETE FROM role",
        "DELETE FROM applicant_scope WHERE code = 'UNIVERSAL'",
        "DELETE FROM application_round_type",
        "DELETE FROM scope_dimension",
        "DELETE FROM student_category",
        "DELETE FROM test_type",
        "DELETE FROM billing_unit",
        "DELETE FROM currency",
        "DELETE FROM intake_season",
        "DELETE FROM degree_level",
        "DELETE FROM destination",
    ):
        bind.execute(sa.text(statement))
