"""Agreement, conflict and context grouping over candidate claims (sections 5-8, 16-21).

TWO KEYS, NOT ONE
=================
Grouping needs to answer two different questions, and one key cannot answer both:

* **context key** -- *what is this claim about?* The institution, the field, the
  applicant scope, the programme, the period. Claims that share a context key are
  talking about the same thing.
* **value key** -- *what does it say?* The normalised value, reduced to the components
  that matter for that field.

Group by context; compare by value. That falls out into exactly the classes section 6
asks for, and it makes section 5's prohibition -- *"Do NOT group solely by normalized
value"* -- structural rather than a rule somebody has to remember:

* one context, one value, several sources     -> AGREES
* one context, one value, one source          -> POSSIBLE_DUPLICATE
* one context, several values                 -> CONFLICTS
* a context missing a component the field needs -> INSUFFICIENT_CONTEXT
* a field that carries nothing comparable at all -> NOT_COMPARABLE

Section 6's three worked examples come out of this without special cases. *"Same
field/value but one institution-wide and one program-specific"* have different context
keys, so they never meet. *"Same value, different academic years"* likewise. And two
pages stating IELTS 7.0 for the same institution share both keys, so they agree.

NOTHING IS STORED
=================
These groups are a pure function of the current candidates. A stored group would be a
cache with no invalidation: one new candidate, one corrected rule or one review decision
changes the answer and nothing would update the row. `field_conflict` cannot be reused
for the same reason it cannot hold anything here -- it is keyed on a canonical entity,
and there are none.

NO WINNER IS EVER CHOSEN
========================
Section 7. A `CONFLICTS` group names every competing candidate and every source. It does
not rank them, and it does not prefer the higher confidence band: confidence describes
extraction quality, not truth (section 4). Deciding is a reviewer's job and this step
does not do it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.domains.claims.model import FieldKind


class Agreement(StrEnum):
    """What a group of candidates sharing one context says collectively."""

    AGREES = "AGREES"
    """Two or more sources state the same value.

    `context_is_thin` says whether the context they share was fully stated. Agreement
    under an unknown context is still corroboration -- every member shares the same
    unknowns, so a sentinel is not evidence that they are about different things -- and
    section 20 is explicit that agreement verifies nothing and must never auto-accept.
    A *contradiction* under an unknown context is a different matter: see `CONFLICTS`."""

    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    """The same value, but from one source. Corroboration this is not."""

    CONFLICTS = "CONFLICTS"
    """One **fully stated** context, more than one value. No winner is chosen here.

    Fully stated, unlike `AGREES`, and the asymmetry is deliberate: a conflict asserts
    that two sources contradict each other, which requires the question to be the same.
    A key of unknowns has not established that -- Caltech's admit-reply date and its
    application deadline are not rival answers. Those become
    `INSUFFICIENT_CONTEXT` with `unconfirmed_disagreement` set."""

    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"
    """The context is too thin to compare safely -- see `REQUIRED_CONTEXT`.

    A group in this state may still hold several values. That is reported, as
    `unconfirmed_disagreement`, rather than dropped: "these two might contradict each
    other and we cannot tell" is a finding, and the safer failure is still a failure."""

    SINGLE = "SINGLE"
    """One candidate, adequate context, nothing to compare it with."""

    NOT_COMPARABLE = "NOT_COMPARABLE"
    """This *kind* of field carries nothing that could make two claims the same claim.

    WHY THIS IS NOT `INSUFFICIENT_CONTEXT`
    ======================================
    The two look alike -- neither can be keyed -- and conflating them cost the console a
    blocker no reviewer could clear. `INSUFFICIENT_CONTEXT` means *this row* did not state
    something the field needs, so a better page or a re-extraction would key it, and a
    human looking at the group has a real question to answer. `NOT_COMPARABLE` means no
    row of this field kind will ever key: a requirement's value is prose and a calendar
    event's identity is a label the evidence does not isolate (see `NEVER_GROUPED`).

    Asking a reviewer to "resolve the conflict" on prose is ceremony, not governance: there
    is no rival value and no question, so the only available answer is a rubber stamp --
    and a queue of rubber stamps is how a reviewer learns to stop reading. So this verdict
    is deliberately absent from `resolution.UNRESOLVED_VERDICTS` and blocks nothing. It
    still appears in the group listing, because "these were never compared" is a fact a
    reviewer should be able to see rather than infer from an absence.

    It weakens no gate. A prose requirement still needs an ACCEPTED review, an eligible
    source and a resolved applicant scope before it can become a `field_claim`."""


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """The parts of a candidate that grouping needs. Deliberately not the whole row."""

    candidate_id: uuid.UUID
    institution: str
    field_kind: str
    value: dict[str, Any] | None
    unresolved_reason: str | None
    confidence_band: str
    source_id: uuid.UUID
    url: str
    extraction_id: uuid.UUID
    locator: dict[str, Any]
    responsibility: str
    extractor_name: str
    extractor_version: str
    evidence_text: str = ""
    value_raw_text: str = ""

    @property
    def context(self) -> dict[str, Any]:
        """The rule-supplied context the extractor stored under `_context`."""
        if not self.value:
            return {}
        inner = self.value.get("_context")
        return inner if isinstance(inner, dict) else {}


