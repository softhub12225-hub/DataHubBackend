"""PostgreSQL enum types.

These are **closed technical states**: adding a member is a deliberate schema change
with code that must handle it. Evolving controlled vocabularies (destinations,
disciplines, currencies, round types, student categories) are *tables* instead, so
operations can extend them without a deployment — see `domains/taxonomy/models.py`.

Every enum here is created once by the reference/taxonomy migration and referenced by
name thereafter, so `create_type=False` is set on every column usage.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


class FieldStatus(StrEnum):
    """Publication state of a governed fact (architecture D2).

    Not a nullable value: "we have not checked" and "the official source publishes
    nothing here" are different facts, and collapsing them into NULL loses the
    distinction the PRD's 未知可见 rule depends on.
    """

    NOT_CHECKED = "NOT_CHECKED"
    OFFICIALLY_NOT_PUBLISHED = "OFFICIALLY_NOT_PUBLISHED"
    PUBLISHED = "PUBLISHED"
    WITHDRAWN = "WITHDRAWN"


class TemporalPrecision(StrEnum):
    """How precisely a source stated a date.

    Application-level vocabulary only: **not** a PostgreSQL enum type. The column is
    a generated `text` constrained by CHECK, because PostgreSQL requires generated
    expressions to be IMMUTABLE and the text-to-enum cast is only STABLE.
    """

    DATETIME = "DATETIME"
    DATE = "DATE"
    MONTH_PART = "MONTH_PART"
    MONTH = "MONTH"
    YEAR = "YEAR"


class MonthPart(StrEnum):
    """ "early/mid/late January". Structured metadata only.

    Deliberately does NOT narrow the derived calendar range (C14): mapping these to
    day ranges is a product interpretation, not something a source published.
    """

    EARLY = "EARLY"
    MID = "MID"
    LATE = "LATE"


class DeadlineKind(StrEnum):
    """What the official page says about closing (C4)."""

    FIXED_DATE = "FIXED_DATE"
    ROLLING = "ROLLING"
    UNTIL_FILLED = "UNTIL_FILLED"
    NO_FIXED_DEADLINE = "NO_FIXED_DEADLINE"
    NOT_CURRENTLY_ACCEPTING = "NOT_CURRENTLY_ACCEPTING"


class RiskLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TrustStatus(StrEnum):
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"
    STALE = "STALE"


class LifecycleStatus(StrEnum):
    """Whether a program or offering is currently on offer."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    WITHDRAWN = "WITHDRAWN"
    NOT_OFFERED_THIS_CYCLE = "NOT_OFFERED_THIS_CYCLE"


class RequirementGrain(StrEnum):
    """Which level a requirement attaches to. Generated from the FK columns (C6).

    Application-level vocabulary only, for the same reason as `TemporalPrecision`.
    """

    PROGRAM = "PROGRAM"
    OFFERING = "OFFERING"
    INTAKE = "INTAKE"


class ScopeOperator(StrEnum):
    """How an applicant-scope criterion compares."""

    EQUALS = "EQUALS"
    IN_GROUP = "IN_GROUP"
    NOT_EQUALS = "NOT_EQUALS"
    NOT_IN_GROUP = "NOT_IN_GROUP"


class SourceResponsibility(StrEnum):
    """字段归责 — how authoritative a source is for a given field."""

    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    CORROBORATING = "CORROBORATING"


class SourceAccessState(StrEnum):
    """D6: a blocked source is reclassified, never circumvented."""

    OK = "OK"
    BLOCKED = "BLOCKED"
    MANUAL_ONLY = "MANUAL_ONLY"