# ===========================================================================
# Context keys
# ===========================================================================

#: For each field kind, the context components that MUST be present before two
#: candidates may be compared at all. A missing one means `INSUFFICIENT_CONTEXT`, not a
#: match on the rest: comparing a fee whose student category is unknown against one
#: whose category is "international" is how a home fee becomes an international fee.
REQUIRED_CONTEXT: dict[str, tuple[str, ...]] = {
    # Currency, billing unit AND student category. Berkeley's cost-of-attendance page
    # alone carries ten USD amounts with none of the last two, and comparing them
    # reports a ten-way "conflict" between a mini-fridge allowance and a semester's
    # tuition. 28 of 40 tuition candidates fail this, and that is the right answer.
    FieldKind.TUITION.value: ("currency", "billing_unit", "student_category"),
    FieldKind.LANGUAGE_OVERALL_SCORE.value: ("test",),
    FieldKind.LANGUAGE_COMPONENT_SCORE.value: ("test", "applies_to"),
    FieldKind.LANGUAGE_TEST.value: ("test",),
    FieldKind.APPLICATION_DEADLINE.value: ("deadline_kind",),
    # A calendar event has no identity to group on. What distinguishes "registration
    # opens" from "midterm grades due" on the same date is the event's own label, and
    # the stored `event_text` is a 160-character window around the date that routinely
    # spans two or three adjacent entries. Grouping by (institution, year) put eight of
    # Cornell's 2027 events in one group and reported them as eight competing values.
    # So: never grouped for agreement, which costs nothing -- section 19 keeps calendar
    # events out of the admission facts anyway.
    FieldKind.ACADEMIC_CALENDAR_EVENT.value: ("__never__",),
    FieldKind.PROGRAM_NAME.value: ("program_name",),
    FieldKind.DEGREE_LEVEL.value: ("degree_level",),
    FieldKind.DISCIPLINE_HINT.value: ("discipline",),
    # A requirement's "value" is prose, so there is no component that makes two of them
    # safely comparable. Every one is INSUFFICIENT_CONTEXT for agreement purposes, and
    # that is the honest answer rather than a weak one -- see `context_key_for`.
    FieldKind.ADMISSION_REQUIREMENT.value: ("__never__",),
}

#: `__never__` is not "we have not got round to it". It marks a field whose claims carry
#: nothing that could make two of them the same claim: a requirement's value is prose, and
#: a calendar event's identity is a label the stored evidence does not isolate. Inventing
#: a key for either would produce agreement and conflict numbers that mean nothing.
NEVER_GROUPED: frozenset[str] = frozenset(
    kind for kind, required in REQUIRED_CONTEXT.items() if "__never__" in required
)

#: Key components that say "we do not know", as opposed to naming something. A key
#: containing one of these cannot support AGREES: section 6 asks for the *same explicit
#: scope*, and two candidates with no explicit scope do not have one.
SENTINELS: frozenset[str] = frozenset(
    {"UNRESOLVED", "PROGRAM_UNKNOWN", "PERIOD_NOT_STATED", "ROUND_NOT_STATED"}
)


def has_sentinel(key: tuple[str, ...]) -> bool:
    return any(part.split("=", 1)[-1] in SENTINELS for part in key)


def _scope_key(value: dict[str, Any] | None) -> str:
    """The applicant scope, as a stable string.

    `UNRESOLVED` is a distinct key, not a wildcard. Two requirements whose scope nobody
    has mapped are not thereby about the same applicants, and treating them as a match
    would be the `UNIVERSAL`-from-silence inference section 12 forbids, arrived at
    sideways.
    """
    if not value:
        return "UNRESOLVED"
    scopes = value.get("applicant_scopes")
    if not isinstance(scopes, list) or not scopes:
        return "UNRESOLVED"
    labels = sorted(
        str(entry.get("scope_label") or entry.get("scope_raw"))
        for entry in scopes
        if isinstance(entry, dict)
    )
    return "|".join(labels) or "UNRESOLVED"