class FetchStatus(StrEnum):
    OK = "OK"
    UNCHANGED = "UNCHANGED"
    HTTP_ERROR = "HTTP_ERROR"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    PARSE_FAILED = "PARSE_FAILED"
    #: A worker stopped renewing its lease and the sweeper closed out the attempt
    #: (Step 3.5). Added to the PostgreSQL type by revision 0015; it was missing here,
    #: so reading back such a row would have failed to coerce. `test_db_enums` now
    #: compares members, not just type names, so the two cannot diverge again.
    ABANDONED = "ABANDONED"
    #: Step 5B.2. Four statuses whose *operational consequence* differs, which is the
    #: only reason to distinguish them. Folding any of these into `BLOCKED` or
    #: `HTTP_ERROR` is what made a single 429 permanent.
    #:
    #: The site asked us to slow down. Earns a cooldown and a later retry -- never a
    #: permanent block, and never a faster retry than it named.
    RATE_LIMITED = "RATE_LIMITED"
    #: The resolver did not answer: timeout, SERVFAIL, EAI_AGAIN. Says nothing about
    #: the source, so it earns a bounded backoff and no judgement.
    DNS_TEMPORARY = "DNS_TEMPORARY"
    #: NXDOMAIN: the name does not exist. A person has to look at the URL, because no
    #: number of retries invents a hostname.
    NAME_NOT_RESOLVED = "NAME_NOT_RESOLVED"
    #: **Our** defect, not the site's behaviour. Recorded so the page is not silently
    #: skipped, and deliberately not retried: hammering a university over a bug on
    #: our side is the wrong direction for the cost to fall.
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AcquisitionRecoveryAction(StrEnum):
    """Manual state transitions on an acquisition source (Step 5B.2 section 17).

    Written to `audit_log.action`, a `String(96)` shared by every consequential action.
    Named so the trail cannot be misread: `ACQUISITION_SOURCE_REENABLE` means *"allow
    a technical fetch attempt again"* and has nothing to do with publication trust,
    which only C27's earned eligibility confers.

    Automatic cooldowns are **not** here. A cooldown expiring is timing, not a
    decision, and auditing every tick would bury the four rows that are decisions
    under thousands that are not (section 18).
    """

    REENABLE = "ACQUISITION_SOURCE_REENABLE"
    DISABLE = "ACQUISITION_SOURCE_DISABLE"
    NEEDS_REVIEW = "ACQUISITION_SOURCE_NEEDS_REVIEW"
    CLEAR_COOLDOWN = "ACQUISITION_SOURCE_CLEAR_COOLDOWN"