def _period_key(row: CandidateRow) -> str:
    """The academic period a claim is about, where the page stated one.

    Section 5 requires academic year / intake in the key where applicable, and section 6
    requires the same value in different years to stay separate. Only what the page
    actually stated is used; absence is its own key, never a wildcard.

    A year stated in the **value** counts. Reading only the rule's `_context` put
    Caltech's "May 1, 2027" in the same group as a November 1 that states no year, and
    reported the two as competing answers about one deadline.
    """
    context = row.context
    for name in ("academic_year_raw", "entry_year"):
        if context.get(name) is not None:
            return f"{name}={context[name]}"
    year = (row.value or {}).get("year")
    if year is not None:
        return f"year={year}"
    return "PERIOD_NOT_STATED"


def _program_key(row: CandidateRow) -> str:
    """The programme a claim is about, where the page tied it to one.

    Section 6: an institution-wide claim and a programme-specific one must not merge.
    They get different keys here, so they cannot land in the same group even when the
    value is identical.

    The sentinel is `PROGRAM_UNKNOWN`, deliberately not `INSTITUTION_WIDE`. A candidate
    that names no programme has not asserted that it applies to the whole institution --
    it has said nothing about programme at all, and labelling that as institution-wide
    would be section 10's `UNIVERSAL`-from-silence mistake in a different column. Only
    `DEGREE_LEVEL` and `DISCIPLINE_HINT` candidates ever carry a programme name.
    """
    name = row.context.get("program_name")
    return f"program={str(name).strip().lower()}" if name else "program=PROGRAM_UNKNOWN"


def context_key_for(row: CandidateRow) -> tuple[str, ...] | None:
    """What this claim is about, or None when it cannot be compared safely.

    Institution is always first and cross-institution grouping never happens: two
    universities publishing the same fee is a coincidence, not corroboration.
    """
    required = REQUIRED_CONTEXT.get(row.field_kind, ())
    if "__never__" in required:
        return None
    value = row.value or {}
    for name in required:
        if value.get(name) in (None, [], ""):
            return None

    parts: list[str] = [
        f"institution={row.institution}",
        f"field={row.field_kind}",
        f"scope={_scope_key(row.value)}",
        _program_key(row),
        _period_key(row),
    ]
    if row.field_kind in (
        FieldKind.LANGUAGE_OVERALL_SCORE.value,
        FieldKind.LANGUAGE_COMPONENT_SCORE.value,
        FieldKind.LANGUAGE_TEST.value,
    ):
        # Never merge IELTS with TOEFL (section 16): the test is part of what the claim
        # is *about*, not part of what it says.
        parts.append(f"test={value.get('test')}")
    if row.field_kind == FieldKind.LANGUAGE_COMPONENT_SCORE.value:
        parts.append(f"component={value.get('applies_to')}")
    if row.field_kind == FieldKind.APPLICATION_DEADLINE.value:
        # Never merge Round 1 with Round 2 even when the date matches (section 18).
        # Only a round the page's own wording stated separates a group: a label taken
        # from a section heading was stamped on dates belonging to other rounds, so it
        # is recorded and reported but does not split anything.
        stated = row.context.get("round_label_source") == "text"
        label = row.context.get("round_label") if stated else None
        parts.append(f"round={str(label).strip().lower() if label else 'ROUND_NOT_STATED'}")
    if row.field_kind == FieldKind.TUITION.value:
        parts.append(f"category={','.join(sorted(value.get('student_category') or []))}")
        parts.append(f"unit={value.get('billing_unit')}")
    if row.field_kind == FieldKind.PROGRAM_NAME.value:
        parts.append(f"name={str(value.get('program_name') or '').strip().lower()}")
    return tuple(parts)


#: The value components that decide whether two claims in one context say the same
#: thing. Everything else on the value -- raw wording, the rule's own context block --
#: is evidence about how it was found, not part of what was said.
VALUE_COMPONENTS: dict[str, tuple[str, ...]] = {
    FieldKind.TUITION.value: ("amount_kind", "amount_min", "amount_max", "currency"),
    FieldKind.LANGUAGE_OVERALL_SCORE.value: ("score", "operator"),
    FieldKind.LANGUAGE_COMPONENT_SCORE.value: ("score", "operator"),
    FieldKind.LANGUAGE_TEST.value: ("test",),
    FieldKind.APPLICATION_DEADLINE.value: (
        "deadline_kind",
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "timezone",
    ),
    FieldKind.ACADEMIC_CALENDAR_EVENT.value: ("year", "month", "day"),
    FieldKind.PROGRAM_NAME.value: ("program_name",),
    FieldKind.DEGREE_LEVEL.value: ("degree_level",),
    FieldKind.DISCIPLINE_HINT.value: ("discipline",),
}


def value_key_for(row: CandidateRow) -> tuple[str, ...]:
    """What this claim says, reduced to the components that decide agreement."""
    value = row.value or {}
    components = VALUE_COMPONENTS.get(row.field_kind)
    if components is None:
        return (f"raw={row.value_raw_text.strip().lower()}",)
    return tuple(f"{name}={value.get(name)!r}" for name in components)


# ===========================================================================
# Groups
# ===========================================================================


@dataclass(frozen=True, slots=True)
class AgreementGroup:
    """Every candidate that is about one thing, and what they collectively say."""

    context_key: tuple[str, ...]
    field_kind: str
    institution: str
    verdict: Agreement
    members: tuple[CandidateRow, ...]
    #: value key -> the candidates asserting it. More than one entry is a conflict.
    values: dict[tuple[str, ...], tuple[CandidateRow, ...]] = field(default_factory=dict)

    @property
    def sources(self) -> frozenset[uuid.UUID]:
        return frozenset(member.source_id for member in self.members)

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(sorted({member.url for member in self.members}))

    @property
    def context_is_thin(self) -> bool:
        """Did the context these claims share contain unknowns?

        Reported beside `AGREES` rather than suppressing it. Two pages agreeing about a
        fee neither of them scopes is still worth a reviewer knowing; what they must not
        be told is that the scope was established.
        """
        return has_sentinel(self.context_key)

    @property
    def unconfirmed_disagreement(self) -> bool:
        """Several values, in a context too thin to call it a conflict.

        Counted and reported separately. The gate that keeps these out of the conflict
        report is what stops a fee table's ten line items being read as a ten-way
        disagreement; it also means a real contradiction under a thin context is not
        confirmed, and pretending otherwise in either direction would be worse.
        """
        return self.verdict is Agreement.INSUFFICIENT_CONTEXT and len(self.values) > 1

    def describe(self) -> str:
        return (
            f"{self.verdict.value}: {len(self.members)} candidate(s) from "
            f"{len(self.sources)} source(s), {len(self.values)} distinct value(s)"
        )


def group_candidates(rows: list[CandidateRow]) -> list[AgreementGroup]:
    """Every agreement group over these candidates. Deterministic, order-independent.

    Candidates whose context cannot be keyed become a singleton `INSUFFICIENT_CONTEXT`
    group rather than being dropped: "we cannot compare this safely" is a finding a
    reviewer needs, and silently omitting them would make the coverage of this report a
    function of how little context the page gave.
    """
    by_context: dict[tuple[str, ...], list[CandidateRow]] = defaultdict(list)
    ungroupable: list[CandidateRow] = []

    for row in rows:
        key = context_key_for(row)
        if key is None:
            ungroupable.append(row)
        else:
            by_context[key].append(row)

    groups: list[AgreementGroup] = []
    for key, members in sorted(by_context.items()):
        by_value: dict[tuple[str, ...], list[CandidateRow]] = defaultdict(list)
        for member in members:
            by_value[value_key_for(member)].append(member)
        sources = {member.source_id for member in members}

        thin = has_sentinel(key)
        if len(by_value) > 1 and not thin:
            verdict = Agreement.CONFLICTS
        elif len(by_value) > 1:
            # Several values, but the key that brought them together is made of
            # unknowns. Caltech's admit-reply date and its application deadline are not
            # rival answers to one question, and a key of sentinels has not established
            # that the question is the same. Reported, not resolved, not discarded.
            verdict = Agreement.INSUFFICIENT_CONTEXT
        elif len(members) == 1:
            verdict = Agreement.SINGLE
        elif len(sources) > 1:
            verdict = Agreement.AGREES
        else:
            # One source repeating itself. Corroboration this is not: a university's
            # overview page echoing its own detail page is one statement published twice.
            verdict = Agreement.POSSIBLE_DUPLICATE

        groups.append(
            AgreementGroup(
                context_key=key,
                field_kind=members[0].field_kind,
                institution=members[0].institution,
                verdict=verdict,
                members=tuple(sorted(members, key=lambda row: str(row.candidate_id))),
                values={
                    value: tuple(sorted(rows_, key=lambda row: str(row.candidate_id)))
                    for value, rows_ in sorted(by_value.items())
                },
            )
        )

    for row in sorted(ungroupable, key=lambda row: str(row.candidate_id)):
        # Two reasons a row cannot be keyed, and they are not the same finding. A field
        # in `NEVER_GROUPED` will never key, whatever the page says; any other row here
        # is missing a component this field needs, which a reviewer can act on.
        groups.append(
            AgreementGroup(
                context_key=(f"candidate={row.candidate_id}",),
                field_kind=row.field_kind,
                institution=row.institution,
                verdict=(
                    Agreement.NOT_COMPARABLE
                    if row.field_kind in NEVER_GROUPED
                    else Agreement.INSUFFICIENT_CONTEXT
                ),
                members=(row,),
                values={value_key_for(row): (row,)},
            )
        )
    return groups