class ExtractionStatus(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DetectionType(StrEnum):
    AUTO_DIFF = "AUTO_DIFF"
    MANUAL_EDIT = "MANUAL_EDIT"
    IMPORT = "IMPORT"
    CORRECTION = "CORRECTION"


class ProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    PENDING_SECOND_REVIEW = "PENDING_SECOND_REVIEW"
    APPROVED = "APPROVED"
    RETURNED = "RETURNED"
    PUBLISHED = "PUBLISHED"
    DISCARDED = "DISCARDED"


class ReviewDecisionKind(StrEnum):
    APPROVE = "APPROVE"
    RETURN = "RETURN"
    CORRECT = "CORRECT"


class ReviewTaskState(StrEnum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"
    REASSIGNED = "REASSIGNED"


class EntityRelationshipKind(StrEnum):
    """Real-entity replacement. A rename is NOT one of these (B6)."""

    SUPERSEDED_BY = "SUPERSEDED_BY"
    MERGED_INTO = "MERGED_INTO"
    SPLIT_INTO = "SPLIT_INTO"


class AliasKind(StrEnum):
    FORMER_NAME = "FORMER_NAME"
    TRADE_NAME = "TRADE_NAME"
    ABBREVIATION = "ABBREVIATION"
    TRANSLITERATION = "TRANSLITERATION"
    EXTERNAL_ID = "EXTERNAL_ID"


class ActorType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    API_CLIENT = "API_CLIENT"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    INFLIGHT = "INFLIGHT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class ChangeKind(StrEnum):
    """Business change classes that power Latest University Updates (D12)."""

    DEADLINE_CHANGED = "DEADLINE_CHANGED"
    TUITION_CHANGED = "TUITION_CHANGED"
    REQUIREMENT_CHANGED = "REQUIREMENT_CHANGED"
    LANGUAGE_REQUIREMENT_CHANGED = "LANGUAGE_REQUIREMENT_CHANGED"
    PROGRAM_OPENED = "PROGRAM_OPENED"
    PROGRAM_CLOSED = "PROGRAM_CLOSED"
    PROGRAM_SUSPENDED = "PROGRAM_SUSPENDED"
    OFFERING_ADDED = "OFFERING_ADDED"
    OFFERING_WITHDRAWN = "OFFERING_WITHDRAWN"
    INTAKE_ADDED = "INTAKE_ADDED"
    RANKING_PUBLISHED = "RANKING_PUBLISHED"
    PROFILE_UPDATED = "PROFILE_UPDATED"


class RootEntityType(StrEnum):
    """Versioned aggregate roots (D13). Only these get `entity_version` rows."""

    UNIVERSITY = "university"
    PROGRAM = "program"


# ---------------------------------------------------------------------------
# Step 4: target onboarding and official source mapping
# ---------------------------------------------------------------------------


class OnboardingStatus(StrEnum):
    """How far a client-nominated institution has progressed toward collection.

    This tracks *our readiness to collect*, never the institution's own facts. An
    institution at ``ACTIVE`` has verified official sources mapped; it does not
    thereby have a single published fact.

    ``BLOCKED`` and ``NEEDS_MANUAL_REVIEW`` are terminal-until-human states and are
    deliberately not on the happy path: an institution whose official identity
    cannot be established must stop here rather than be guessed forward.
    """

    NOT_STARTED = "NOT_STARTED"
    IDENTITY_VERIFICATION = "IDENTITY_VERIFICATION"
    DOMAIN_CANDIDATE = "DOMAIN_CANDIDATE"
    DOMAIN_VERIFIED = "DOMAIN_VERIFIED"
    SOURCE_MAPPING = "SOURCE_MAPPING"
    READY_FOR_COLLECTION = "READY_FOR_COLLECTION"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"


class OfficialVerificationStatus(StrEnum):
    """Whether a host or URL has been *confirmed* to be officially the university's.

    Shared by `official_domain` and `university_source_mapping` so the two cannot
    drift into different notions of "verified".

    ``AUTHORIZED_EXTERNAL`` is the load-bearing member. A great many universities
    run admissions on third-party SaaS (application portals, fee calculators,
    prospectus hosts). Such a host is legitimately authoritative *because a
    university authorised it*, which is a recorded human decision -- not because the
    university links to it. It is therefore a distinct status from
    ``VERIFIED_OFFICIAL`` and requires an authorisation reference, so no code path
    can promote "linked from the official site" into "is the official site".
    """

    VERIFIED_OFFICIAL = "VERIFIED_OFFICIAL"
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"
    LEGACY = "LEGACY"
    AUTHORIZED_EXTERNAL = "AUTHORIZED_EXTERNAL"


class DomainVerificationMethod(StrEnum):
    """*How* a host was confirmed. Required whenever a host leaves CANDIDATE.

    Recording the method is what makes a verification auditable rather than an
    assertion. Note what is absent: there is no member for "the name matched" or
    "it was the first search result". A plausible-looking domain is a
    ``CANDIDATE`` and nothing more.
    """

    GOVERNMENT_REGISTRY = "GOVERNMENT_REGISTRY"
    ACCREDITATION_BODY = "ACCREDITATION_BODY"
    UNIVERSITY_SELF_DECLARATION = "UNIVERSITY_SELF_DECLARATION"
    TLS_CERTIFICATE_SUBJECT = "TLS_CERTIFICATE_SUBJECT"
    MANUAL_STAFF_REVIEW = "MANUAL_STAFF_REVIEW"
    AUTHORIZED_PARTNER_AGREEMENT = "AUTHORIZED_PARTNER_AGREEMENT"


class SourceCategory(StrEnum):
    """What kind of official page a mapped URL is.

    Universities do not share a website structure, so this is a category of
    *content we need*, not a path template. One university may satisfy
    ``ENTRY_REQUIREMENTS`` from a page per programme and another from one table for
    the whole institution; both are mapped, and coverage is satisfied either way.
    """

    UNIVERSITY_HOME = "UNIVERSITY_HOME"
    UNDERGRADUATE_ADMISSIONS = "UNDERGRADUATE_ADMISSIONS"
    POSTGRADUATE_ADMISSIONS = "POSTGRADUATE_ADMISSIONS"
    PHD_ADMISSIONS = "PHD_ADMISSIONS"
    PROGRAM_CATALOG = "PROGRAM_CATALOG"
    FACULTY_OR_SCHOOL = "FACULTY_OR_SCHOOL"
    PROGRAM_PAGE = "PROGRAM_PAGE"
    ENTRY_REQUIREMENTS = "ENTRY_REQUIREMENTS"
    LANGUAGE_REQUIREMENTS = "LANGUAGE_REQUIREMENTS"
    TUITION_FEES = "TUITION_FEES"
    APPLICATION_DEADLINES = "APPLICATION_DEADLINES"
    OFFICIAL_PDF = "OFFICIAL_PDF"
    ACADEMIC_CALENDAR = "ACADEMIC_CALENDAR"
    GOVERNMENT = "GOVERNMENT"
    AUTHORIZED_RANKING = "AUTHORIZED_RANKING"


class DegreeScope(StrEnum):
    """Which applicant audience a source page serves.

    Deliberately *not* `degree_level`. The award a student receives (Bachelor,
    Master, Doctorate) and the admissions audience a university's site is organised
    around are different axes, and conflating them is how a taught-Masters fee page
    gets read as authoritative for PhD funding.

    ``TAUGHT_POSTGRADUATE`` and ``RESEARCH_POSTGRADUATE`` are separate members
    precisely because a Masters page must never imply PhD coverage: the two are
    almost always different pages, run by different offices.
    """

    UNDERGRADUATE = "UNDERGRADUATE"
    TAUGHT_POSTGRADUATE = "TAUGHT_POSTGRADUATE"
    RESEARCH_POSTGRADUATE = "RESEARCH_POSTGRADUATE"


class AcquisitionFetchStrategy(StrEnum):
    """How a mapped URL would be retrieved once collection is authorised.

    Recorded during onboarding so scheduling is a configuration decision rather
    than something a crawler infers at runtime. ``HTTP`` corresponds to
    ``source.fetch_strategy = 'STATIC'`` in the evidence plane; the names differ
    because the client's specification uses these, and the mapping is applied at
    promotion time (see `domains/onboarding/models.py`).
    """

    HTTP = "HTTP"
    BROWSER = "BROWSER"
    DOCUMENT = "DOCUMENT"
    MANUAL = "MANUAL"


class TargetChangeKind(StrEnum):
    """A difference between two versions of the client's target list.

    ``REMOVED_FROM_NEW_LIST`` is a *statement about the list*, not an instruction to
    delete anything. Canonical universities, evidence and history outlive the list
    that first nominated them (Step 4 requirement 13).
    """

    ADDED_TARGET = "ADDED_TARGET"
    REMOVED_FROM_NEW_LIST = "REMOVED_FROM_NEW_LIST"
    RANK_CHANGED = "RANK_CHANGED"
    SCORE_CHANGED = "SCORE_CHANGED"
    NAME_CHANGED = "NAME_CHANGED"
    REGION_CHANGED = "REGION_CHANGED"


class PublicationEligibility(StrEnum):
    """Whether a source may support the publication of a governed fact.

    WHY THIS EXISTS
    ===============
    Step 4 claimed that database role separation prevented client-spreadsheet and QS
    values from becoming published university facts. **That claim was wrong.** Role
    separation stops the API and the workers writing canonical tables at all, but
    ``app_publisher`` can read the onboarding tables *and* write the canonical ones,
    so a buggy or compromised publication service could read
    ``target_list_entry.qs_name`` and write it into ``university.name_en``. Grants
    cannot express "this value may determine scope but may not become a fact",
    because that is a statement about a value's *origin*, not about a table.

    So origin is made explicit and checked. Every `source` carries one of these
    classes, and triggers on `field_claim` and `field_provenance` refuse evidence
    whose class does not permit the fact being asserted. Those triggers are genuine
    enforcement here, verified against the live database: ``app_publisher`` owns no
    table, so ``ALTER TABLE ... DISABLE TRIGGER`` fails with "must be owner", and it
    cannot ``SET session_replication_role``.

    WHAT IT STILL CANNOT DO
    =======================
    Nothing in the database can tell where a *string* came from. A publication
    service that reads a QS name and types it into a canonical insert while citing a
    genuinely official source produces a row that is indistinguishable from an honest
    one. That limit is real, is documented in ARCHITECTURE.md under C27, and is why
    the Step 9 publication transaction must compare each published value against the
    cited claim rather than trusting its own inputs.
    """

    TARGET_SCOPE_ONLY = "TARGET_SCOPE_ONLY"
    """Establishes project scope and nothing else.

    The client's target workbook and the QS name/rank/score it carries. It may decide
    *which* institutions we cover; it may never support a published fact about one.
    A source in this class is rejected as evidence by trigger.
    """

    OFFICIAL_VERIFIED = "OFFICIAL_VERIFIED"
    """A verified university or government/regulator source. Eligible as evidence."""

    AUTHORIZED_EXTERNAL = "AUTHORIZED_EXTERNAL"
    """An externally hosted service confirmed to be authorised by the institution.

    Eligible only within its recorded authorisation scope, which is expressed as
    `source_field_binding` rows -- the existing 字段归责 matrix, reused rather than
    duplicated by a second scope table. An application portal authorised for
    deadlines does not thereby become evidence for tuition.
    """

    AUTHORIZED_RANKING = "AUTHORIZED_RANKING"
    """A ranking publisher with a confirmed licence. Eligible for ranking facts ONLY.

    Not for a university's name, its programmes, or anything else a ranking file
    happens to contain. This is the class QS data would occupy *if* a licence
    existed; it does not, so `RANKINGS_ENABLED` is false and no source holds this
    class today (U9).
    """

    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    """Not usable as evidence. The default for a newly registered source, so a source
    is closed until someone classifies it."""


#: Classes that may support a published fact at all. The two excluded members are the
#: point of the enum, so the set is named once and shared by the models, the
#: migration's frozen literal and the tests.
EVIDENCE_ELIGIBLE_CLASSES: tuple[PublicationEligibility, ...] = (
    PublicationEligibility.OFFICIAL_VERIFIED,
    PublicationEligibility.AUTHORIZED_EXTERNAL,
    PublicationEligibility.AUTHORIZED_RANKING,
)

#: Entity types an `AUTHORIZED_RANKING` source may support. Rankings are scoped by
#: *entity type* rather than by field path: every ranking value lives in one of these
#: tables, so the entity type alone answers "is this a ranking fact?" without a
#: field-path convention nobody would maintain.
#:
#: Frozen in the migration too (C21/C22): a migration must not import this.
RANKING_ENTITY_TYPES: tuple[str, ...] = (
    "ranking_entry",
    "ranking_edition",
    "ranking_publisher",
)


# ---------------------------------------------------------------------------
# Step 4 readiness: tuition shape, manual collection staging, verification queue
# ---------------------------------------------------------------------------


class TuitionAmountKind(StrEnum):
    """What shape an official page published a fee in (U14).

    Not a field status. "The university publishes no fee" is
    `amount_field_status = OFFICIALLY_NOT_PUBLISHED` with no kind; `VARIABLE` is the
    opposite -- the page addresses fees and does not give a figure.
    """

    EXACT = "EXACT"
    """One stated figure. Both endpoints are set and equal."""

    RANGE = "RANGE"
    """Two stated endpoints, e.g. "GBP 28,000-32,000 depending on pathway".

    No midpoint is ever derived. A consultant quoting 30,000 would be quoting a
    number no university published.
    """

    FROM = "FROM"
    """A stated floor and no ceiling: "from GBP 24,500"."""

    UP_TO = "UP_TO"
    """A stated ceiling and no floor: "up to GBP 9,250"."""

    VARIABLE = "VARIABLE"
    """Published, but not as a figure -- "fees vary by module selection".

    Both endpoints may be NULL, so `official_text` becomes mandatory: the wording is
    the whole fact.
    """


class SourceCandidateState(StrEnum):
    """Triage state of a URL a human collector supplied (U15).

    Deliberately separate from `OfficialVerificationStatus`. This records whether a
    *collected* URL has been looked at; that records whether a host or page is
    institutionally authoritative. A candidate reaching `VERIFIED` here means "worth
    registering", never "official" -- the domain and mapping workflow still has to
    run, and a source is never born `OFFICIAL_VERIFIED`.
    """

    PENDING = "PENDING"
    """Imported from a workbook and not yet looked at. The default."""

    VERIFIED = "VERIFIED"
    """A human confirmed this is the institution's page for what it claims to cover."""

    REJECTED = "REJECTED"
    """A human confirmed it is not. Kept, so the same wrong URL is not re-proposed."""

    NEEDS_REVIEW = "NEEDS_REVIEW"
    """Looked at and unresolved -- a second opinion is wanted. Never a silent pass."""


class PilotImportStatus(StrEnum):
    """Lifecycle of one returned workbook (U12)."""

    VALIDATED = "VALIDATED"
    """Read and checked; staging rows written. The normal end state."""

    REJECTED = "REJECTED"
    """Validation found errors; nothing was staged."""

    SUPERSEDED = "SUPERSEDED"
    """A later submission of the same list replaced it. Kept and queryable."""


class FetchEligibility(StrEnum):
    """May we *fetch* this source? An operational question, not a trust question.

    THIS IS NOT PUBLICATION ELIGIBILITY, AND CONFLATING THEM WAS A REAL MISTAKE
    ===========================================================================
    Step 5A coupled the two: a source could only be registered once its host was
    verified, so nothing could be fetched until a human had finished verifying it.
    That is backwards. Fetching a page is how we *find out* what it is; requiring
    the answer first means the reviewer has to judge a URL they cannot look at
    through our own record.

    So the two questions are separated:

    * `fetch_eligibility` -- may a worker send an HTTP request to this URL? Decided
      by syntax, scheme, SSRF validation, whether the source is active, and whether
      the site has told us to stop. A machine can answer it.
    * `publication_eligibility` (C27) -- may a fact derived from this source be
      published? Decided by domain verification, mapping promotion and responsibility
      verification. Only a person can answer it, and C27 is unchanged.

    A `FETCHABLE` source with `NOT_ELIGIBLE` publication eligibility is the **normal**
    state for everything in the pilot right now: we may look, and nothing we see may
    be published yet. Evidence captured this way is real evidence of what a page said;
    it is simply not yet evidence anyone may cite.
    """

    FETCHABLE = "FETCHABLE"
    """Passed technical and safety validation. A worker may request it."""

    BLOCKED = "BLOCKED"
    """The site refused us -- 403, a WAF, a CAPTCHA wall, repeated 429. Set from
    observed behaviour, and it stops the retry loop rather than working around it."""

    DISABLED = "DISABLED"
    """Switched off by a person, or the source itself is inactive."""

    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
    """Something a machine should not decide alone: a redirect leaving the
    institution's host, a content type nobody expected, an unresolvable name."""


#: Fetch eligibility states that permit a worker to send a request. One name, so a
#: scheduler and a report cannot disagree about what "schedulable" means.
FETCHABLE_STATES: frozenset[FetchEligibility] = frozenset({FetchEligibility.FETCHABLE})


class PilotSubmissionKind(StrEnum):
    """What a returned workbook is for.

    Application-level vocabulary, not a PostgreSQL type: the column is a
    `String(32)` with a CHECK, because this list will grow as the client sends
    different kinds of file and none of them is a closed technical state.

    The distinction that matters is `defines_pilot_scope`: an official-source list
    says *which institutions the pilot covers*, a collection workbook says *what was
    read about them*. Conflating the two would let a facts workbook silently change
    the scope by omitting a row.
    """

    OFFICIAL_SOURCE_LIST = "OFFICIAL_SOURCE_LIST"
    """University -> official page URLs. Acquisition targets, no collected facts."""

    COLLECTION_WORKBOOK = "COLLECTION_WORKBOOK"
    """The generic pilot template: programmes, fees, deadlines, requirements."""


#: Staging-only `source_type` for a URL whose category nobody has decided yet (Step
#: 5A section 5). Deliberately **not** a `SourceCategory` member: the canonical enum
#: describes what a page *is*, and "we have not looked" is not one of those. Keeping
#: it out of `SourceCategory` also means no canonical row can ever carry it.
#:
#: A candidate may be rejected while unclassified -- "this is not a page we want" needs
#: no category -- but it may not be VERIFIED, because verification asserts the page is
#: authoritative *for something*, and there is nothing to be authoritative for yet.
UNCLASSIFIED_SOURCE_TYPE = "UNCLASSIFIED"


class PilotFactValidationState(StrEnum):
    """Whether one staged row is ready for reconciliation (U12)."""

    OK = "OK"
    SCOPE_MAPPING_REQUIRED = "SCOPE_MAPPING_REQUIRED"
    """A non-universal applicant scope nobody has mapped yet. Never becomes
    UNIVERSAL by default."""

    NEEDS_REVIEW = "NEEDS_REVIEW"
    """Something else a human must settle before this row can be reconciled."""


class OnboardingVerificationAction(StrEnum):
    """Manual decisions a reviewer can record against a host or source mapping.

    Application-level vocabulary only: **not** a PostgreSQL enum type. These are
    written to `audit_log.action`, which is a `String(96)` shared by every
    consequential action in the system. Introducing a second, narrower enum for the
    same column would make the audit chain's action vocabulary un-extendable.
    """

    VERIFY = "ONBOARDING_VERIFY"
    REJECT = "ONBOARDING_REJECT"
    REPLACE = "ONBOARDING_REPLACE"
    MARK_LEGACY = "ONBOARDING_MARK_LEGACY"
    REQUEST_REVIEW = "ONBOARDING_REQUEST_REVIEW"


class PilotCandidateAction(StrEnum):
    """Decisions a reviewer can record against a collected source candidate (U15).

    Application-level vocabulary, like `OnboardingVerificationAction`: these are
    written to `audit_log.action`, a `String(96)` shared by every consequential
    action in the system.

    Named distinctly from the onboarding actions on purpose. `ONBOARDING_VERIFY`
    means a host was confirmed to be officially the institution's;
    `PILOT_SOURCE_VERIFY` means a reviewer thinks a collected URL is worth
    registering. Collapsing them would make an audit trail read as though a
    spreadsheet row had verified a domain.

    A HISTORICAL COLLISION, DELIBERATELY NOT REWRITTEN
    --------------------------------------------------
    `register_verified_candidate` used to append `VERIFY` as well, so two different
    acts shared one name: the human deciding a URL is worth registering, and the
    mechanical creation of the `source_mapping` that follows. The real ANU pass
    produced **six** registration rows under `PILOT_SOURCE_VERIFY` (audit seq 28-33)
    on top of the six genuine decisions, so a reader counting that action sees twelve
    events where six judgements were made.

    Those six rows are **not** rewritten. The audit log is append-only and hash-chained;
    editing history to make a report tidier is precisely the thing the chain exists to
    make impossible, and a corrected row would be indistinguishable from a tampered one.
    They remain separable by inspection: a registration carries
    `after_state ? 'promoted_source_mapping_id'` and a decision does not.

    From Step 5C.7L onward registration appends `REGISTERED`, so the collision is
    bounded to those six rows and cannot grow.
    """

    VERIFY = "PILOT_SOURCE_VERIFY"
    REJECT = "PILOT_SOURCE_REJECT"
    NEEDS_REVIEW = "PILOT_SOURCE_NEEDS_REVIEW"
    CLASSIFY = "PILOT_SOURCE_CLASSIFY"
    REGISTERED = "PILOT_SOURCE_REGISTERED"
    """The `source_mapping` was created from a decision already made. Not a decision."""


# ---------------------------------------------------------------------------
# SQLAlchemy type objects
#
# `native_enum=True` with `create_type=False`: the types are created once by the
# taxonomy migration. Letting each table's DDL try to create them again produces
# "type already exists" on the second table that uses one.
# ---------------------------------------------------------------------------

_ENUM_SPECS: tuple[tuple[type[StrEnum], str], ...] = (
    (FieldStatus, "field_status"),
    (FetchEligibility, "fetch_eligibility"),
    (MonthPart, "month_part"),
    (DeadlineKind, "deadline_kind"),
    (RiskLevel, "risk_level"),
    (TrustStatus, "trust_status"),
    (LifecycleStatus, "lifecycle_status"),
    (ScopeOperator, "scope_operator"),
    (SourceResponsibility, "source_responsibility"),
    (SourceAccessState, "source_access_state"),
    (FetchStatus, "fetch_status"),
    (ExtractionStatus, "extraction_status"),
    (DetectionType, "detection_type"),
    (ProposalStatus, "proposal_status"),
    (ReviewDecisionKind, "review_decision_kind"),
    (ReviewTaskState, "review_task_state"),
    (EntityRelationshipKind, "entity_relationship_kind"),
    (AliasKind, "alias_kind"),
    (ActorType, "actor_type"),
    (OutboxStatus, "outbox_status"),
    (ChangeKind, "change_kind"),
    (RootEntityType, "root_entity_type"),
    # Step 4
    (OnboardingStatus, "onboarding_status"),
    (OfficialVerificationStatus, "official_verification_status"),
    (DomainVerificationMethod, "domain_verification_method"),
    (SourceCategory, "source_category"),
    (DegreeScope, "degree_scope"),
    (AcquisitionFetchStrategy, "acquisition_fetch_strategy"),
    (TargetChangeKind, "target_change_kind"),
    (PublicationEligibility, "publication_eligibility"),
    (TuitionAmountKind, "tuition_amount_kind"),
    (SourceCandidateState, "source_candidate_state"),
    (PilotImportStatus, "pilot_import_status"),
    (PilotFactValidationState, "pilot_fact_validation_state"),
)


def _pg(enum_cls: type[StrEnum], name: str) -> ENUM:
    return ENUM(
        enum_cls,
        name=name,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


FIELD_STATUS = _pg(FieldStatus, "field_status")
FETCH_ELIGIBILITY = _pg(FetchEligibility, "fetch_eligibility")
MONTH_PART = _pg(MonthPart, "month_part")
DEADLINE_KIND = _pg(DeadlineKind, "deadline_kind")
RISK_LEVEL = _pg(RiskLevel, "risk_level")
TRUST_STATUS = _pg(TrustStatus, "trust_status")
LIFECYCLE_STATUS = _pg(LifecycleStatus, "lifecycle_status")
SCOPE_OPERATOR = _pg(ScopeOperator, "scope_operator")
SOURCE_RESPONSIBILITY = _pg(SourceResponsibility, "source_responsibility")
SOURCE_ACCESS_STATE = _pg(SourceAccessState, "source_access_state")
FETCH_STATUS = _pg(FetchStatus, "fetch_status")
EXTRACTION_STATUS = _pg(ExtractionStatus, "extraction_status")
DETECTION_TYPE = _pg(DetectionType, "detection_type")
PROPOSAL_STATUS = _pg(ProposalStatus, "proposal_status")
REVIEW_DECISION_KIND = _pg(ReviewDecisionKind, "review_decision_kind")
REVIEW_TASK_STATE = _pg(ReviewTaskState, "review_task_state")
ENTITY_RELATIONSHIP_KIND = _pg(EntityRelationshipKind, "entity_relationship_kind")
ALIAS_KIND = _pg(AliasKind, "alias_kind")
ACTOR_TYPE = _pg(ActorType, "actor_type")
OUTBOX_STATUS = _pg(OutboxStatus, "outbox_status")
CHANGE_KIND = _pg(ChangeKind, "change_kind")
ROOT_ENTITY_TYPE = _pg(RootEntityType, "root_entity_type")
ONBOARDING_STATUS = _pg(OnboardingStatus, "onboarding_status")
OFFICIAL_VERIFICATION_STATUS = _pg(OfficialVerificationStatus, "official_verification_status")
DOMAIN_VERIFICATION_METHOD = _pg(DomainVerificationMethod, "domain_verification_method")
SOURCE_CATEGORY = _pg(SourceCategory, "source_category")
DEGREE_SCOPE = _pg(DegreeScope, "degree_scope")
ACQUISITION_FETCH_STRATEGY = _pg(AcquisitionFetchStrategy, "acquisition_fetch_strategy")
TARGET_CHANGE_KIND = _pg(TargetChangeKind, "target_change_kind")
PUBLICATION_ELIGIBILITY = _pg(PublicationEligibility, "publication_eligibility")
TUITION_AMOUNT_KIND = _pg(TuitionAmountKind, "tuition_amount_kind")
SOURCE_CANDIDATE_STATE = _pg(SourceCandidateState, "source_candidate_state")
PILOT_IMPORT_STATUS = _pg(PilotImportStatus, "pilot_import_status")
PILOT_FACT_VALIDATION_STATE = _pg(PilotFactValidationState, "pilot_fact_validation_state")


def enum_ddl_specs() -> list[tuple[str, list[str]]]:
    """(type_name, members) for every enum, for the migration that creates them."""
    return [(name, [m.value for m in cls]) for cls, name in _ENUM_SPECS]


__all__ = [
    "ACQUISITION_FETCH_STRATEGY",
    "ACTOR_TYPE",
    "ALIAS_KIND",
    "CHANGE_KIND",
    "DEADLINE_KIND",
    "DEGREE_SCOPE",
    "DETECTION_TYPE",
    "DOMAIN_VERIFICATION_METHOD",
    "ENTITY_RELATIONSHIP_KIND",
    "EVIDENCE_ELIGIBLE_CLASSES",
    "EXTRACTION_STATUS",
    "FETCHABLE_STATES",
    "FETCH_ELIGIBILITY",
    "FETCH_STATUS",
    "FIELD_STATUS",
    "LIFECYCLE_STATUS",
    "MONTH_PART",
    "OFFICIAL_VERIFICATION_STATUS",
    "ONBOARDING_STATUS",
    "OUTBOX_STATUS",
    "PILOT_FACT_VALIDATION_STATE",
    "PILOT_IMPORT_STATUS",
    "PROPOSAL_STATUS",
    "PUBLICATION_ELIGIBILITY",
    "RANKING_ENTITY_TYPES",
    "REVIEW_DECISION_KIND",
    "REVIEW_TASK_STATE",
    "RISK_LEVEL",
    "ROOT_ENTITY_TYPE",
    "SCOPE_OPERATOR",
    "SOURCE_ACCESS_STATE",
    "SOURCE_CANDIDATE_STATE",
    "SOURCE_CATEGORY",
    "SOURCE_RESPONSIBILITY",
    "TARGET_CHANGE_KIND",
    "TRUST_STATUS",
    "TUITION_AMOUNT_KIND",
    "AcquisitionFetchStrategy",
    "ActorType",
    "AliasKind",
    "ChangeKind",
    "DeadlineKind",
    "DegreeScope",
    "DetectionType",
    "DomainVerificationMethod",
    "EntityRelationshipKind",
    "ExtractionStatus",
    "FetchEligibility",
    "FetchStatus",
    "FieldStatus",
    "LifecycleStatus",
    "MonthPart",
    "OfficialVerificationStatus",
    "OnboardingStatus",
    "OnboardingVerificationAction",
    "OutboxStatus",
    "PilotFactValidationState",
    "PilotImportStatus",
    "ProposalStatus",
    "PublicationEligibility",
    "RequirementGrain",
    "ReviewDecisionKind",
    "ReviewTaskState",
    "RiskLevel",
    "RootEntityType",
    "ScopeOperator",
    "SourceAccessState",
    "SourceCandidateState",
    "SourceCategory",
    "SourceResponsibility",
    "TargetChangeKind",
    "TemporalPrecision",
    "TrustStatus",
    "TuitionAmountKind",
    "enum_ddl_specs",
]