def conflicts(groups: list[AgreementGroup]) -> list[AgreementGroup]:
    """Only the groups where sources disagree. No winner is chosen."""
    return [group for group in groups if group.verdict is Agreement.CONFLICTS]


@dataclass(frozen=True, slots=True)
class ContextConflict:
    """One value carrying contradictory context at one institution.

    Keying by context hides this class of disagreement: Harvard's two pages both give
    January 1 as a deadline, one calling it Regular Decision and the other Early Action,
    so by context they are two unrelated groups and the contradiction disappears. The
    value is the same; what the sources disagree about is *what it is a deadline for*,
    which is exactly as much of a conflict as two different dates.
    """

    institution: str
    field_kind: str
    value_key: tuple[str, ...]
    contexts: dict[tuple[str, ...], tuple[CandidateRow, ...]]

    @property
    def members(self) -> tuple[CandidateRow, ...]:
        return tuple(row for rows in self.contexts.values() for row in rows)


def context_conflicts(rows: list[CandidateRow]) -> list[ContextConflict]:
    """One value, one institution, more than one context. No winner is chosen."""
    buckets: dict[tuple[str, str, tuple[str, ...]], dict[tuple[str, ...], list[CandidateRow]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for row in rows:
        key = context_key_for(row)
        if key is None:
            continue
        buckets[(row.institution, row.field_kind, value_key_for(row))][key].append(row)

    out: list[ContextConflict] = []
    for (institution, field_kind, value), contexts in sorted(buckets.items()):
        if len(contexts) < 2:
            continue
        # The component they DIFFER on has to be stated on both sides. Differing only
        # because one page said which round it meant and the other did not is a gap, not
        # a contradiction. Requiring the whole key to be free of unknowns was too strict
        # and discarded the one case this detector exists for: Harvard's January 1,
        # labelled Early Action on one page and Regular Decision on the other, with
        # everything else unknown on both.
        keys = list(contexts)
        differing = {
            index
            for index in range(len(keys[0]))
            if len({key[index] for key in keys if index < len(key)}) > 1
        }
        if not differing or any(
            key[index].split("=", 1)[-1] in SENTINELS
            for key in keys
            for index in differing
            if index < len(key)
        ):
            continue
        out.append(
            ContextConflict(
                institution=institution,
                field_kind=field_kind,
                value_key=value,
                contexts={
                    key: tuple(sorted(members, key=lambda row: str(row.candidate_id)))
                    for key, members in sorted(contexts.items())
                },
            )
        )
    return out


def corroborated_ids(groups: list[AgreementGroup]) -> frozenset[uuid.UUID]:
    """Candidates that another *independent source* agrees with (section 20).

    Used by review priority as one signal among several. Section 20 is explicit that
    agreement does not verify anything and must not become an automatic acceptance: this
    returns ids, never a decision.
    """
    out: set[uuid.UUID] = set()
    for group in groups:
        if group.verdict is Agreement.AGREES:
            out.update(member.candidate_id for member in group.members)
    return frozenset(out)


def conflicted_ids(groups: list[AgreementGroup]) -> frozenset[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for group in conflicts(groups):
        out.update(member.candidate_id for member in group.members)
    return frozenset(out)


# ===========================================================================
# Source-local context groups (sections 8, 16)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class ContextGroup:
    """Candidates that came from ONE region of ONE document.

    The grouping section 8 asks for -- programme name with its degree level and
    discipline hint -- and section 16's -- a test with its overall score and component
    minimum. Both are safe for the same reason: the members share an extraction *and* a
    locator, so they are provably parts of one statement rather than two parts of a page
    that happen to be about related things (section 28).
    """

    extraction_id: uuid.UUID
    institution: str
    url: str
    #: What ties the members together. For programmes, the locator; for language, the
    #: block plus the test, since one block may name two tests.
    anchor: str
    members: tuple[CandidateRow, ...]

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted({member.field_kind for member in self.members}))


#: Field kinds a programme context group may contain (section 8).
PROGRAM_KINDS: frozenset[str] = frozenset(
    {
        FieldKind.PROGRAM_NAME.value,
        FieldKind.DEGREE_LEVEL.value,
        FieldKind.DISCIPLINE_HINT.value,
        FieldKind.DURATION.value,
        FieldKind.STUDY_MODE.value,
        FieldKind.CAMPUS.value,
        FieldKind.FACULTY_OR_SCHOOL.value,
    }
)

LANGUAGE_KINDS: frozenset[str] = frozenset(
    {
        FieldKind.LANGUAGE_TEST.value,
        FieldKind.LANGUAGE_OVERALL_SCORE.value,
        FieldKind.LANGUAGE_COMPONENT_SCORE.value,
    }
)


def _locator_anchor(locator: dict[str, Any]) -> str:
    """A stable string for one region, for grouping within a document."""
    parts = [str(locator.get("kind"))]
    for name in ("block_index", "list_item_index", "table_index", "row_index", "link_index"):
        if locator.get(name) is not None:
            parts.append(f"{name}={locator[name]}")
    return "/".join(parts)


def program_groups(rows: list[CandidateRow]) -> list[ContextGroup]:
    """Programme candidates that came from one region of one document.

    Keyed on `(extraction, locator)` because that is what the rule actually produced:
    `_program_candidates` emits the name, the level and the discipline hint for one
    heading or one link with one identical locator. Grouping on anything looser -- the
    same page, a nearby heading -- would combine unrelated parts of a page, which
    section 8 forbids.
    """
    return _context_groups(rows, PROGRAM_KINDS, lambda row: _locator_anchor(row.locator))


def language_groups(rows: list[CandidateRow]) -> list[ContextGroup]:
    """Language candidates from one block that are about one test.

    The block alone is not enough: one paragraph may name IELTS and TOEFL, and section
    16 says never to merge them. So the anchor is the block *and* the test, which keeps
    "IELTS 7.0 overall, no component below 6.5" together and keeps TOEFL out of it.
    """

    def anchor(row: CandidateRow) -> str:
        test = (row.value or {}).get("test")
        block = row.locator.get("block_index")
        # The list item as well as the block: UNSW's block 21 list item 0 is one
        # ~13,200-character item naming IELTS, TOEFL, PTE, CAE and CPE, and its
        # siblings name them again. Block alone would merge two separate statements.
        item = row.locator.get("list_item_index")
        return f"block={block}/item={item}/test={test}"

    return _context_groups(rows, LANGUAGE_KINDS, anchor)


def _context_groups(
    rows: list[CandidateRow], kinds: frozenset[str], anchor: Any
) -> list[ContextGroup]:
    buckets: dict[tuple[uuid.UUID, str], list[CandidateRow]] = defaultdict(list)
    for row in rows:
        if row.field_kind not in kinds:
            continue
        buckets[(row.extraction_id, anchor(row))].append(row)
    return [
        ContextGroup(
            extraction_id=extraction_id,
            institution=members[0].institution,
            url=members[0].url,
            anchor=key,
            members=tuple(sorted(members, key=lambda row: row.field_kind)),
        )
        for (extraction_id, key), members in sorted(
            buckets.items(), key=lambda item: (str(item[0][0]), item[0][1])
        )
    ]


__all__ = [
    "LANGUAGE_KINDS",
    "NEVER_GROUPED",
    "PROGRAM_KINDS",
    "REQUIRED_CONTEXT",
    "SENTINELS",
    "VALUE_COMPONENTS",
    "Agreement",
    "AgreementGroup",
    "CandidateRow",
    "ContextConflict",
    "ContextGroup",
    "conflicted_ids",
    "conflicts",
    "context_conflicts",
    "context_key_for",
    "corroborated_ids",
    "group_candidates",
    "has_sentinel",
    "language_groups",
    "program_groups",
    "value_key_for",
]
