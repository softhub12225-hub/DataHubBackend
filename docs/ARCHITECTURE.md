# Overseas University Information Platform — Architecture

**Status:** Approved, revision 13. Steps 3, 3.5, 4, 5A and 5B implemented and verified. The 35-university pilot's 319 pages can be fetched safely and preserved as immutable evidence; **no extraction and no publication**.
**Source of record:** `docs/PRD.docx` v1.0 (2026-09-16) + client chat history + architect decisions of 2026-09-17.
**Pilot destinations:** the client's 35 span US (13), UK (7), Australia (6), Hong Kong (4), Canada (3), Singapore (2) — wider than the UK/HK/Macau the PRD framing suggested (D30). · Bachelor + Master · Business, Computer Science/Data, Engineering.
**Volume target (PRD, as written):** ≥36 institutions, 120 faculty/campus relations, 600 programs (counting basis — see U3).
**Implemented pilot scope:** **35 institutions**, fixed by the client in Step 5A when they supplied the final source list. The PRD line above is left as written; see D30.
**Governing rule:** official or authorized sources only; field-level provenance; review before publish.

---

## 1. Decisions of record

### 1.1 Revision 2 decisions (unchanged)

| # | Decision |
|---|---|
| D1 | Evidence → Governance → Canonical separation. Crawler never writes published canonical data. Publication through one controlled transaction. |
| D2 | `field_status ∈ {NOT_CHECKED, OFFICIALLY_NOT_PUBLISHED, PUBLISHED, WITHDRAWN}` with `value = NULL` unless `PUBLISHED`. Display labels live in the UI, never in the database. |
| D3 | Four temporal concepts stay distinct: `effective_from`/`effective_to`, `observed_at`, `reviewed_at`, `published_at`. |
| D4 | Extractor-drift protection: a high-risk `value → absent` transition requires validation or becomes a source-health alert, not a change proposal. |
| D5 | **Assumption:** 14 days = MVP/pilot implementation period; Day 3 = first hard milestone. Not a confirmed requirement. |
| D6 | Never bypass CAPTCHA, authentication, WAF or access controls. Blocked sources → source-health issue + manual verification fallback. |
| D7 | A reviewer who alters a high-risk candidate value cannot approve it; it enters a second-review flow. An unaltered candidate from another user may be approved directly. |
| D8 | Rankings are feature-gated; without an authorized source, ingestion/display/filtering are disabled without blocking the MVP. |
| D9 | No full event sourcing. Immutable history + current canonical projection optimized for search/API. |
| D10 | Code-first contract: FastAPI/Pydantic → OpenAPI → generated TypeScript. No hand-maintained spec. |
| D11 | One Python codebase, four runtime roles: API, Celery worker, scheduler, browser worker. |
| D12 | MVP "Latest University Updates" = verified structured changes from `change_event`. No news/article crawling. |

### 1.2 Revision 3 corrections

| # | Correction | Effect |
|---|---|---|
| **C1** | **Provenance is append-only with no mutation whatsoever.** Supersession is expressed by version chronology, plus an append-only `entity_relationship` table for entity-level supersession. No `superseded_at` update, ever. | §5, §6 |
| **C2** | **Segregation of duties is enforced in the policy layer and re-validated inside the publication transaction.** A PostgreSQL `CHECK` is not used for cross-row/cross-user authorization; a trigger exists only as defense-in-depth. | §6.2, §7 |
| **C3** | Version invariant is **unique and monotonically increasing per root**. No gapless guarantee is required or claimed. Allocation happens inside the publication transaction while holding `SELECT … FOR UPDATE` on `entity_head`, so a rollback rolls back the allocation — an aborted transaction consumes nothing. | §6.2, §7 |

### 1.2.1 Revision 4 corrections

| # | Correction | Effect |
|---|---|---|
| **C4** | **A published deadline does not always have a date.** `deadline_kind ∈ {FIXED_DATE, ROLLING, UNTIL_FILLED, NO_FIXED_DEADLINE, NOT_CURRENTLY_ACCEPTING}`, with `deadline_at` nullable per kind. `PUBLISHED ⟺ deadline_at IS NOT NULL` is **not** a valid status constraint and is removed. | §4.5, §5.2, §8.1 |
| **C5** | **Application rounds are not just numbers.** `round_code` (controlled), `round_label` (the institution's own wording), `sequence_no` nullable, `opens_at` nullable — capable of representing Round 1, Priority, Early Action, Main Round, Rolling and institution-defined labels. | §4.5, §5.2, §8.1 |
| **C6** | **No unenforced polymorphic ownership.** Requirement applicability uses **nested-scope nullable FKs with composite foreign keys**, so PostgreSQL enforces that the offering belongs to the program and the intake belongs to that offering. No `owner_type`/`owner_id` pair. | §4.6, §8.1 |
| **C7** | **Business history and delivery infrastructure are separate tables.** `change_event` is immutable published business history and the source of Latest Updates. `outbox_message` is mutable delivery infrastructure (notifications, cache invalidation, integrations, retries). Both are written in the publication transaction where needed. | §5.3, §5.8, §7 |
| **C8** | **U4 resolved: program offerings are governed, source-backed facts.** Study mode, campus, delivery mode and duration require evidence and provenance because they determine admissions, tuition and comparison behaviour. Offerings enter through proposal → review → publish like any other governed fact. **U3 resolved:** "600 programs" means 600 `program` rows; offering count is reported separately. | §4.7, §5.2, §11 |

### 1.2.2 Revision 5 corrections

| # | Correction | Effect |
|---|---|---|
| **C9** | **Never manufacture temporal precision or timezone.** A single `timestamptz` plus a precision flag was wrong: it required inventing a time and a zone for a date-only fact. Temporal facts now store the calendar parts the source actually gave (`year`, `month`, `day`, `month_part`), an optional `time`, an optional declared timezone, and the verbatim source text. Precision is **derived** from which parts are present, and an exact UTC instant exists **only** where the source supplied date + time + zone. | §4.5, §5.2, §8.1 |
| **C10** | **`sequence_no` does not define round identity.** The previous `UNIQUE (intake_id, round_code, sequence_no) NULLS NOT DISTINCT` blocked two institution-defined rounds with different labels when `sequence_no` was null. Identity is now: controlled codes unique per `(intake, round_code)`; `INSTITUTION_DEFINED` unique per `(intake, normalised label)`. `sequence_no` is display/order metadata only. | §4.5, §8.1 |
| **C11** | **Four separate database identities from the outset**: a migration/owning role plus `app_api`, `app_worker`, `app_publisher`. No service may connect as the owning role, and there is no shared credential to fall back on. Privileges arrive with the Step 3 domain migrations; the identities and their enforcement exist now. | §2.2, §8.1, §8.2, §10 |
| **C12** | **Production immutable retention is a deployment decision, not a MinIO assumption.** Local development keeps bucket versioning. WORM/Object-Lock configuration depends on the selected production object-storage provider and hosting environment (assumption A16). Evidence integrity at the application and database level remains mandatory and is specified independently of any provider feature. | §5.5, §10.4, §11 |
| **C13** | **No UTC range where the source gave no timezone.** C9's replacement column, `window_utc tstzrange`, was still invention: its endpoints were manufactured offsets and its name asserted a zone the institution never published. Withdrawn. An exact `instant_utc` exists only with date + time + zone; imprecise facts are filtered and sorted by their calendar components, with a separate, explicitly calendar-local `cal_range daterange` as a derived query aid that is never source truth and never labelled UTC. | §4.5.1.1, §8.1, §8.3 |
| **C14** | **EARLY/MID/LATE must not be encoded as database semantics.** Mapping them to days 1-10 / 11-20 / 21-end is a product interpretation, not a source fact. A `MONTH_PART` temporal fact now covers the **whole stated month** in the derived calendar range -- conservative over-coverage rather than a guess -- while `month_part` is preserved as structured metadata. Narrower windows, if ever wanted, are a configurable product policy in the query layer. | §4.5.1.1, §8.1 |
| **C15** | **Two derived columns are generated `text` with a CHECK, not enums.** `<p>_precision` and `requirement.grain` cannot be enum-typed: PostgreSQL requires a generated expression to be IMMUTABLE and the text-to-enum cast (`enum_in`) is only STABLE. A CHECK constrains the values, so the columns are still impossible to set wrongly, which is the property that mattered. `TemporalPrecision` and `RequirementGrain` remain application-level vocabularies. | §4.5.1, §8.1 |
| **C16** | **`program.lifecycle_status` and `program_offering.lifecycle_status` are nullable with no default.** `NOT NULL DEFAULT 'ACTIVE'` made their `field_status` column decorative — the value/status CHECK could never be false — and, worse, asserted that an unverified programme was open. Discovered by the invariant tests; the same class of invention C9 removed from dates. | §4.7, §8.1 |
| **C17** | **Query-layer indexes are migration-owned, not model-declared.** Trigram, full-text, range-overlap and several partial indexes live in the search-index migration and are excluded from autogenerate comparison via `include_object`. Operator classes and text-search configurations do not round-trip through autogenerate, so declaring them on the models would make `alembic check` propose dropping them on every run. One shared name list keeps the migration and the exclusion in step. | §8.3, §10 |
| **C18** | **Audit chain order is an explicit monotonic `seq`, assigned under a row lock — not `(occurred_at, id)`.** Rows written in one transaction share `occurred_at` exactly, and the tie fell through to a random UUIDv4; under READ COMMITTED two writers also read the same predecessor. A single-row `audit_chain_head` is locked `FOR UPDATE` before predecessor assignment, so appends serialise instead of forking and retries stay safe. The trigger is `SECURITY DEFINER`, so no runtime role holds privileges on the head. UUID identity and chain ordering are now separate concerns, and UUIDv7 is explicitly not an integrity mechanism. | §9.2 |
| **C19** | **Fetch lifecycle split into mutable `fetch_attempt` + immutable `fetch_run`.** `fetch_run` was classified immutable while carrying `started_at`/`finished_at`/`status`, i.e. it wanted updating as work progressed. The attempt row is leased and heartbeated; a terminal state writes exactly one `fetch_run`, including `ABANDONED` for a crashed worker, so a crash becomes permanent history rather than a row stuck in RUNNING. Retries are new attempts. | §9.3 |
| **C20** | **Content identity split from observation.** `snapshot` carried `UNIQUE (source_id, content_hash)`, which made it *impossible* to record the same bytes observed twice from one source — silently destroying the 'we checked on this date' record. `content_blob` now holds content identity (the dedup boundary, one row per sha256) and `snapshot` holds observations, with no uniqueness on content and with `requested_url`/`effective_url`/`http_status`/`fetcher`. | §9.4 |
| **C21** | **A migration must not import a mutable shared constant.** Revision 0013 read the live table classification, so adding tables in Step 3.5 made it try to grant on tables that would not exist for two more revisions, and a fresh database could no longer be built. Its lists are now frozen literals; the shared module remains current-state truth for tests, and a coverage test asserts every classified table received grants from some revision. | §8.1 |

### 1.2.3 Revision 10 — Step 4 decisions and corrections

Step 4 introduced the **onboarding plane**: which institutions the client asked us to
cover, and how we established where each publishes official information. Full design
in [`docs/ONBOARDING.md`](ONBOARDING.md).

| # | Decision | Effect |
|---|---|---|
| **D18** | **A client target list is scope, never evidence.** A QS-derived spreadsheet answers exactly one question — which universities are in the project — and is authoritative for that and nothing else. Every QS-originated value is confined to `target_list_entry` (append-only, version-scoped). **This decision originally claimed privileges enforced it; they did not — see C27.** The enforcement is `source.publication_eligibility` plus triggers on `field_claim` and `field_provenance`. | [ONBOARDING §1](ONBOARDING.md#1-the-rule-this-phase-exists-to-protect), §8.2 |
| **D19** | **Target record and canonical identity are separate rows.** `target_institution` is client scope plus onboarding progress and carries **no** descriptive attribute of the institution; `university` is verified institutional identity. `matched_university_id` links them and is set only by a recorded human decision. A NULL match is a normal state, not a defect. | [ONBOARDING §3](ONBOARDING.md#3-data-model) |
| **D20** | **Trust in a host is a recorded human act with a named method.** `official_domain.verification_status` starts at `CANDIDATE` and can only reach a trusted state with a `verification_method`, a `verified_at` and a `verified_by` (CHECK). `DomainVerificationMethod` deliberately has no member meaning "the name matched". | [ONBOARDING §5](ONBOARDING.md#5-establishing-official-identity), §8.1 |
| **D21** | **`AUTHORIZED_EXTERNAL` is not official.** A third-party application portal may carry official information *because a university authorised it* — a recorded decision — not because the university links to it. It is a distinct status, requires an `authorization_reference`, and a trigger forbids an `AUTHORIZED_EXTERNAL` host from backing a `VERIFIED_OFFICIAL` source mapping. | [ONBOARDING §5](ONBOARDING.md#5-establishing-official-identity), §8.1 |
| **D22** | **Source audience is a separate axis from degree level.** `DegreeScope` (`UNDERGRADUATE` / `TAUGHT_POSTGRADUATE` / `RESEARCH_POSTGRADUATE`) describes which applicants a page serves; `degree_level` describes the award. Taught-Masters and doctoral admissions are separate members because they are separate pages run by separate offices, and coverage for one never implies the other. | [ONBOARDING §6](ONBOARDING.md#6-mapping-sources) |
| **D23** | **Registration-time URL validation is a floor, not the SSRF defence.** Only `http`/`https` may be *stored* (CHECK plus service validation), and IP literals, credentials, numeric-address spellings and local names are refused. DNS is deliberately not consulted: a resolution now says nothing about the resolution at fetch time. Fetch-time controls (resolve, reject non-public addresses, pin the address, re-check every redirect) remain mandatory and are specified in [`docs/ONBOARDING.md` §9](ONBOARDING.md#9-url-safety). | [ONBOARDING §9](ONBOARDING.md#9-url-safety), §8.1 |
| **D24** | **Pilot membership is not inferred.** The client's list holds 57 institutions across the pilot destinations while the PRD speaks of at least 36. Which 36 is the client's decision, so `pilot_wave` is nullable, is NULL for all 181 after import, and is modelled independently of destination. | [ONBOARDING §3](ONBOARDING.md#3-data-model), §11 |
| **D25** | **A hand-filled workbook is a collection artifact, not evidence.** A `field_claim` is a statement about a stored snapshot of a fetched page — `claim → extraction → snapshot → source` plus `snapshot → content_blob`, NOT NULL end to end — and a person typing into Excel produces none of it. The returned workbook therefore lands in its own plane (`pilot_submission`, `pilot_selected_university`, `pilot_collected_program`, `pilot_collected_source`, `pilot_collected_fact`), which no foreign key path reaches from `field_provenance`, which `app_publisher` holds no privilege on, and from which nothing becomes a claim without real acquisition. The alternative — manufacturing a fetch that never happened — would have made the evidence chain a formality. | [PILOT_COLLECTION §8](PILOT_COLLECTION.md#8-the-staging-plane-u12) |
| **D26** | **Workbook-local references are submission-local by key, not by convention.** `program_ref` (`P0001`) and `source_ref` (`S0001`) mean something only inside one file. Both definition tables carry a UNIQUE `(submission_id, ref)` and the fact table reaches them by a composite foreign key on the same pair, so a row physically cannot cite a programme or source from another workbook, and a second workbook reusing `P0001` is not a collision. | [PILOT_COLLECTION §4](PILOT_COLLECTION.md#4-the-three-identifiers) |
| **D27** | **A correction is a new submission.** `pilot_submission.file_sha256` is UNIQUE, so identical bytes re-import as a no-op; different bytes create a new submission and mark the previous one `SUPERSEDED`, with all of its rows untouched and still queryable. The collected rows are append-only by trigger. The point of keeping both versions is that they can be compared, and a comparison against a row edited in place compares nothing. | [PILOT_COLLECTION §8](PILOT_COLLECTION.md#8-the-staging-plane-u12) |
| **D28** | **A published fee records its shape, and no midpoint is ever derived.** `tuition.amount` was a single scalar bound to its status; "GBP 28,000–32,000 depending on pathway", "from GBP 24,500" and "fees vary by module" are all *published* fees with no scalar, and every way of forcing them into one column lies. Replaced by `amount_kind` (`EXACT`/`RANGE`/`FROM`/`UP_TO`/`VARIABLE`) with `amount_min`/`amount_max` and a CHECK per kind. A `RANGE` keeps both ends: 28,000–32,000 is not 30,000, and averaging is a presentation choice for a query layer that shows its working, never a stored value. `amount_kind` is **not** a status — `OFFICIALLY_NOT_PUBLISHED` still means the page says nothing, `VARIABLE` means it does address fees without giving a figure. | [PILOT_COLLECTION §9](PILOT_COLLECTION.md#9-fees-that-are-not-a-single-number-u14), §8.1 |
| **D29** | **A collected URL is a candidate, and a matching hostname is evidence rather than a decision.** ~200 URLs arrive with the workbook; each lands `PENDING` in `pilot_collected_source` and reaching `VERIFIED` is a recorded act with an actor, a timestamp and a reason (CHECK, plus the audit chain). `VERIFIED` here means "worth registering", never "official": registration still runs `official_domain` verification then `source_mapping` promotion, and C27 still refuses a `source` that was not earned. The queue surfaces whether a candidate's host falls under a domain already verified *for that institution* — the most useful thing to show a reviewer and the most tempting to automate on. It is not automated on: a university's own domain also carries news articles, student societies and staff pages, so a hostname says where a page lives, not what it is. | [PILOT_COLLECTION §10](PILOT_COLLECTION.md#10-the-source-verification-queue-u15) |
| **D30** | **The pilot is 35 institutions, chosen by the client, and not the destinations we assumed.** The PRD asked for ≥36 and its framing implied UK/Hong Kong/Macau; the client's final list names 35 across six destinations, 13 of them in the United States. Both statements are kept: the PRD line records what was asked for, `PILOT_INSTITUTION_COUNT = 35` is what the tooling now enforces, and a test asserts the historical text has not been quietly rewritten. Scope is set by the file that declares it — `pilot_submission.defines_pilot_scope`, which only an `OFFICIAL_SOURCE_LIST` may claim, so a facts workbook that merely omitted a university cannot drop it. | [PILOT_COLLECTION §11](PILOT_COLLECTION.md#11-the-final-source-list-step-5a) |
| **D33** | **Fetchable and publication-eligible are different questions, decided by different parties.** Step 5A coupled them: registration ran through `source_mapping`, which needs a verified `official_domain`, so with zero domains verified nothing could be fetched at all. That is backwards — fetching a page is how you find out what it is, and requiring the answer first makes the reviewer judge a URL they cannot see through our own record. `source.fetch_eligibility` (`FETCHABLE` / `BLOCKED` / `DISABLED` / `NEEDS_MANUAL_REVIEW`) is answered by a machine from syntax, scheme, SSRF validation and the site's own behaviour. `publication_eligibility` stays exactly as C27 left it. The normal state for every pilot source is **FETCHABLE + NOT_ELIGIBLE**: we may look, and nothing we see may be published. **A source existing is not a trust signal.** | [ACQUISITION §1](ACQUISITION.md#1-the-correction-this-step-made) |
| **D34** | **A lease that expires is not enough; the fence is a token.** A worker that stalls past its lease, is swept, and then finalises would write an authoritative `fetch_run` and `snapshot` for work it no longer owns — and no comparison of timestamps prevents it, because the clock disagreement is what caused the stall. Every claim mints a fresh `fetch_attempt.lease_token`; every heartbeat and finalisation is conditional on it; the fenced transition is the **first write of the finalisation transaction**, so a lost lease produces no run, no snapshot and no blob row. Exactly one terminal run per attempt has two independent guarantees: the fenced state transition and `uq_fetch_run_attempt_id`. | [ACQUISITION §4](ACQUISITION.md#4-lease-fencing) |
| **D35** | **A 304 is not a 200 that happened to match.** A conditional request answered 304 produces a `fetch_run` with status `UNCHANGED`, `http_status = 304` and `unchanged_content_hash` naming the blob the server confirmed — and **no snapshot**, because no bytes arrived and a snapshot means "we saw these bytes". A 200 returning identical bytes did transfer a body, so it produces a real snapshot sharing the existing blob. A CHECK stops anything but a 304 claiming unchanged content, because a failed fetch asserting "nothing changed" would silently extend the life of evidence nobody re-checked. | [ACQUISITION §7](ACQUISITION.md#7-what-is-preserved) |
| **D36** | **Object storage is written before the transaction that references it.** The two failure modes are not symmetrical. An object nothing references is wasted bytes, reconcilable, harmless. A `content_blob` row pointing at bytes that were never stored is a published fact whose evidence cannot be produced — the outcome C12 exists to prevent. So the recoverable failure is the one we allow, `find_orphans` takes a 24-hour age floor (an object is written seconds before its row), and deletion is an operator's decision rather than an automatic sweep. | [ACQUISITION §7](ACQUISITION.md#7-what-is-preserved) |
| **D37** | **Politeness is a constraint, not a tunable.** 120 hosts serve 319 pages and one host serves eleven. One in-flight request per host, a configurable floor between them, `Retry-After` obeyed as stated. There is no proxy rotation, no user-agent cycling and no CAPTCHA handling anywhere in the acquisition package, and their absence is the design: those exist to defeat a decision the site has made, and a system whose claim is "we only use what the university published" cannot also be one that works around the university saying no. | [ACQUISITION §6](ACQUISITION.md#6-politeness) |
| **D31** | **A physical page and a claimed responsibility are different rows.** One official page legitimately answers for several categories: in the client's file, 66 of 385 URL cells repeat a URL already given under another heading, and 32 of the 35 "additional source" cells repeat a core column outright. Each cell becomes its own `pilot_collected_source` row (one claim), and `duplicate_of_source_ref` marks the repeats, so the rows with NULL there are the distinct pages — 319 of them. A partial unique index enforces one page per URL per institution. Acquisition fetches the pages once; **review happens per claim**, because "this is on the university's domain" and "this is the authority for tuition" are different questions (Step 5A section 14). | [PILOT_COLLECTION §11](PILOT_COLLECTION.md#11-the-final-source-list-step-5a) |
| **D38** | **First contact with the real web is twelve deliberate requests, checked in and repeatable — not 319.** The acquisition stack had only ever met fixtures, and a fixture agrees with whatever the test assumed. The smoke set (`apps/api/smoke/acquisition_smoke_set.toml`) names one page per institution across all six destinations and every structurally unusual case, so the first real run hits twelve hosts once each rather than putting 319 pages in front of 120 universities to find out whether the client was right about the URLs. It is **not business configuration**: nothing reads it at runtime, no scheduler consults it, and the sources it names are exactly as publication-ineligible as the other 307. Twelve pages found three defects (C32–C34); the same three would have been found by 319 pages plus a great deal of traffic we would have had to apologise for. | [ACQUISITION §13](ACQUISITION.md#13-what-the-first-real-web-run-found-step-5b1) |
| **D39** | **"May we fetch this at all?" and "may we fetch it *now*?" are different questions, and one column was answering both.** A single `429` set `fetch_eligibility = 'BLOCKED'`; a resolver timeout took the same path as *"this hostname resolves to loopback"*; and nothing anywhere set that column back, because registration is `ON CONFLICT DO NOTHING` by design. One rate limit or one DNS hiccup during a 319-page run silently dropped a legitimate page for good. Eligibility is now a **durable judgement** — changed by a machine only on evidence of refusal, and otherwise only by an audited human action — while `source.cooldown_until` is **operational timing**, set and cleared automatically and carrying no judgement at all. A cooldown is deliberately *not* a new eligibility member: making it one would force every reader of that column to know that one of its values expires, and leave the scheduler as the only thing able to say whether a source was really blocked. The view answers both questions separately, as `health` and `schedule_state`. | [ACQUISITION §14](ACQUISITION.md#14-recovery-and-cycle-isolation-step-5b2) |
| **D40** | **A `429` is a statement about the host, so the pause applies to the host.** One pilot host serves eleven pages. Honouring a throttle on the page that happened to receive it and then immediately asking for the other ten is not honouring it — it is the behaviour that turns a temporary throttle into a permanent block. `host_cooldown` is one row per hostname, consulted by both the enqueue and the claim. It is a table rather than in-process state because `HostGate` is empty in the next worker process, and a cooldown a worker forgets is not a cooldown. Deliberately the smallest thing that is actually safe: no host scheduler, no priority queue, one timestamp and a reason. | [ACQUISITION §14](ACQUISITION.md#14-recovery-and-cycle-isolation-step-5b2) |
| **D41** | **A cycle key is a filter, not a label.** `run_cycle(cycle_key=K)` passed `K` to the report and nothing else: `claim_next` had no `cycle_key` predicate, so a worker drained every due attempt in the table and attributed them all to `K`. Nothing was mis-recorded — a `Lease` carries the attempt's real cycle, so retries stayed correct — but a smoke cycle and the full pilot run could interleave, and the counts would not add up afterwards. `cycle_key` is now a **required keyword argument** on `claim_next` and a predicate in its SQL. Required rather than defaulted, so that no future call site can quietly drain the wrong cycle; the `UPDATE` still never writes `cycle_key`, so claiming cannot re-label work. | §8.1 |
| **D42** | **One broken page must not end a cycle.** `_process_one` caught only `LeaseLostError`, so any other exception propagated out of `run_cycle`: with 319 pages queued, a defect on page 40 left 279 universities unvisited and produced a traceback instead of a report. Containment now sits at the per-attempt boundary, catching `Exception` — **not** `BaseException`, so `KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` still stop the run, because an unkillable worker is a worse failure than the one being fixed. A contained failure writes a terminal `INTERNAL_ERROR` run if the lease is still ours, and if it is not — the lease expired, or the database is what broke — **nothing is manufactured**: the attempt stays `RUNNING` and the existing sweeper closes it as `ABANDONED`, which is the history that describes what happened. `INTERNAL_ERROR` earns no automatic retry: the cost of a defect in this repository should not be paid in requests to someone else's server. | §8.1 |
| **D43** | **Extraction reuses the `extraction` table; the payload does not go in PostgreSQL.** The table already carried `snapshot_id`, `extractor_name`, `extractor_version`, `status`, an append-only trigger and `field_claim.extraction_id` — the exact lineage Step 5C.2 needs — so revision `c8d9e0f1a2b3` adds five columns rather than a model. The normalised document itself goes to the object store under `document_hash`: for 174 real pages it is 11 MB of data *reproducible from bytes we already store*, and keeping it in PostgreSQL would put derived data in every backup and every replica. `output` keeps a summary. A CHECK requires a stored document to be completely described — hash, key and size together or none — which is C12's dangling-reference failure one plane further down. | [EXTRACTION §2-3](EXTRACTION.md#2-where-it-lives) |
| **D44** | **The derived artifact is a pure function of (input bytes, extractor version), and carries no identifiers.** `canonical_bytes()` gives one byte sequence per document — sorted keys, tight separators — and its sha256 is the artifact hash. No snapshot id, no source id, no timestamp: including them would make the hash depend on *when and where* extraction ran rather than on *what was parsed*, and the determinism requirement would be unverifiable. Lineage lives on the row, where a join can follow it. Two consequences, both intended: identical bytes from two sources produce **one** artifact (175 extractions, 174 artifacts in the real fleet), and idempotency needs no run record — `UNIQUE (snapshot_id, extractor_name, extractor_version)` makes a repeat pass a no-op, while changed logic takes a new version and the old row is retained so extractor drift stays visible (D4). | [EXTRACTION §4-5](EXTRACTION.md#4-determinism) |
| **D45** | **Navigation is kept and labelled, not deleted.** `script` and `style` are dropped because executable code is not content. `nav`, `header`, `footer` and `aside` are preserved, because universities put fee tables in asides and application deadlines in footers — a parser that dropped them because a CSS class looked like navigation would silently lose the thing we came for. Every block instead records the container it came from, so Step 5C.2 can prefer the main column if it wants to; the decision belongs to the step that knows what it is looking for. Charset handling is a fixed four-step ladder rather than a detection library, because `chardet` and `charset-normalizer` guess and guess differently between versions, which would make the artifact hash depend on a dependency's heuristics. | [EXTRACTION §7-8](EXTRACTION.md#7-html-normalisation) |
| **D46** | **Finding a value is not permission to publish it, so there is a second claim plane.** The instruction asked for `field_claim` rows; C27's `field_claim_requires_eligible_evidence` refuses them, because all 319 pilot sources are `NOT_ELIGIBLE` and nobody has verified an official domain yet. Demonstrated by attempting the insert and reading the refusal, not by reading the trigger. The two ways to comply were to weaken C27 or to fake the verification, and both were explicitly forbidden — so `field_claim_candidate` is a separate append-only table with no eligibility gate and no path to publication. This is D33 one plane further up: Step 5B had to separate `FETCHABLE` from `PUBLICATION_ELIGIBLE`; this separates **finding a value** from **permission to publish it**. `test_field_claim_still_refuses_a_pilot_source` attempts the insert every run, so if C27 is ever relaxed this decision fails a test rather than quietly becoming false. | [CLAIMS §1](CLAIMS.md#1-field_claim-could-not-hold-these-claims-and-that-is-c27-working) |
| **D47** | **A claim's identity is the region it came from, never its value.** `fingerprint = sha256(extraction_id, field_kind, locator, extractor, version)`. Including the value would collapse two official pages stating the same fee into one claim, and corroboration — the most useful thing a second source gives you — only exists as two rows. It would also mean a rule change that altered a number produced a brand-new claim rather than a second version of the same one. The consequence is that **the locator is load-bearing**: a locator precise enough to review is the same object as a locator precise enough to be an identity, and two claims sharing one are silently dropped by `ON CONFLICT DO NOTHING` (C45, C46). | [CLAIMS §3-4](CLAIMS.md#3-a-locator-names-the-region-not-the-page) |
| **D48** | **Each rule carries its own version, and superseded rows are kept.** Six extractors initially shared three version constants, so correcting the calendar locator would have relabelled 81 unchanged deadline claims as a new version — the opposite of what versioning is for. Every extractor now has its own constant, and the reports derive "current" from the runner's registry rather than a written-out list, so a bump cannot leave a query quietly counting old rows. Superseded claims are retained because the table is append-only and because comparing two versions over one document is how rule drift becomes visible rather than being mistaken for a source change (D4). After the audit-and-correct cycle the table holds 5,977 rows, 1,941 of them current: that ratio is what auditing looks like when nothing is overwritten. | [CLAIMS §10](CLAIMS.md#10-rule-versions-and-why-there-are-several) |
| **D49** | **Extraction retry is a partial index, not a new table.** Step 5C.1's `UNIQUE (snapshot_id, extractor_name, extractor_version)` made `extractor_version` mean two things at once — "the logic changed" and "the filesystem was briefly unavailable" — so a transient failure could only be retried by lying about the first. The uniqueness is now partial over non-`FAILED` rows: one result per (snapshot, extractor, version) is still enforced, a failure does not occupy that slot, and failures accumulate as history. No `extraction_attempt` table: the acquisition plane has one because a *fetch* has a lease, a heartbeat and a worker that can die mid-request, while extraction is a pure function over bytes already held. `already_extracted` uses the identical predicate, because a check that counted failures where the index did not would skip the retry and leave the slot empty — a page that could be extracted, permanently reported as done. | [CLAIMS §11](CLAIMS.md#11-extraction-retry-separately) |
| **D50** | **A review decision holds what a human decided and nothing else.** The instruction suggested one state set: `UNREVIEWED | ACCEPTED | REJECTED | NEEDS_CONTEXT | NEEDS_SCOPE_MAPPING | SOURCE_NOT_VERIFIED | SUPERSEDED`. Three of those are not decisions -- whether a candidate is superseded, whether its source has been verified and whether its scope resolved are all things a machine knows at any moment. Collapsing them into the human's decision would make one column answer two questions, which is the mistake D39 corrected for `fetch_eligibility`, and it would mask a reviewer's judgement behind a fact about the source -- the opposite of what section 30 asks for. So `field_claim_candidate_review` stores four decisions and `candidate_review_state` exposes `is_superseded`, `source_not_verified` and `scope_unresolved` as separate columns beside them. A reviewer who accepts a candidate from an unverified source has made a real judgement, and it survives. The existing `review_task`/`review_decision` chain could not be reused: both are anchored by NOT NULL foreign keys terminating at `change_proposal`, and `review_decision_kind` (`APPROVE | RETURN | CORRECT`) has no member for `REJECTED`. | [CANDIDATE_REVIEW §2-3](CANDIDATE_REVIEW.md#3-the-review-model) |
| **D51** | **Group by context, compare by value -- and gate conflicts, not agreement.** One key cannot answer both *what is this claim about* and *what does it say*, so there are two: a context key (institution, field, applicant scope, programme, period, plus the test for a language claim and the round for a deadline) and a value key. Section 5's prohibition on grouping by value alone becomes structural rather than a rule to remember, and its three worked examples fall out without special cases. A key component meaning "we do not know" is a **sentinel**, and the sentinel gate applies to conflicts and not to agreement: a conflict asserts that two sources answered the *same question* differently and a key of unknowns has not established that, while agreement verifies nothing (section 20) and two pages agreeing about an unscoped fee is still corroboration -- reported with `context_is_thin` set. Without the gate, a fee table's ten budget line items read as one ten-way disagreement; with it on both sides, `AGREES` became unreachable. Nothing is stored: a group is a pure function of the current candidates, and a stored row would be a cache nothing invalidates. | [CANDIDATE_REVIEW §6](CANDIDATE_REVIEW.md#6-grouping-two-keys-not-one) |
| **D52** | **Two field kinds are never grouped for agreement, because they carry no identity.** An `ADMISSION_REQUIREMENT`'s value is prose. An `ACADEMIC_CALENDAR_EVENT`'s identity is the event's label, and the stored evidence is a 160-character window around the date that routinely spans two or three adjacent entries -- grouping Cornell's calendar by (institution, year) put eight of its 2027 events in one group and reported them as eight competing values. `__never__` is therefore a first-class answer, not a gap: inventing a key for either would produce agreement and conflict numbers that mean nothing. 74% of the corpus is `INSUFFICIENT_CONTEXT` and the honest headline is that **this corpus contains essentially no corroboration** -- 0 `AGREES` groups, 0 confirmed conflicts -- so Step 5C.4 should not assume a voting or merge mechanism will help. | [CANDIDATE_REVIEW §6](CANDIDATE_REVIEW.md#fields-that-are-never-grouped) |
| **D53** | **The live rule versions live in a table the code rewrites, not in a view.** Revision `e0f1a2b3c4d5` froze the current `(extractor, version)` pairs into `candidate_review_state`. Four rule corrections later the view reported **every current candidate as superseded** -- silently, with a total that happened to look plausible -- and the cost of being wrong there is a review queue built entirely from history. `claim_rule_version` holds one row per extractor and the claim runner rewrites it from `EXTRACTORS` on every pass, so the code stays the source of truth and publishes what it knows to SQL. It is mutable on purpose: "which version is current" has one answer at a time, and the history of which versions existed is already append-only in `field_claim_candidate.extractor_version`. 6,129 superseded rows now sit beside 1,936 current ones. | [CANDIDATE_REVIEW §5](CANDIDATE_REVIEW.md#5-what-current-means-and-why-one-frozen-list-was-not-enough) |
| **D54** | **Priority is a sum of named signed factors, and every one that applied comes back with the score.** Section 27 forbids a black-box score, so there is no weight that cannot be read off the row: +40 for a high-risk field, +25/+15/-20 for the confidence band, +20 corroborated, +10 body-confirmed, +5 has evidence, -10 body unconfirmed, -15 in conflict, -20 scope unresolved, -25 thin source. LOW candidates are stored, kept and never deleted, and stay out of the primary queue **unless nothing better exists for that field on that page** -- 690 of 1,936 are the only answer their page gives, which is section 28's stated exception rather than a loophole. Confidence never implies a decision: an unreviewed HIGH candidate is blocked and an accepted LOW one is not. | [CANDIDATE_REVIEW §14](CANDIDATE_REVIEW.md#14-review-priority-and-queues) |
| **D55** | **"Current" has two independent axes: the rule version and the document artifact version.** Re-extracting the fleet at document `2.0.0` kept the `1.0.0` artifacts, so every snapshot has two extractions and a candidate can be stale in two unrelated ways. The obvious fix -- bump every rule version -- was measured and rejected: running each rule's current code over both artifacts of all 175 dual-artifact snapshots, the **calendar, language, tuition and deadline rules produced byte-identical statements** (242->242, 113->113, 40->40, 77->77 rows; 0 added, 0 removed, 1 re-worded). Bumping them would have marked 472 correct claims superseded in order to express a fact about the parser. So `document_artifact_version` records which parse is live, `claim_rule_version` records which rule is, and `candidate_review_state` exposes `rule_superseded` and `document_superseded` separately with `is_superseded` as their combination. One column must not answer two questions (D39). The 472 rows that are document-superseded-but-rule-current are the arithmetic proof that the axes are genuinely independent. | [CANDIDATE_REVIEW §5](CANDIDATE_REVIEW.md#5-what-current-means-and-why-one-frozen-list-was-not-enough) |
| **D56** | **A review decision is surfaced as stranded, never transferred to its replacement.** The one recorded decision -- `ACCEPTED` on a `$13,545` tuition figure -- is now `review_superseded`, because the page was re-parsed even though the tuition rule did not change. It is not copied to the candidate that replaced it. Two candidates whose normalized values match are not the same observation: a reviewer who accepted one has not looked at the other, and inferring equivalence from equal values is how a human judgement gets attributed to evidence nobody read. `decision_state` still says `ACCEPTED` on the row where it was decided; the live corpus is 1,399 `UNREVIEWED`. | [CANDIDATE_REVIEW §11](CANDIDATE_REVIEW.md#11-supersession-and-stranded-decisions) |
| **D57** | **The runtime-role privilege tests provision their own credentials rather than skipping.** Fifteen tests that exercise `app_api`, `app_worker` and `app_publisher` skipped whenever `APP_*_PASSWORD` was unset, which on a machine with no `.env` is always -- and `pg_authid` holds only a SCRAM verifier, so the plaintext is unrecoverable. The suite now generates one password per role per run against a loopback database using the owner connection, authenticates as the real roles over the same `scram-sha-256` path production uses, and restores each original verifier byte-for-byte afterwards (a stored verifier can be re-applied verbatim). `SET ROLE` was rejected because it reproduces privileges without authentication and would not detect a role that cannot log in at all; a member role was rejected because `NOINHERIT` means a member's privileges are not the role's; catalog assertions were rejected because a grant that is written is not a grant the server enforces. **0 privilege tests skipped**, from 15. | [ARCHITECTURE §9](#9-change-detection-review-security) |
| **D58** | **The step runs in MODE B: proposals, not decisions.** The verification machinery has existed since revision `b0c1d2e3f4a5` -- `verify_domain`, `authorize_external_domain`, `reject_domain`, `replace_domain`, `mark_domain_legacy`, `verify_source_mapping`, `verify_candidate`, `classify_candidate`, each demanding an `Actor` and a reason -- so the reason for MODE B is not that a decision could not be recorded. There is exactly one `app_user` row, created in Step 5C.3 to attribute one review, with no `password_hash` and no `external_subject`. Writing 129 domain and 385 responsibility decisions under it would record that a person inspected 514 things they have not seen: the identity would be real and the judgement would not, which is the disguise section 10 prohibits. So the step persists `domain_decisions_proposed`, `source_decisions_proposed` and `responsibility_decisions_proposed`, every row `NEEDS_REVIEW`, and stops. Promotion readiness is consequently 0, which is the correct answer rather than a shortfall. | [SOURCE_VERIFICATION §1](SOURCE_VERIFICATION.md#1-mode-b-and-why) |
| **D59** | **Withdrawing trust stops future publication; it deletes nothing.** `source.publication_eligibility` is a stored copy of a decision two tables away, checked by `app_source_eligibility_is_earned` at the moment it is set and never again. Rejecting a host therefore stopped nothing: every source under it still read `OFFICIAL_VERIFIED`, and C27 reads exactly that column. Two `AFTER UPDATE` triggers now set dependent sources back to `NOT_ELIGIBLE` when a domain or a mapping stops being trusted or active. They touch no evidence, no `field_claim`, no `field_provenance` and no `change_event` -- facts already published stay, visibly sourced from a host whose verification was later withdrawn, which is what an auditor needs. Rewriting them would destroy the record of what was believed and when. | [SOURCE_VERIFICATION §5](SOURCE_VERIFICATION.md#5-revocation) |
| **D60** | **A replaced URL is a new source, linked, never an edited old one.** `source.superseded_by_source_id` records that one source replaced another. The old row keeps its URL, its `url_hash` and every fetch it ever made, because its snapshots were fetched from the old URL and repointing it would make the evidence say it came from somewhere it did not. A source may be superseded only once inactive: a replaced source still being fetched is two sources for one thing. No replacement URL is discovered at this stage -- that is a reviewer's input, not a crawl. | [SOURCE_VERIFICATION §6](SOURCE_VERIFICATION.md#6-replacing-a-dead-url) |
| **D61** | **A jurisdiction scope requires applicant or qualification semantics, never a bare country token.** Of twelve jurisdiction scopes on the real corpus, three were right; the rest were a subject ("Chinese medicine"), a campus location ("Beijing, China"), a list of campus cities, an institution's own name ("NTU Singapore"), a postal address ("Cambridge, MA 02138 USA") and three runs of navigation text -- every one of them reading as *resolved*. A blocklist would have fixed two strings and left the next address to walk through, so the rule is an allowlist of five named constructions: `DEMONYM`, `APPLICANT_ORIGIN`, `QUALIFICATION_ORIGIN`, `CONDITIONAL_ORIGIN`, `NATIONAL_QUALIFICATION`. The construction that carried the scope is recorded on the row, because an explicit reason is reviewable and a score is not. Negation is detected and the marker dropped rather than inverted: "non-US citizens" is not a scope, and guessing which jurisdictions it leaves would be an inference the page never made. | [CLAIMS §13](CLAIMS.md#13-applicant-scope) |
| **D62** | **Jurisdiction and qualification system are two dimensions, carried separately.** They were distinguishable only by `applicant_country_code` being null, which is an accident of the data rather than a statement about it. Every marker now carries an explicit `dimension`, and a candidate carries `applicant_jurisdictions` and `qualification_systems` beside the combined `applicant_scopes`. A requirement with a qualification hint and no jurisdiction says `APPLICANT_JURISDICTION_UNRESOLVED` rather than being lumped in with the 804 that state no scope at all: knowing an applicant holds A-levels does not say where they are from, and one flag covering both would hide which is missing. Nothing is ever mapped to `UNIVERSAL`. | [CLAIMS §13](CLAIMS.md#13-applicant-scope) |
| **D63** | **Publication authority is scoped to a field, not to a source.** C27 checked `AUTHORIZED_RANKING` against entity types and `AUTHORIZED_EXTERNAL` against an exact `(entity_type, field_path)` binding, and checked `OFFICIAL_VERIFIED` -- the class every university page will hold -- at source level only. The binding requirement now applies to both official and authorised-external sources, which is symmetry rather than a new mechanism: `source_field_binding` already existed and already did this job for one class. The consequence is that an eligible source publishes nothing until its bindings exist, and they are written by promotion from the verified mapping's `source_category`. | [SOURCE_VERIFICATION §9](SOURCE_VERIFICATION.md#9-authority-is-scoped-to-a-field) |
| **D64** | **Publication authority is its own table, tied to extraction authority by an invariant.** `FIELD_AUTHORITY` maps a candidate field kind to the responsibilities that may publish it. Reusing `claims.model.ROUTING` was tempting and wrong: "may this rule read this page?" and "may this page publish this fact?" are different questions, and widening extraction to improve recall would have silently widened publication. What connects them is an assertion instead -- publication authority must be a **subset** of extraction authority, because a page nobody was allowed to read cannot be one something is published from. The invariant immediately caught a mistake in the first draft: `APPLICATION_DEADLINE` had been granted to `ENTRY_REQUIREMENTS`, which `ROUTING` does not even let the deadline extractor read, so the grant authorised something that could not exist. | [CLAIMS §14](CLAIMS.md#14-publication-authority) |
| **D65** | **Promotion is explicit, and it consumes trust rather than creating it.** `verification.promotion.promote` performs ten checks and repairs none of them: a missing domain verification or responsibility decision raises `NotYetTrustedError`, whose remedy is a person, not a retry. It is a separate act from verification on purpose -- verification says *this page is what it claims to be* and promotion says *and we are now going to rely on it* -- which makes the second observable, testable and separately revocable. Idempotent: a repeat returns the current state and writes no audit row, because repeating a request is not a second decision. | [SOURCE_VERIFICATION §10](SOURCE_VERIFICATION.md#10-promotion) |
| **D66** | **Verification gets a permission, not a second role.** `role.reviewer` already exists with `is_reviewer_role = true`. The new `source:verify` permission is deliberately not `source:manage`, which reads "register and configure sources" and is held by `data_editor`: adding a URL and declaring that URL officially the institution's are different acts, and the second is what the whole C27 boundary rests on. Identities are provisioned by an operator who supplies the email and display name; nothing is defaulted, a reserved test domain is refused unless the identity is explicitly marked `[TEST ONLY]`, and no password is set because there is nowhere yet to sign in. | [SOURCE_VERIFICATION §11](SOURCE_VERIFICATION.md#11-who-may-decide) |
| **D67** | **An approved manifest carries a content hash, and applying a different one is refused.** The failure this prevents is specific and silent: a reviewer reads manifest A, the generator is re-run producing manifest B, and the apply command applies B. Each manifest is wrapped in an envelope carrying `content_sha256` over its rows, checked against itself on load and against `--expect-sha256` when the operator names the version they approved. A row with no `decision` is malformed and aborts the file; it is never read as `VERIFIED`. | [SOURCE_VERIFICATION §12](SOURCE_VERIFICATION.md#12-batch-application) |
| **D68** | **A write is authenticated; the actor comes from the credential, never from an argument.** `--actor <uuid>` established attribution and nothing else -- anyone with CLI access could type Dejan's id and the audit log would name a person who had not decided. The argument is **removed** from every authenticated write rather than retained and compared, because an argument that must equal the session is one that will eventually be trusted without the comparison. Authentication reuses what the model already documented: `AppUser.password_hash` is Argon2id, the local fallback to the preferred OIDC path, and `argon2-cffi` implements the algorithm the column comment has named since the identity module was written. The password is read from a TTY or `DATAHUB_REVIEWER_PASSWORD`, never an argument, because arguments leak through shell history, `ps` and logs. No token is issued and `user_session` is untouched: those rows belong to the Next.js BFF, and a second, weaker way in is not an improvement. | [SOURCE_VERIFICATION §15](SOURCE_VERIFICATION.md#15-authentication) |
| **D69** | **The reviewer role no longer carries `proposal:publish`, and no role does.** The architecture's separation was "you may not publish your own work", re-validated per proposal at publication time (C2/D7) -- which confirms the permission *is* the publication authorisation, gated by a same-actor check. Step 5C.7B prefers the stronger form: the role that reviews does not hold the authority to publish at all. There is no distinct application publisher role to move it to, so it is assigned to nobody and the permission row is kept. Nothing breaks today -- `change_proposal` is empty and the publication transaction is unbuilt -- and when it is built, somebody chooses an owner with a single insert. `proposal:create` stays with the reviewer: a proposal is a *request* for governed publication, not publication. The database role `app_publisher` is untouched and remains the only role that may write canonical tables. | [SOURCE_VERIFICATION §16](SOURCE_VERIFICATION.md#16-review-is-not-publication) |
| **D70** | **Granting a role requires an authenticated administrator, and the bootstrap escape closes itself.** `authorize_grant` permits an unauthenticated grant only while the database holds no active identity carrying `admin:roles` -- that is, only when nobody exists who *could* have authorised it. The moment one does, unauthenticated provisioning is refused, and there is no flag to re-open it: a permanent escape hatch would be the same hole as an unauthenticated `--actor`. An authorised grant appends `ROLE_GRANTED` to the audit chain; a bootstrap grant does not, because filing it under a machine actor would put a fabricated approval in the chain. Dejan's own provisioning is that bootstrap case and is documented rather than back-filled. | [SOURCE_VERIFICATION §17](SOURCE_VERIFICATION.md#17-provisioning-and-the-bootstrap-row) |
| **D32** | **A page with no stated category imports as `UNCLASSIFIED`, not as a guess.** The "Important Additional Source" column says the collector thought a page mattered; it does not say what the page is. Three of the client's are genuinely new and one is a fee-rates table, so labelling them `OFFICIAL_PDF` because of the column heading would have been inventing a claim. `UNCLASSIFIED` is deliberately not a `SourceCategory` member, so no canonical row can carry it, and a CHECK lets such a page be *rejected* but never *verified* — verification asserts a page is authoritative for something. Classification is its own recorded decision. | [PILOT_COLLECTION §11](PILOT_COLLECTION.md#11-the-final-source-list-step-5a) |

| # | Correction | Effect |
|---|---|---|
| **C22** | **Two more migrations were reading live application state (C21 again).** Revision 0002 imported `enum_ddl_specs()` and revision 0012 asserted its index names equalled the live `MIGRATION_OWNED_INDEXES`. Adding Step 4 enums and indexes therefore broke **fresh** database builds while already-upgraded ones stayed green — the fault was invisible except on a clean build. Both lists are now frozen literals; completeness is asserted against the built schema by tests instead (`test_enum_members_match_the_python_enums`, `test_migration_owned_indexes_exist_and_are_complete`). | §8.1, §8.3 |
| **C23** | **`FetchStatus` was missing `ABANDONED`.** Revision 0015 added the member to the PostgreSQL type; the Python enum never gained it, so reading back an abandoned fetch would have failed to coerce. The enum test compared type *names* only. It now compares members, in both directions. | §9.3 |
| **C24** | **`Faculty.parent`/`children` needed explicit `foreign_keys`.** The composite containment FK `(parent_faculty_id, university_id) -> (id, university_id)` made SQLAlchemy infer that the hierarchy relationships also write `faculty.university_id`, which belongs to `university`/`faculties`. `configure_mappers()` warned; under `filterwarnings = error` the warning became an exception and poisoned the registry for the process. Latent since Step 3 because the integration tests speak raw SQL — the target-list importer was the first code to use the ORM, so the failure surfaced as thirty errors in an unrelated module. A unit test now configures the mappers under `-W error`. | §4.4 |
| **C25** | **A `NULL` JSONB must be SQL `NULL`, not the JSON literal `null`.** SQLAlchemy's `JSONB` maps Python `None` to JSON `null` by default, which is a perfectly good non-NULL value — so `target_list_diff`'s CHECK requiring `before_value IS NULL` for an addition failed on every row. More generally, storing an absence as JSON `null` makes it indistinguishable from a source that published null, which is the distinction `field_status` exists to preserve. The onboarding JSONB columns use `none_as_null=True`; the emitted DDL is unchanged. | [ONBOARDING §5](ONBOARDING.md#5-establishing-official-identity), §8.1 |
| **C26** | **DELETE is granted where deleting loses no fact, and that list is now explicit.** `source_degree_scope` and `source_discipline_scope` are membership sets — which audiences and disciplines a mapped page serves is current configuration, and a wrongly ticked audience must be un-tickable without discarding the mapping's verification. The privilege test now carries a justification per grant rather than an allow-list of two, plus a separate class-level assertion that no immutable or canonical table is deletable. | §8.2 |
| **C27** | **Role separation does not prevent a spreadsheet value becoming a published fact, and Step 4 claimed it did.** `app_publisher` holds `SELECT` on the onboarding tables and `INSERT`/`UPDATE` on the canonical ones, so one identity sufficed; verified live, it could also insert a `university` with no provenance at all and a `PUBLISHED` `field_provenance` citing no evidence. Grants classify tables, but this rule is about a value's *origin*. Replaced with an explicit `publication_eligibility` class on every `source` (`TARGET_SCOPE_ONLY` / `OFFICIAL_VERIFIED` / `AUTHORIZED_EXTERNAL` / `AUTHORIZED_RANKING` / `NOT_ELIGIBLE`, default closed), a **generated** derivation on `source_mapping` that no role can write, `source_eligibility_is_earned` so a class must be earned by promoting a verified mapping at a matching URL, gates on `field_claim` and `field_provenance`, a CHECK that any status but `NOT_CHECKED` cites something, and eight governed canonical columns requiring matching published provenance at commit. `app_publisher` additionally loses `SELECT` on the onboarding plane, the coverage view and `audit_log`. **Honest limits, recorded because the last claim was overstated:** value fidelity is not enforceable — citing a genuinely official source for a hand-copied string is indistinguishable from honest work, so the Step 9 publication transaction must compare each published value against the cited claim; and provenance is mandatory for eight columns, not the whole canonical plane. | [ONBOARDING §11](ONBOARDING.md#11-publication-eligibility-c27) |
| **C28** | **Dropping a governed column does not update the trigger that governs it.** C27 froze its column list into revision 0017 as a literal, including `tuition.amount`. U14 dropped that column — and nothing in PostgreSQL revises a trigger's `TG_ARGV`. The trigger would have kept firing on every tuition write, looked up a key no longer present in `to_jsonb(NEW)`, taken the "a NULL column asserts nothing" branch and passed: tuition would have left the governed set silently, with no error anywhere. Revision 0019 drops and recreates the trigger over `(amount_kind, amount_min, amount_max)`, and a test reads the live `TG_ARGV` back — a frozen literal in an old migration is correct history, not a description of now, and something has to notice when the two diverge. | §8.1 |
| **C29** | **`UNIQUE (submission_id, url_sha256)` asserted one URL = one source, and the client's real file disproves it 66 times.** The constraint was written before any real source list existed, from the reasonable-sounding idea that a URL identifies a source. It does — but a *source responsibility* is a URL plus a category, and one admissions page answers for undergraduate admissions, entry requirements and application deadlines at once. Importing the file under that constraint would have failed outright or silently discarded 66 claims. Replaced by the D31 split. The general lesson is the one this project keeps relearning: a uniqueness rule inferred from a model rather than from data encodes an assumption nobody checked. | §8.1 |
| **C30** | **`ipaddress.is_private` is not the test for "safe to connect to", and using it would have let carrier-NAT space through.** On Python 3.12, `100.64.0.0/10` (RFC 6598) reports `is_private = False` and `is_global = False`: it is not private, and it is not routable either. The SSRF classifier now gates on `is_global` and uses the specific predicates only to produce a readable reason, which also covers ranges a future RFC adds. Separately, IPv4-mapped, 6to4 and Teredo IPv6 addresses carry a v4 address the v6 predicates do not inspect, so each is unwrapped and the inner address classified — `::ffff:127.0.0.1` is not loopback to `ipaddress`, but it reaches loopback. | [ACQUISITION §5](ACQUISITION.md#5-ssrf-and-the-limit-we-are-not-hiding) |
| **C31** | **`httpx.Response.is_redirect` is true for 304.** It tests the status class, not the presence of a `Location` header, so a conditional request answered 304 was classified as a redirect with no destination and recorded as an HTTP error. Caught by the first conditional-request test rather than in production, which is the argument for testing transport behaviour against a real socket instead of a mocked client: a mock would have returned whatever the test assumed. The redirect branch now names the statuses it handles. | §8.1 |
| **C32** | **A 200 with a body is not evidence; NUS returned an Imperva interstitial and we stored it.** The first real run fetched the NUS academic calendar, got HTTP 200 and 212 bytes, hashed them, wrote a blob and a snapshot, and marked the source `HEALTHY`. The bytes were an `_Incapsula_Resource` challenge with an empty `<body>` — a WAF page wearing a success status. Nothing downstream could have told it from a calendar except by parsing it, which is exactly the point at which a wrong fact becomes a published fact. The fetcher now recognises a challenge interstitial (HTML, under 8 KiB, containing a known marker), returns `BLOCKED` with `error_class = "ChallengeInterstitial"`, and **discards the bytes** rather than preserving them as evidence. The size bound is what keeps it honest: a real page that merely mentions Cloudflare is not a challenge, and a test asserts that. Detection is not circumvention — a blocked source stops being scheduled and waits for a person (D6). | [ACQUISITION §13](ACQUISITION.md#13-what-the-first-real-web-run-found-step-5b1) |
| **C33** | **A fetch that redirected and then failed lost every hop.** `effective_url` and `redirect_chain` lived only on `snapshot`, and a snapshot is written only when bytes arrive. UBC's calendar redirects four times and the connect on a later hop timed out; the record said `TIMEOUT, http_status 301` and nothing about where it had been going — which is the one thing that separates "the site moved" from "the site is down". The chain is a fact about the **attempt**, not about the bytes, so revision `f4a5b6c7d8e9` adds both columns to `fetch_run` as well. Keeping them on `snapshot` too is not duplication: a snapshot must be able to say which URL produced *these* bytes without joining back through the run. | §8.1 |
| **C34** | **A 404 was retried like a timeout.** PolyU's tuition URL is dead, and the runner scheduled a retry after each failure exactly as it would for a transient error — four requests to a page that will never exist, and in a full cycle that becomes a repeated knock on a university's door for a page they deleted. Retries now stop at `404` and `410`: a permanent status is a finding for the reviewer (the URL in the client's workbook is wrong), not a condition that improves by asking again. `429`, `5xx` and timeouts still back off and retry. | §8.1 |
| **C35** | **A report assembled from scrollback stated two counts that the database contradicted.** The Step 5B.1 write-up said source health was `HEALTHY 9 / BLOCKED 2` while also saying NUS was blocked — which cannot both be true — and said the run produced "8 `text/html` + 1 `application/pdf`" when only eight pages held evidence at all. Both errors were the same error: the numbers were read off terminal output from an **earlier cycle**, captured before C32 landed, and the development database had since been purged to keep the test suite green, so nothing could contradict them. The health model was correct throughout; only the prose was wrong. Two things changed. `acquisition_smoke.py audit` now prints every count straight from SQL with each **grain** named — sources, snapshots, distinct blobs, and responses observed-but-discarded — because the underlying mistake was merging grains: a challenge response is observed and not stored, and an unchanged re-fetch is an observation without a body. And four regression tests now assert the counts the prose claimed, including that a challenged source reports `BLOCKED` **health** (not merely `fetch_eligibility`) even when it holds an earlier body. The general lesson: a number in a report should be a query someone else can re-run, and evidence that is purged cannot audit the claims made about it. | [ACQUISITION §13](ACQUISITION.md#13-what-the-first-real-web-run-found-step-5b1) |
| **C36** | **"Redirected off host" counted any redirect at all, and counted occasions instead of pages.** The operational summary asked `SELECT count(*) FROM snapshot WHERE effective_url IS NOT NULL` — but `effective_url` is written whenever the final URL differs from the requested one **as a string**, so an `http`→`https` upgrade, a trailing slash or an `/index.html` canonicalisation all set it. The count an operator reads as *"a university sent us somewhere else"* was therefore inflated by redirects that never left the host, and being over `snapshot` it grew with every cycle: one page redirecting on three cycles contributed three. The `--moved` filter in `assist` used the same predicate, so `report` and `assist --moved` disagreed with each other — the smoke set listed four "moved" pages when three had actually changed host. The correct predicate already existed twice in the codebase (`effective_host_differs` in the fetcher, `host_differs` in the assist rows) and the summary used neither, because `effective_host_differs` is only logged and never persisted. Both call sites now share one SQL host expression, compare hosts rather than URLs, and count `DISTINCT source_id`; the field is renamed `pages_redirecting_off_host` so its grain is in its name (C35). A test asserts the SQL and Python host functions agree, because one definition with two implementations drifts silently. | §8.1 |
| **C37** | **The operational summary counted a pilot numerator against whole-database totals, and counted view rows as pages.** Two grain errors in one sentence, found by auditing the code that produced C35's wrong numbers rather than by a failing test. First: `physical_pages`, `responsibility_claims`, `hosts` and every eligibility count honoured `pilot_only`, while `total_runs`, `snapshots`, `blobs` and the off-host count were plain global `count(*)`s — so `report` rendered a filtered population against unfiltered evidence as though both described one fleet. Second: those page counts were `count(*) FROM acquisition_target`, and that view is one row per page **per institution** — a URL's uniqueness is enforced per institution (D31) while `source` is unique on `url_hash` globally, so a page two universities both cite becomes two rows and one source, and `sum(claim_count)` over it counts each of that page's claims twice. The client's current file is 1:1 (319 sources, 319 non-duplicate physical rows), so this was **latent, not wrong** — and latent means wrong the moment two universities cite one government fee table, which is a normal thing for two universities to do. Everything is now scoped to one population, pages are `DISTINCT source_id`, and `blobs` is `count(DISTINCT content_hash)` reachable from that population's snapshots rather than the row count of a globally shared table. | §8.1 |
| **C38** | **Re-enabling a source did not make it schedulable, because `access_state` stayed `BLOCKED`.** The recovery path this step exists to provide set `fetch_eligibility = 'FETCHABLE'` and stopped there — so a source refused with `403` came back eligible and *still* reported `BLOCKED`, because `source_health` reads `access_state` too. The operator's re-enable appeared to do nothing. Found by the test written for the feature in the same commit, which is the argument for asserting the consequence rather than the column: `fetch_eligibility == 'FETCHABLE'` passed while the thing the operator wanted did not happen. `reenable` now returns `access_state` to `OK` as well, which is what the operator is asserting when they act. The same review found `claim_next` filtering `fetch_eligibility` but **not** `access_state`, while `enqueue_cycle` and the health view both honour it — so a worker could have claimed a page the scheduler and the operator's own report agreed was blocked. Both now read the same state. | [ACQUISITION §14](ACQUISITION.md#14-recovery-and-cycle-isolation-step-5b2) |
| **C39** | **A garbage-collection warning was failing an arbitrary test, and the suite's own gate was the mechanism.** `filterwarnings = ["error"]` is load-bearing — it is what turned a SQLAlchemy relationship misconfiguration into a failing test rather than a log line (C24). But `ResourceWarning` is raised by the **collector**, not by the code that leaked, so the interpreter attributes it to whichever test happens to be executing when collection runs and pytest fails that one. Measured over seven full runs of the Step 5B.2 suite: three failed, each time on a different and entirely innocent test (once a WAF-challenge test, three times an eight-thread audit-chain test), never reproducibly, and never in isolation — 17 of 17 passes when run alone. With `ResourceWarning` and its `PytestUnraisableExceptionWarning` wrapper demoted to warnings, five of five full runs passed. A gate that fails two runs in five, and blames a different test each time, is worse than no gate: it teaches people to re-run until green. **Not a product defect** — the fetcher closes its client with `async with` and its responses with `aclose()`; the leak is test scaffolding, loopback fixture-server sockets plus one `ProactorEventLoop` per `asyncio.run` on Windows. Those two classes are now `default::`, every other warning is still an error, and the leak is still worth closing at the source. | §6.2 |
| **C40** | **The 12-page smoke set said "no JSON-LD anywhere"; across 169 pages, 50 have it.** Step 5B.1 concluded that a structured-data parser had nothing to gain, from a sample of nine stored HTML bodies none of which carried JSON-LD. The full cycle stored 169, and **50 carry a JSON-LD block** — a third of the fleet. The conclusion was not wrong about its sample; it was over-generalised from twelve pages chosen for *technical* variety, which is a different thing from being representative of structure. A structured-data path is now worth building for Step 5C as an accelerator, not a replacement, since two thirds of pages still need HTML parsing. The lesson is narrower than "sample more": a sample chosen to cover known edge cases will not tell you the distribution of the ordinary cases. | [ACQUISITION §15](ACQUISITION.md#15-the-first-full-pilot-cycle-step-5b3) |
| **C41** | **A page count and a run count were added together, and the sum was stated wrong anyway.** The Step 5B.3 report gave "74 source-review pages — 61 dead URLs, 2 NXDOMAIN, 7 TLS failures, 5 HTTP 202/other". Two errors compounded: the `5` is a count of *runs* (two pages, retried) where the other three terms are counts of *pages*, and 61+2+7+5 is 75 rather than the 74 stated, so the figure did not even match its own breakdown. Queried from the persisted cycle the answer is **72** — 61 dead, 7 TLS, 2 NXDOMAIN, 2 HTTP 202 — and the categories do not overlap, because each page has exactly one `last_status`. The cause was upstream in the reporting code: `SOURCE_REVIEW_REQUIRED` was one coarse bucket of 11, coarse enough that restating it in prose required re-deriving the parts by hand. The buckets are now named separately, they are asserted to partition their population, and the report prints the sum so a mismatch is visible in the output rather than in a later paragraph. | §8.1 |
| **C42** | **Two reporting buckets overlapped, and the overlap was double-counted.** The same report accounted for `HTTP_ERROR = 88` as "61 × 404 + 5 × 202 + 21 TLS = 87" and could not find the 88th. The real breakdown is 61 × 404, 5 × 202 and **22** `ConnectError`: 21 TLS certificate failures over 7 pages, plus one bare transport error on `admissions.northwestern.edu` that **succeeded on retry** — so that page has evidence and is not a failure at all. Separately, the DNS section's "transport error" bucket matched `error_class LIKE 'Connect%'`, which catches `ConnectTimeout` as well as `ConnectError`, so its 25 silently included the 3 connect timeouts already counted under `TIMEOUT`. Both buckets are now mutually exclusive by predicate, and a regression test asserts that the corrected pair covers exactly what the old overlapping one did, once each. A list of counts that does not partition its population invites double-counting by whoever reads it. | §8.1 |
| **C43** | **A regex text-length estimate said 33 characters where a real parser finds 3,320.** Step 5B.3 classified five pages `POSSIBLE_BROWSER_REQUIRED` from a crude regex measure of visible text, and McGill's homepage was one of them. The deterministic parser recovers **3,320 characters across 33 blocks** from the same stored bytes: the page never needed a browser, the estimate needed a parser. It also reported "4 of the 5 contain embedded JSON" when the three Chicago pages carry **JSON-LD**, not a serialised data payload — and their JSON-LD holds only title and description metadata, so it does not substitute for the missing prose either. The corrected count of pages that plausibly need rendering is **5 of 175**, on different evidence and with a different membership. A structural estimate is worth having and is not worth quoting as a measurement. | [EXTRACTION §12](EXTRACTION.md#12-commands) |
| **C44** | **Two extractors read the whole paragraph once per test, so TOEFL inherited IELTS's score.** A page reading "IELTS 7.0 overall ... We also accept TOEFL iBT with a minimum of 100" produced a TOEFL requirement of **7.0**, because the rule ran the entire block once for each test name it found and took the first plausible number. A wrong entry requirement is the most damaging thing this extractor can produce: it sends a student to apply for something they cannot get. Attribution now reads what the wording *binds* — a short gap containing no coordination, no other number and no other test name — and falls back to nearest-by-position only when nothing binds, recording which of the two happened on the claim and never giving a positional attribution a HIGH band. Distance alone is not enough, and the failing case proves it: in "7.0 in IELTS or 100 in TOEFL" the 100 sits four characters after "IELTS" and seven before "TOEFL", so the nearest name is the wrong one. | [CLAIMS §6](CLAIMS.md#6-the-refusals) |
| **C45** | **Every claim from one block shared one locator, so three of every ten were silently discarded.** Candidates were located to the block, and the fingerprint is built from the locator — so two `LANGUAGE_TEST` claims (IELTS, TOEFL) and two component scores (Writing, Speaking) from one paragraph hashed identically, and `ON CONFLICT DO NOTHING` dropped the duplicates. **The pass reported success.** It was found only because a run that produced 2,156 candidates left 2,122 rows, and the arithmetic was checked. Candidates now carry the character span of their own match, which makes the locator the precise pointer section 3 asks for *and* makes the fingerprint distinguish claims that are genuinely different. A sanity check now looks for shared fingerprints directly, because a silent drop is invisible unless something looks for it. | [CLAIMS §3-4](CLAIMS.md#3-a-locator-names-the-region-not-the-page) |
| **C46** | **Two more locators that could not distinguish, and one that pointed nowhere.** The same class twice more, both found by running the real fleet rather than fixtures. The calendar rule located an entry by a window *clamped* to the block, so two dates near the start of one short block on Cornell's calendar both got `char_start=0` — 34 of 2,156 candidates lost. And the programme rule recorded a link-derived claim with `json_ld_path = "links.192"`, a field documented as a path into a JSON-LD object, which `resolve` had no branch for: **37 of every 200 locators sampled did not resolve at all**. `Locator.link_index` is now its own field. The shared cause is worth naming: locating a claim by anything other than the wording that produced it is how a pointer stops being followable. | [CLAIMS §3](CLAIMS.md#3-a-locator-names-the-region-not-the-page) |
| **C47** | **An ancestor heading was treated as evidence, which on an admissions page means every paragraph on it.** The precision audit found a dean's byline, a news blurb about a journalism trip, and "students from all walks of life come together" recorded as `ADMISSION_REQUIREMENT` at MEDIUM — because the page's `h1` was "Undergraduate Admission" and that was enough. The same shape put a $121,467.08 **United Way donation total** under Princeton's "Cost & Aid" heading as a tuition claim, and Imperial's entire navigation menu — one `<li>` containing "Entry requirements", "Deadlines", "Fees and funding" and forty other links — became a 2,000-character admission requirement that resolved perfectly and was attributed correctly. Three fixes: site chrome (`nav`, `header`, `footer`, `noscript`) is excluded using the `container` Step 5C.1 recorded *for exactly this decision* (D45); MEDIUM now requires the wording itself to be about the field; and heading-only matches stay as LOW candidates with the reason stated, because filtering them out would also drop "We also accept the Cambridge Pre-U Diploma". Found in the same audit and fixed with it: a postcode ("Evanston, IL 60208"), a CSS colour triplet (`122,0,223`) and a settings-payload id became fees, and institution-code fragments became "IELTS 1.0" and "TOEFL 3.0" — every one of them inside the plausible range, not one of them published by anybody. | [CLAIMS §7, §9](CLAIMS.md#7-confidence-is-a-band-with-a-reason-never-a-decimal) |
| **C48** | **A scope regex asserted a United States applicant scope from the English pronoun "us".** `\bUSA?\b` compiled with `re.I` matched "us", so pages reading "contact us", "study with us" and "Explore what makes us special" were given an applicant scope. Worse than a wrong label: `scopes_in` returning non-empty sets `unresolved_reason` to NULL, so those rows read as **scope resolved** -- section 10's failure mode arriving through the back door, from pages that said nothing about scope at all. The same audit found the opposite error beside it: `china(?:ese)?` is "chin" + "a" + an optional "ese", so it matched "china" and the non-existent "chinaese" and **could never match "Chinese"** -- the single most important scope form for a product built for Chinese students. The rule is now per-alternative: a NAME is case-insensitive, an ACRONYM is not. Fixing it the first time by lowercasing the whole pattern and dropping `re.I` repaired the acronym and broke every country name, so there is now a test for each direction. | [CANDIDATE_REVIEW §12](CANDIDATE_REVIEW.md#two-live-bugs-this-found) |
| **C49** | **A deadline's time was taken from anywhere in the block, and a round label from the section heading.** Resolving all 1,941 locators found six candidates whose `value_raw_text` was not a substring of the wording they point at, and every one was a deadline built by concatenating a date match with a time match found elsewhere. UCL's *"except those with a 15 October deadline, should arrive at UCAS by 18:00 (UK time) on 13 January 2027"* produced "15 October 18:00"; Harvard's timeline joined a date and a time 700 characters apart. A time is now bound to its own date within 30 characters with no intervening date, the same binding-over-distance correction C44 made for language scores. Separately, `_context_for` searched the heading and the text together and took the first match, so under Caltech's "Early Action" heading a list item reading *"January 4, 2027 for Regular Decision"* was stored as Early Action -- a date filed under the wrong round, which is worse than no round. The label now comes from the wording first and records which of the two it was, so grouping can decline to separate rounds on a label the page only implied. And "6pm (GMT)" recorded `TIMEZONE_NOT_STATED`, because the zone pattern could not cross an opening bracket. | [CANDIDATE_REVIEW §18](CANDIDATE_REVIEW.md#18-rule-corrections-this-step-made) |
| **C50** | **A postcode became an IELTS score and a ZIP code became a TOEFL score.** `_SCORE` captured `\d{1,3}` with no token boundary, so Toronto's *"IELTS Results Service Account address: 172 St. George St., Toronto ON M5R 0A3"* yielded IELTS 5.0 and *"Educational Testing Service PO Box 6151 Princeton, New Jersey USA, 08541-6151"* yielded TOEFL 85 from the raw text "085". **Both are plausible scores**, so neither the scale check nor the published-requirement floor added in 5C.2 could catch them -- the only thing wrong with them is that the number was part of a longer token, which is the same correction the tuition rule needed for digit-run fragments. The same version found that a comma is coordination: in *"an overall score of 7.0 in IELTS, 100 in TOEFL, or 76 in PTE"* the 100 sits two characters after "IELTS" and four before "TOEFL", so by proximity IELTS claimed it -- and because IELTS already had 7.0, TOEFL ended up with no score at all. | [CANDIDATE_REVIEW §18](CANDIDATE_REVIEW.md#18-rule-corrections-this-step-made) |
| **C51** | **A heading's preceding sibling was recorded as its ancestor.** `heading_path_for` keeps the first heading it meets walking backwards unconditionally, because `shallowest` starts as None. For a content block that is right -- the nearest preceding heading *is* its section. For a block that is itself a heading it is wrong, and `extract_program` is the only caller that asks for the path of a heading: Cambridge's "Archaeology, BA (Hons)" carried the path `[..., 'A', 'Anglo-Saxon, Norse, and Celtic, BA (Hons)']`, naming the programme listed above it as a parent section. A heading's own level now seeds the walk. The same audit found a year range read as a day: "winter term 2026-27 December 4" produced the date "27 December" -- `\b` is satisfied between "-" and "27" -- and the bad match consumed "December", so the next match became "4 November" out of the text after it. | [CANDIDATE_REVIEW §18](CANDIDATE_REVIEW.md#18-rule-corrections-this-step-made) |
| **C52** | **C47's chrome fix works exactly as tested, and 178 chrome rows still reached the admission candidates.** Not one of the 985 comes from a `nav`, `header`, `footer` or `noscript` container -- the container test catches everything it tests. They arrived in `main` (517), untagged (243) and `article` (223) instead: **146** whose evidence is character-identical to a link label in the same document, **30** raw inline `<script>`/JSON payloads read as paragraph text, and **2** navigation menus long enough to saturate the stored evidence at 4,000 characters (the real list items are 30,326 characters of "Open submenu ..."). Two of them carry MEDIUM, which makes them indistinguishable by band from a real requirement sentence. They are classified here, because Step 5C.3 is about preparing a review queue; the durable fixes are upstream and are named as blockers -- dropping `<script>`/`<style>` subtrees in the normaliser, and testing link-label identity at extraction time where `document.links` is already in hand. Both alter every document hash and supersede every candidate, which is a decision to take deliberately rather than as a side effect of a review-tooling step. | [CANDIDATE_REVIEW §11](CANDIDATE_REVIEW.md#c47s-fix-works-and-178-chrome-rows-still-got-through) |
| **C53** | **A page heading outranked the sentence, and the first keyword beat the nearest one.** The first tuition financial-context run returned 18 `ESTIMATED_TOTAL_COST` and **zero** `HOUSING` on a corpus that visibly contains housing amounts. Two causes: Berkeley's housing line sits under a page heading reading "Student Budgets (Cost of Attendance)" and the heading was consulted before the wording, so the amount was classified from *Cost of Attendance* rather than from *Housing*; and within one sentence the first matching pattern won rather than the closest, so Harvard's collapsed table `Tuition$56,550Fees$5,126Housing$12,922Food$8,268` classified every amount in the run from whichever label the pattern order reached first. Classification now reads the column label, the row label, the wording **by proximity to the amount**, then the headings deepest-first. A third bug hid underneath: `\bhousing\b` never matched in that table at all, because the character before "Housing" is a digit and a digit is a word character -- the labels need letter-only lookarounds. | [CANDIDATE_REVIEW §13](CANDIDATE_REVIEW.md#13-tuition) |
| **C54** | **The drop pass mutated the tree while iterating it, so most of what it meant to remove survived.** `for element in tree.iter(): if tag in DROPPED_TAGS: _drop(element)` advances past the next sibling every time it removes one, so **22 of 29 scripts and 48 of 49 SVGs stayed in the fleet's documents** and their payload was emitted as visible prose -- `wp-emoji-settings`, an `Mmenu` constructor, an MSCI analytics IIFE. C52 named the durable fix as "drop `<script>`/`<style>` subtrees in the normaliser" and that description was accurate but incomplete: the subtrees *were* being dropped, just not all of them, and removing `<script>` from the emitted block list would not have helped because `parent.text_content()` still contained the descendant's text. Collecting the doomed elements first and then removing them took the fleet's script-shaped blocks from 168 to 1 and its script-derived candidates from 20 to 0. Removal also welded the surrounding words together (`see below` from `see <script>x=1</script> below`), so a space is inserted where an element is taken out. | [CLAIMS §12](CLAIMS.md#12-extraction-hygiene) |
| **C55** | **Adding `section` to the container list created a way out of a `nav`.** `container` records the *innermost* semantic container, so once `section` was recognised, a `<section>` wrapped in a `<nav>` began reporting `section` -- and every chrome guard that tests `container in {nav, header, footer}` would have waved it through. A field added to help tell body from chrome made one case of it worse. The two facts are now recorded separately: `container` stays the innermost container, and `in_chrome` is sticky, true for everything below a `nav`, `header` or `footer` however many `<section>`s intervene. Candidates under a navigation heading path fell from 72 to 5, and the five that remain sit in a `<div class="breadcrumb">` with no semantic element at all -- reported rather than special-cased, because section 5 forbids relying on a CSS class name. | [CLAIMS §12](CLAIMS.md#12-extraction-hygiene) |
| **C56** | **The programme rule was reading site-wide course pickers because a link had no structural provenance.** Step 5C.3 quarantined 210 of 226 programme groups, 180 of them link-derived, because `Link` recorded no container: a catalogue's programme link and an A-Z index entry were the same row. Measured after the fix, **60% of the fleet's links (16,874 of 28,047) are inside navigation**, which is the population the rule had been reading. Requiring body origin -- `main`, `article` or `section`, and not `in_chrome` -- took programme candidates from 479 to 54 and distinct programme names from 253 to 33. Cambridge's "Courses for 2027 entry >> Vertical menu >> A" picker is the shape that disappears: thirty real award names, in a site-wide index, describing no page in particular. | [CLAIMS §12](CLAIMS.md#12-extraction-hygiene) |
| **C57** | **The degree level was read from the whole heading path, so a page title stamped every paragraph beneath it.** `degree_level_of(" ".join([*heading_path, text]))` meant a page titled "Graduate Admissions" wrote `degree_level_raw='Graduate'` onto student testimonials. That is a wrong value on the row rather than a recall trade-off, so it is fixed rather than reported: the level now comes from the wording plus the **innermost** heading, which is the one describing the section the text actually sits in. Distinguished deliberately from the neighbouring defect that was *not* fixed -- 469 LOW rows exist because `_REQUIREMENT_HEADING` matched a page's own title, and removing the title from that test would take genuine requirement prose with it. The first is a bug; the second is a recall trade for a reviewer with the numbers in front of them. | [CLAIMS §12](CLAIMS.md#12-extraction-hygiene) |
| **C58** | **A link label is not a requirement, and the same shape in a catalogue is a programme.** 144 LOW candidates and 2 MEDIUM were paragraphs or list items whose entire text is one anchor's text -- "International entry requirements", "Explore Support Resources". The parser could not delete them, because a programme catalogue legitimately lists its programmes as links and deleting link-only content wholesale would lose the catalogue in order to fix the navigation. So the parser records the shape (`LinkProfile.is_link_only`, `link_only_items`, `Link.is_link_only_item`) and the *field rule* decides: the admission rule refuses a link-only unit, the programme rule reads one. The 85 short title-case admission candidates that remain are A-level subject lists, which look exactly like navigation and are exactly what an entry requirement is made of. | [CLAIMS §12](CLAIMS.md#12-extraction-hygiene) |
| **C59** | **Two definitions of "current" agreed by coincidence.** `review.py`'s docstring claimed there was exactly one definition; there were two, the second in `extract_claims.py` under a different bound-parameter name. They matched, so nothing failed -- until "current" grew a second axis (D55), at which point the copy would have gone on counting v1-derived candidates as live while the review queue did not, with no test to notice. The script now imports the definition, and both the predicate and the parameters it needs travel together as `current_only()` and `CURRENT_PARAMS`, so a call site that forgets the new axis fails loudly on a missing bind rather than quietly on a wrong count. Found while adding the axis, not by a test. | [CANDIDATE_REVIEW §5](CANDIDATE_REVIEW.md#5-what-current-means-and-why-one-frozen-list-was-not-enough) |
| **C60** | **The claim pass would have run every rule over both parses of every page.** `targets_for_claims` selected every non-`FAILED` extraction, and after re-extraction that is two per snapshot. The corpus would have doubled and the fingerprint could not have collapsed it, because `extraction_id` is part of the fingerprint and the two extractions are different rows. Every count in every report would have doubled with it, plausibly. Caught by checking the version tally before re-running the pass rather than after. | [CLAIMS §12](CLAIMS.md#12-extraction-hygiene) |
| **C61** | **A regex said `\\b` and contained a backspace, and had never matched.** `language._COORDINATOR` was written as `\\b(?:or\|and\|either\|alternatively)\\b\|[/;,]` and stored as `<BS>(?:or\|and\|either\|alternatively)<BS>\|[/;,]`, because a tool wrote the file through a non-raw Python string where `"\\b"` means U+0008. The file looks correct in an editor, in `grep` and in a diff; `ruff` and `mypy` both pass; the pattern compiles. It simply cannot match, because no web page contains a backspace -- so since the rule was written, only punctuation had ever coordinated and the words never had. Restoring the boundaries added one correct claim ("a minimum IELTS of 6 or TOEFL of 80") and removed none, so the language rule went to version 6. Found by scanning for control characters after the same mistake was made a second time in the scope rule's negation guard, where it would have let "non-US citizens" assert a United States scope. `tests/test_source_hygiene.py` now fails on any control character in any source file, and it caught a third instance immediately -- in the comment being written about the second. | [CLAIMS §13](CLAIMS.md#13-applicant-scope) |
| **C62** | **Revoking a domain revoked nothing.** See D59. The gap was not that revocation was unimplemented -- `reject_domain` and `mark_domain_legacy` have existed since revision `b0c1d2e3f4a5` and both write correctly. It was that nothing downstream read the result: `source.publication_eligibility` is a copy, and no trigger re-derived it. A host could be declared not the institution's and keep publishing. It was found by writing the test section 17 asks for and watching it pass a claim it should have refused. | [SOURCE_VERIFICATION §5](SOURCE_VERIFICATION.md#5-revocation) |
| **C63** | **The last arrow of the promotion chain has no writer.** `official_domain` -> `source_mapping` -> `source_mapping.promoted_source_id` -> `source.publication_eligibility` is enforced end to end by `source_mapping_requires_trusted_host`, the generated `source_mapping.publication_eligibility`, `source_eligibility_is_earned` and C27. But `promoted_source_id` is written by nothing in `src/` or `scripts/` -- only by tests and the migration. `register_verified_candidate` creates a mapping and stops, by design, because promotion is a separate decision. So no pilot source can be made eligible today even with every human decision in place. This is the first blocker for Step 5C.6 and it is a missing function, not a missing rule. | [SOURCE_VERIFICATION §7](SOURCE_VERIFICATION.md#7-what-blocks-promotion) |
| **C64** | **An official source could publish a fee because it was verified as the language page.** The pilot's shape is 385 responsibility claims over 319 URLs, so one URL is submitted as both `LANGUAGE_REQUIREMENTS` and `TUITION_FEES`, and a reviewer decides them separately. C27 read only `source.publication_eligibility`, so the two decisions collapsed into one and the permissive one won: a tuition claim would have published on the strength of the language verification, from the same source, snapshot and extraction. Found by auditing the gate before implementing promotion rather than after. `test_one_url_verified_for_language_and_rejected_for_tuition` fails against any source-level implementation. | [SOURCE_VERIFICATION §9](SOURCE_VERIFICATION.md#9-authority-is-scoped-to-a-field) |
| **C65** | **Revoking a promoted mapping was impossible.** `ck_source_mapping_only_a_trusted_mapping_may_be_promoted` refuses a `promoted_source_id` on anything not verified, so the single `UPDATE` that rejects a mapping violated the constraint -- and the withdrawal trigger Step 5C.5 added could never fire, because the update never landed. Losing trust now clears the promotion in the same statement, and the withdrawal trigger reads `OLD.promoted_source_id` because by then `NEW` has been cleared. Revocation was written as a feature in 5C.5 and was not exercised end to end until a test tried it. | [SOURCE_VERIFICATION §13](SOURCE_VERIFICATION.md#13-revocation-and-partial-revocation) |
| **C66** | **A trust guard that blocked the withdrawal it was meant to protect.** The first version of `app_promotion_requires_live_trust` fired on `UPDATE OF promoted_source_id, official_domain_id, is_active`, which meant deactivating an already-promoted mapping was treated as an attempt to promote one and refused. Retiring a page is not an act of promotion. The trigger now returns early when the promotion is unchanged, so it guards establishing trust and never getting rid of it. | [SOURCE_VERIFICATION §13](SOURCE_VERIFICATION.md#13-revocation-and-partial-revocation) |
| **C67** | **The operator path was missing two of its seven steps.** `verify_candidate` and `register_verified_candidate` had existed and been tested since revision `b0c1d2e3f4a5`, and neither had an entry point: a reviewer could verify a host and then had nowhere to go, because the responsibility decision acts on a `source_mapping` and nothing could create one. Step 5C.7A found it by writing out the commands Dejan would actually type and discovering two of them did not exist. `pilot-source` and `register-pilot-source` are thin wrappers -- the business logic, the lock order and the audit append stay in the services. | [SOURCE_VERIFICATION §14](SOURCE_VERIFICATION.md#14-the-operator-path) |
| **C68** | **A fixture that could not have existed.** The first end-to-end workflow test gave each responsibility claim on one URL a distinct `url_sha256`, and promotion correctly refused it: the mapping and the source described different URLs. Checking the real data showed all 385 rows share their source's hash exactly, and that `ix_pilot_collected_source_physical` -- UNIQUE on `(submission_id, target_institution_id, url_sha256) WHERE duplicate_of_source_ref IS NULL` -- **is** the 385-over-319 model: one physical row per URL, every further responsibility a duplicate pointing at it. The real CUHK page is precisely this (S0355 physical, S0356 and S0363 duplicates, one of them `UNCLASSIFIED`). The fixture was rewritten to that shape; it had been testing an arrangement the schema forbids. | [SOURCE_VERIFICATION §14](SOURCE_VERIFICATION.md#14-the-operator-path) |
| **C69** | **An exception named `Test...` broke test collection.** `TestIdentityRefusedError` was collected by pytest as a test class, and the module failed to import at all -- so every test in it, including the actor-spoofing ones, silently did not run. Renamed `FixtureIdentityRefusedError`, with the reason recorded on the class so the prefix is not reintroduced. A suite that cannot import is indistinguishable from one that passes if nobody reads the collection error. | — |
| **C70** | **An email address was accepted as authority over an account.** `reviewer-set-password` was `UPDATE app_user SET password_hash = :h WHERE lower(email) = lower(:e) AND is_active`, with nothing checking whether a credential already existed. Anyone able to run the CLI could overwrite any reviewer's password knowing only their email, authenticate as them, and record verification decisions in their name -- defeating the whole of Step 5C.7B's authentication work with a value printed on business cards. The command is replaced by three: **initial enrolment**, which writes with `WHERE password_hash IS NULL` so it can happen exactly once per account and cannot overwrite anything; **change**, which proves the current password first; and **administrative reset**, which proves an administrator and is audited as `CREDENTIAL_RESET`. The guard is the `WHERE` clause rather than a read-then-decide, so two concurrent enrolments cannot both succeed. Found by auditing the command before its intended user ran it. | [SOURCE_VERIFICATION §18](SOURCE_VERIFICATION.md#18-credential-enrolment) |
| **C71** | **Knowing an email address was enough to claim an identity.** C70 stopped an existing credential being *overwritten*; it did not stop an account being claimed in the first place. `enrol_initial_password` required an email and the absence of a password, so whoever ran it first won -- an operator who knew Dejan's address could enrol before him, choose the password, and record verification decisions in his name. The same takeover as C70, one step earlier, and the email address it turned on is printed in this repository's own documentation. The first password now requires a one-time challenge: a 256-bit token issued for that account and delivered out of band, of which only the SHA-256 is stored. What makes it hold is the grant matrix rather than the Python -- the attacker in this threat model runs our code -- so `app_api` holds no `INSERT` on `credential_enrollment` and no `UPDATE` on `app_user`, and reaches a password only through `app_claim_enrollment`, a `SECURITY DEFINER` function whose `WHERE` clauses are the checks. Issuing needs the owner's connection; claiming does not. Found by auditing C70's fix before its intended user ran it. | [SOURCE_VERIFICATION §18](SOURCE_VERIFICATION.md#18-credential-enrolment) |
| **C72** | **Test-shaped code could write to the real pilot database.** A Step 5C.7E verification script assembled its DSN from the project settings; `.env` sets `POSTGRES_DB=datahub`, so it connected to the **real pilot database**, created eight `[TEST ONLY]` identities, issued each an enrolment challenge and deleted them. The cascade removed the identities and challenges; the nine `CREDENTIAL_ENROLLMENT_ISSUED` rows remain, because `audit_log` is append-only -- correctly, and they are kept as evidence rather than erased. No trust state moved and nothing was corrupted, but a default nobody mentioned had put production data one typo from a test write. The fix is a guard that asks the **server** `SELECT current_database()` and refuses anything not on a code-level allow-list, wired into `postgres_dsn` -- the one fixture every DB-backed test descends from, so it cannot be forgotten. Checking `POSTGRES_DB` or requiring `DATAHUB_ENV=test` was rejected: that is the check that already failed, because the variable was right and the intent was wrong. An unrecognised database is refused exactly like the real one, and there is deliberately no environment flag that relaxes it -- a flag is one inherited export from being always-on. `provision_reviewer(test_only=True)` and fixture-scoped token issuance refuse the real database outright, which is the layer that would have caught the original mistake. | [SOURCE_VERIFICATION §21](SOURCE_VERIFICATION.md#21-keeping-tests-out-of-the-real-database) |
| **C73** | **A SECURITY DEFINER function ran with superuser authority.** `app_claim_enrollment` was owned by `datahub`, the schema owner, which on this cluster is a superuser -- so every statement in its body ran as one. The body was static, had no dynamic SQL and was tested, so nothing was exploitable; but "not exploitable" is a property of today's code and the owner is a property of the object, so any future defect had the whole cluster available to it. The function is now owned by `app_credential_definer`: `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`, no password, no members, holding **column-level** privileges only -- `UPDATE(used_at)` on `credential_enrollment`, `UPDATE(password_hash, updated_at)` on `app_user`, and `SELECT` on exactly the columns the body reads. No `INSERT`, so the function that consumes tokens cannot manufacture them; whole-table `UPDATE` was rejected because it would let a defect change an email or `is_active`. A defect now reaches two tables and four columns. | [SOURCE_VERIFICATION §18.3](SOURCE_VERIFICATION.md#183-issuing-and-claiming-are-different-privileges) |

C1 has fallout beyond publication step 5, which revision 2 missed: `entity_version.is_current`, `field_provenance.superseded_at` and `field_claim.resolution_status` were all mutable columns on tables declared immutable. All three are removed. Current-state pointers now live in explicit **projection** tables (`entity_head`, `field_current`), which are mutable by design and rebuildable — consistent with D9.

### 1.3 Revision 3 schema decisions

| # | Decision |
|---|---|
| **B1** | Admission requirements may vary by applicant country and qualification. `applicant_scope` is a first-class, extensible concept built from typed criteria over an open set of dimensions. **No China-specific rule is hardcoded** — institution tier lists are ordinary source-published data. |
| **B2** | Introduce **`program_offering`**: Program → Program Offering → Intake. Offering dimensions: study mode, campus, delivery mode, duration. Tuition and intake attach to the offering. |
| **B3** | Chinese content is **internal derived translation** for MVP, not an authoritative source-backed fact. Official-language values remain the governed facts. A separate `translation` model carries locale, value, translator/method and optional review metadata. It does **not** duplicate field provenance. |
| **B4** | Deadlines: Program Offering → Intake → Application Round → Application Deadline, optionally scoped by `applicant_scope`. Campus is **not** duplicated in the deadline key — it is determined by the offering. |
| **B5** | `student_category` is a controlled table, destination-scopable (UK: Home/International; HK: Local/Non-local), not a fixed database enum. `tuition` references it. |
| **B6** | `canonical_id` is immutable once created. Names and external identifiers change without changing it. Renames use alias tables; real-entity replacement uses explicit `SUPERSEDED_BY` / `MERGED_INTO` relationships. |
| **B7** | Standard academic hierarchy: **University → Faculty/School → Program → Program Offering → Intake → Application Round.** Requirements, deadlines and tuition attach at the narrowest correct grain. |

### 1.4 Revision 3 decisions arising from the above

| # | Decision | Rationale |
|---|---|---|
| **D13** | **Versioned aggregate roots are `university` and `program`.** Offerings, intakes, rounds, deadlines, requirements and tuition are versioned *as part of their program's* version; campuses, faculties and rankings as part of their university's. Provenance rows are still keyed to the narrow row they describe, and carry a denormalized `(root_type, root_id, root_version_no)` so a whole program's provenance loads in one indexed query. | A consultant's mental unit of history is "this program" and "this university". Per-row versioning would force assembling a dozen version streams to answer "what did this program look like on 1 Sept". |
| **D14** | **Enforcement strength tracks field risk.** High-risk facts live on narrow tables with an **inline `*_status` column beside the value**, so value/status agreement is a real single-row `CHECK`. Low-risk descriptive fields on wide entities use the generic `field_current` projection, where agreement is trigger- and reconciliation-enforced. | The PRD demands 100% rigor precisely on the narrow high-risk tables. Buying a genuine database constraint there is cheap; paying for it across every descriptive field is not. |
| **D15** | **Absence of a whole fact collection is asserted explicitly** in `fact_absence` (e.g. "this offering's official fee page publishes no international tuition for 2027"). An inline status column cannot express a row that does not exist. | Without this, 官网未发布 is representable for fields but not for missing records, which is the more common case. |
| **D16** | **A composite fact carries one `field_status`, not one per column.** A published deadline is the triple (kind, date, official text); a published fee is (amount, currency, unit). Status governs the fact, so the inline column is named for the fact (`deadline_field_status`, `amount_field_status`) rather than for each column. | Per-column statuses on a composite fact are always either redundant or contradictory. One status per fact keeps the D14 `CHECK` meaningful and makes C4's kind/date matrix expressible. |
| **D17** | **One reusable "source date" column group** for every temporal fact (deadlines, application open dates, ranking publication dates, status effective dates). Its shape is fixed in §4.5 and applied with a per-use column prefix. Precision is a **generated** column, never hand-set. | Three or four tables will carry temporal facts with the same honesty requirement. Defining the group once means the constraints are written once and cannot drift between tables, and a reviewer learns one pattern instead of four. |

---

## 2. System architecture

### 2.1 The three planes

```
  EVIDENCE PLANE — append-only, machine-written, never served as truth
    source → fetch_run → snapshot → extraction → field_claim → claim_resolution
                                        │
                                        ▼
  GOVERNANCE PLANE — mutable working state; decisions append-only
    change_proposal → change_proposal_item → review_task → review_decision
                                        │
                                        ▼  publication.publish()  ← the only writer
  CANONICAL PLANE — curated, human-approved
    ├─ HISTORY (append-only, never mutated)
    │    entity_version · field_provenance · audit_log · change_event · entity_relationship
    └─ PROJECTIONS (mutable by design, rebuildable)
         university … application_deadline · entity_head · field_current · entity_alias
```

The split inside the canonical plane is the substance of **C1**: history is append-only, projections are mutable, and nothing is both.

### 2.2 Runtime topology

Unchanged from revision 2: Next.js 15 console + BFF · FastAPI (Python 3.12, SQLAlchemy 2.0 async, Pydantic v2) · Celery 5 with queues `crawl`/`browser`/`extract`/`detect`/`sla`/`notify`/`maint` · Celery Beat in its own container · Scrapy-as-library + Playwright + httpx · PostgreSQL 16 · Redis 7 · S3-compatible evidence store with versioning and Object Lock · Nginx with a separate origin for snapshot rendering.

Four container roles, one codebase (D11): `api`, `worker`, `beat`, `worker-browser`.

---

## 3. Domain / module boundaries

| # | Context | Owns | May read | May write |
|---|---|---|---|---|
| 1 | **Catalog** | Canonical identity + published state, aliases, relationships | — | nothing directly — written only by Publication |
| 2 | **Source Governance** | Registry, field bindings, crawl policy, robots/ToS, authorization + ranking gate | Catalog | own tables |
| 3 | **Acquisition** | Fetch scheduling, runs, snapshots, versioned extractors | 2 | evidence tables, S3 |
| 4 | **Claims** | Normalization, entity resolution, claim store | 3; Catalog read-only | claim tables |
| 5 | **Detection** | Comparators, risk matrix, drift guard, conflicts, SLA, proposals | 4; Catalog projections | proposals, conflicts, alerts |
| 6 | **Review & Publication** | Tasks, decisions, return/correct, second review, publish transaction, version issuance | 5, 9 | **Catalog**, history, projections, outbox |
| 7 | **Versioning & Audit** | Immutable versions, provenance, audit log | — | own tables, INSERT only |
| 8 | **Discovery** | Search, filter, compare, Latest Updates, internal API | 1, 7 | nothing |
| 9 | **Identity & Access** | Users, roles, permissions, API clients, SoD policy | — | own tables |
| 10 | **Localization** | Translations, staleness detection (B3) | 1, 7 | `translation` only |
| — | **Operations** | Health aggregates, alerts, assignment, retrigger | 2,3,5,6 | `alert` only |

Localization is a new, deliberately thin context: it depends on Catalog and Versioning but nothing depends on it, so translations can never influence a governed fact.

---

## 4. Revised entity relationship structure

### 4.1 Academic hierarchy (B7)

```
destination
    │
    ▼
university ────────────────────────┬──────────────┬─────────────────┐   [VERSIONED ROOT]
    │                              │              │                 │
    ├─▶ campus                     │              ├─▶ ranking_entry ─▶ ranking_edition ─▶ ranking_publisher
    │      ▲                       │              │                              (feature-gated, D8)
    ├─▶ faculty ──┬── parent_faculty_id (self, school → department)
    │      │      └─▶ faculty_campus ─▶ campus
    │      │
    │      ▼
    └─▶ program ───────────────────────────────────────────────────┐   [VERSIONED ROOT]
           │  program_faculty (M:N)    program_discipline (M:N) ─▶ discipline
           │
           ▼
        program_offering            (study_mode × campus × delivery_mode × duration)   [B2]
           │
           ├─▶ tuition              (× academic_year × student_category)               [B5]
           │
           ▼
        intake                      (× academic_year × season)
           │
           ▼
        application_round           (round_no, opens_at)
           │
           ▼
        application_deadline        (× applicant_scope)                                [B4]

  admission_requirement  ─┐
  language_requirement   ─┴─▶ attaches at the narrowest correct grain:
                              program | program_offering | intake,  × applicant_scope  [B1, B7]
```

### 4.2 Applicant scope — extensible, not China-specific (B1)

```
scope_dimension            e.g. applicant_country · qualification_type · qualification_group
      ▲                         · residency_status · (future dimensions added as data)
      │
applicant_scope_criterion  (dimension, operator, value | value_ref)
      ▲
      │  1..n  (a scope is the conjunction of its criteria; zero criteria = universal)
applicant_scope ◀────────── admission_requirement · language_requirement · application_deadline
      │
      └─ value_ref may point at ─▶ qualification_group ─▶ qualification_group_member
```

A university's own institution tier list (the 985/211 case, and every equivalent elsewhere) is a `qualification_group` whose members are source-published data with its own provenance. No jurisdiction's rules are encoded in the schema — adding a new dimension is an INSERT into `scope_dimension`, not a migration.

### 4.3 Evidence → governance → history

```
source ──▶ source_field_binding (field responsibility, 字段归责)
   │   └──▶ source_authorization (drives the D8 ranking gate)
   ▼
fetch_run ──▶ snapshot ──▶ extraction ──▶ field_claim ──▶ claim_resolution
                                             │    └────────▶ resolution_candidate (mutable queue)
                                             ▼
                                    change_proposal ──▶ change_proposal_item
                                             │                  │
                                             ▼                  └─▶ field_conflict ─▶ conflict_resolution
                                        review_task ──▶ review_decision   (append-only)
                                             │
                                             ▼  publication.publish()
                    ┌────────────────────────┴─────────────────────────┐
                    ▼                                                  ▼
      HISTORY (append-only)                              PROJECTIONS (mutable)
   entity_version ──┐                                  entity_head (current version pointer)
   field_provenance ┼─▶ references snapshot + claim     field_current (current field status)
   audit_log        │                                   canonical tables (current values)
   change_event ────┘                                   entity_alias
   entity_relationship
```

### 4.4 Identity, supersession and translation

```
university / program
   ├─ canonical_id            IMMUTABLE once created                         [B6]
   ├─▶ entity_alias           former names, trade names, external IDs (projection)
   └─▶ entity_relationship    SUPERSEDED_BY | MERGED_INTO | SPLIT_INTO (append-only)  [B6]

any governed field ──▶ translation  (locale, value, method, translator, source_version_no)  [B3]
                            └─ derived, never a governed fact; stale when source_version_no
                               falls behind the field's current published version
```

### 4.5 Temporal facts, deadlines and rounds (C4, C5, C9, C10, D17)

#### 4.5.1 The source-date column group (D17)

Official sources state dates at wildly different precision, and with or without a
timezone:

| The source says | What is actually known |
|---|---|
| `15 January 2027` | year, month, day. No time. No zone. |
| `January 2027` | year, month. |
| `mid-January` | month and a part-of-month. Possibly not even a year. |
| `15 January 2027, 23:59 GMT` | year, month, day, time, zone — an exact instant. |
| `rolling` / `until filled` | no date at all; see `deadline_kind`. |

**Revision 4 was wrong here.** It stored a single `timestamptz` plus a precision
flag, with a convention that a date-only fact became 23:59:59 in the declared zone.
That manufactures two pieces of information the institution never published — a
time and a timezone — and the manufactured value is indistinguishable from a real
one in every query that does not also read the precision column.

The corrected shape stores exactly what the source gave. Applied with a per-use
prefix (`deadline_`, `opens_`, `published_`, `status_effective_`):

```
<p>_year         smallint    NULL    -- NULL only when the source gives no date at all
<p>_month        smallint    NULL    -- 1..12
<p>_day          smallint    NULL    -- 1..31, and a real calendar date
<p>_month_part   enum        NULL    -- EARLY | MID | LATE  ("mid-January")
<p>_time         time        NULL    -- local wall time, only if stated
<p>_timezone     text        NULL    -- IANA name or fixed offset, as declared
<p>_precision    GENERATED STORED    -- DATETIME | DATE | MONTH_PART | MONTH | YEAR | NULL
<p>_instant_utc  timestamptz NULL    -- exact instant; NULL unless date+time+zone known
<p>_cal_range    GENERATED STORED    -- daterange, CALENDAR-LOCAL query aid. NOT UTC.
<p>_text         text                -- the source's verbatim wording
```

Three properties worth stating explicitly:

- **Precision is derived, not asserted.** It is a generated column computed from
  which parts are present, so it can never contradict the data. Nobody sets it, and
  nobody can set it wrongly.
- **`instant_utc` exists only where it is real.** It is populated if and only if the
  source supplied date, time and zone. A `CHECK` enforces that, so "we have an exact
  instant" is a database-guaranteed claim rather than a convention.
- **`<p>_text` is the authoritative human answer.** For `ROLLING`, `UNTIL_FILLED` and
  imprecise dates it is the *only* faithful rendering, so the UI renders it rather
  than formatting the parts.

#### 4.5.1.1 Filtering and sorting imprecise facts (C13)

FR-01 must filter and sort by deadline, and that has to work for `January 2027` as
well as for an exact instant. Revision 5 proposed a `window_utc tstzrange` holding
"the instants the fact could denote across plausible offsets". **That was still
invention.** A source that never stated a timezone cannot be given a UTC interval:
the endpoints would be manufactured offsets, and the `_utc` name would assert a zone
the institution never published. Withdrawn.

Two representations, with a hard line between them:

| | `<p>_instant_utc` | `<p>_cal_range` |
|---|---|---|
| Type | `timestamptz` | `daterange` (calendar dates, no time, **no zone**) |
| Exists when | the source gave date **+ time + zone** | the source gave any calendar date |
| Status | **source truth** — an exact instant the source published | **derived query aid** — never source truth, never displayed |
| Timezone | the declared one | none, and none implied |

`<p>_cal_range` is a range of *calendar dates in the source's own frame of
reference*. It carries no time and no zone, so it asserts nothing about instants:

```
15 January 2027     ->  [2027-01-15, 2027-01-16)
mid-January 2027    ->  [2027-01-01, 2027-02-01)   -- whole month, conservative
January 2027        ->  [2027-01-01, 2027-02-01)
2027                ->  [2027-01-01, 2028-01-01)
rolling             ->  NULL
```

It is a **generated column**, computed from the stored parts by immutable
expressions, so - like `precision` - it cannot drift from the data and nobody can
set it wrongly.

**The range is conservative, and encodes no interpretation (C14).** An earlier draft
mapped `EARLY`/`MID`/`LATE` to days 1-10 / 11-20 / 21-end. Those boundaries are a
*product* interpretation, not a source fact, and had no business being immutable
database semantics. A `MONTH_PART` fact therefore covers the **whole stated month**:
"mid-January 2027" filters as `[2027-01-01, 2027-02-01)`. The range over-covers
rather than guessing, which is the safe direction for a filter -- it can return a
deadline that turns out to be late January, but it will never hide one.

`month_part` remains stored as structured metadata, and `<p>_text` ("mid-January")
remains the authoritative rendering. If the business later wants narrower windows,
that is an explicit, configurable product policy in the query layer -- never a
generated column and never source semantics. Recorded as **N5**.

**Ordering.** There is no single instant to sort mixed-precision facts by, so the
sort key is the calendar components themselves -
`(year, month, day NULLS FIRST, time NULLS FIRST)` - with `instant_utc` breaking
ties only among facts that genuinely have one. No comparison is invented, because
calendar components are what the sources published.

**What the API must admit.** A filter like "deadlines before 15 January" cannot be
answered to the hour for a fact that states only `January 2027`, and that is a
property of the data rather than a defect. Read models therefore return `precision`
alongside every temporal fact, and a filter over imprecise facts is documented as
matching on calendar overlap rather than instant comparison. The console renders
`<p>_text` and marks approximate values as approximate.

#### 4.5.2 Rounds and deadlines

```
intake
   ▼
application_round
   ├─ round_code        FK → application_round_type   ROUND_1 · ROUND_2 · PRIORITY ·
   │                                                  EARLY_ACTION · EARLY_DECISION ·
   │                                                  MAIN · ROLLING · CLEARING · LATE ·
   │                                                  INSTITUTION_DEFINED
   ├─ round_label       the institution's own wording, verbatim
   ├─ round_label_norm  GENERATED — lower(collapse whitespace(trim(label)))   [C10]
   ├─ sequence_no       nullable — display/ordering ONLY, never identity      [C10]
   ├─ opens_*           source-date column group (§4.5.1)
   ├─ opens_field_status
   └─ lifecycle_status  derived view of openness; never contradicts its deadlines
   ▼
application_deadline    × applicant_scope
   ├─ deadline_field_status   NOT_CHECKED | OFFICIALLY_NOT_PUBLISHED | PUBLISHED | WITHDRAWN
   ├─ deadline_kind           FIXED_DATE | ROLLING | UNTIL_FILLED |
   │                          NO_FIXED_DEADLINE | NOT_CURRENTLY_ACCEPTING
   └─ deadline_*              source-date column group (§4.5.1)
```

Identity rules for rounds (**C10**, constraints in §8.1):

- a controlled `round_code` occurs at most once per intake;
- an `INSTITUTION_DEFINED` round is identified by its normalised institution label,
  so an intake may hold several of them with different wording;
- `sequence_no` participates in no unique constraint. It orders a display list.

Two distinctions the deadline model makes that a plain date column could not:

- **`NO_FIXED_DEADLINE` is not `OFFICIALLY_NOT_PUBLISHED`.** The first means the page
  states there is no fixed deadline; the second means the page says nothing about
  deadlines. Collapsing them publishes a claim the institution never made.
- **`NOT_CURRENTLY_ACCEPTING` is a published statement, not a derived state.** Round
  openness is derived from its deadlines, and the publication transaction blocks a
  publish that would make the two contradict (cross-row, so not a `CHECK` — §8.2).

### 4.6 Requirement ownership — nested scope with enforced integrity (C6)

Requirements apply at one of three nested levels. The levels are nested rather than disjoint, so "exactly one owner" is the wrong shape; the grain is the **deepest non-null level**, and composite foreign keys make every level referentially sound.

```
admission_requirement / language_requirement
   ├─ program_id     NOT NULL  ─────▶ program(id)
   ├─ offering_id    NULL      ──┬──▶ program_offering(program_id, id)   [composite FK]
   ├─ intake_id      NULL      ──┴──▶ intake(offering_id, id)            [composite FK]
   ├─ applicant_scope_id NOT NULL ──▶ applicant_scope(id)                [B1]
   └─ grain          GENERATED: INTAKE | OFFERING | PROGRAM

   offering_id NULL,     intake_id NULL      →  applies to the whole program
   offering_id SET,      intake_id NULL      →  applies to that offering
   offering_id SET,      intake_id SET       →  applies to that intake only
   offering_id NULL,     intake_id SET       →  impossible, rejected by CHECK
```

Because `program_offering` carries `UNIQUE (program_id, id)` and `intake` carries `UNIQUE (offering_id, id)`, the composite FKs guarantee that a requirement's offering really belongs to its program and its intake really belongs to that offering. PostgreSQL enforces the whole chain; nothing relies on application discipline. See §8.1 for the exact constraints.

**Accepted MVP limitation.** A requirement that applies to *some but not all* offerings of a program is represented by one row per offering. If that duplication becomes burdensome, the upgrade is a shared `requirement_set` plus a `requirement_applicability` join whose rows carry these same composite FKs — an additive change that preserves every constraint here. Tracked as **N3**.

### 4.7 Offerings are governed facts (C8 / U4)

`program_offering` is inside the governance plane. A crawler that discovers a part-time variant does not insert a row; it creates a `change_proposal`, and the offering exists only after review and publish.

```
program_offering
   ├─ canonical_id        IMMUTABLE once created (B6) — offerings are API-referenceable
   ├─ study_mode          ─┐
   ├─ campus_id           ─┤ identity-bearing, NOT NULL, each with field_provenance
   ├─ delivery_mode       ─┤ rows written at creation and on any correction
   ├─ duration_value/unit ─┘
   ├─ lifecycle_status        ACTIVE | SUSPENDED | WITHDRAWN
   └─ lifecycle_field_status  high-risk: closure affects admissions (inline status, D14/D16)
```

The dimensions are identity-bearing and therefore `NOT NULL` — an offering with an unknown study mode is not an offering. Their evidence lives in `field_provenance` like any other governed field. Because they also form the natural key, a correction that would collide with an existing offering is not an in-place edit but a merge, recorded as `MERGED_INTO` in `entity_relationship` (B6).

---

## 5. Table list with grain and purpose

`I` = append-only immutable · `P` = mutable projection (rebuildable) · `W` = mutable working state · `R` = reference data

### 5.1 Reference and taxonomy

| Table | Grain — one row per… | Purpose | Kind |
|---|---|---|---|
| `destination` | destination (country or SAR) | Pilot/rollout scoping | R |
| `discipline` | node in the discipline tree | Business / CS & Data / Engineering + subfields, external taxonomy mappings | R |
| `degree_level` | degree level | bachelor, master; doctorate reserved and extensible | R |
| `intake_season` | season term | Controlled vocabulary for intakes | R |
| `application_round_type` | round code | **C5.** ROUND_1 · ROUND_2 · PRIORITY · EARLY_ACTION · EARLY_DECISION · MAIN · ROLLING · CLEARING · LATE · INSTITUTION_DEFINED | R |
| `deadline_kind` | deadline kind | **C4.** FIXED_DATE · ROLLING · UNTIL_FILLED · NO_FIXED_DEADLINE · NOT_CURRENTLY_ACCEPTING | R |
| `currency` | ISO-4217 code | Money integrity | R |
| `billing_unit` | fee basis term | per_year / per_credit / total_program / per_module / per_semester | R |
| `test_type` | language test | IELTS / TOEFL / PTE / … — no equivalence between them is ever computed | R |
| `student_category` | fee category, optionally destination-scoped | UK Home/International, HK Local/Non-local (**B5**) | R |
| `scope_dimension` | applicability dimension | Open set: applicant_country, qualification_type, qualification_group, residency_status (**B1**) | R |
| `applicant_scope` | named applicability scope | The conjunction of its criteria; zero criteria = universal | R |
| `applicant_scope_criterion` | criterion within a scope | `(dimension, operator, value \| value_ref)` | R |
| `qualification_group` | named group of qualifications or institutions, as a source publishes it | Institution tier lists and their equivalents, as data | R |
| `qualification_group_member` | member of a group | The listed institution or qualification | R |

### 5.2 Canonical projections — academic hierarchy

| Table | Grain — one row per… | Purpose | Kind |
|---|---|---|---|
| `university` | institution | **Versioned root** (D13). Immutable `canonical_id` | P |
| `campus` | physical campus of a university | Location axis referenced by offerings | P |
| `faculty` | faculty/school/department node | Self-nesting via `parent_faculty_id` | P |
| `faculty_campus` | faculty ↔ campus association | The PRD's "120 学院或校区关系" | P |
| `program` | academic program as the institution names it | **Versioned root** (D13). Immutable `canonical_id` | P |
| `program_faculty` | program ↔ faculty association | Jointly-run programs | P |
| `program_discipline` | program ↔ discipline mapping | `is_primary` flag for filtering | P |
| `program_offering` | deliverable combination of study_mode × campus × delivery_mode × duration | **B2, C8.** The grain a fee or an intake actually belongs to. Governed: dimensions carry provenance; immutable `canonical_id` | P |
| `intake` | offering × academic_year × season | Admission cycle | P |
| `application_round` | round within an intake | **C5.** `round_code` + institution's own `round_label`, nullable `sequence_no`, nullable `opens_at` | P |
| `application_deadline` | round × applicant_scope | **B4, C4.** `deadline_kind` + nullable `deadline_at` + precision + declared timezone + official text. Campus omitted — determined by the offering | P |
| `admission_requirement` | (program, nullable offering, nullable intake) × applicant_scope × requirement_kind | Nested-scope grain with composite FKs (**C6**); official text always retained | P |
| `language_requirement` | (program, nullable offering, nullable intake) × applicant_scope × test_type | Same nested-scope design; overall + subscores; no cross-test conversion | P |
| `tuition` | offering × academic_year × student_category | Amount + currency + billing_unit as an inseparable tuple | P |
| `ranking_publisher` | publisher | QS / THE / ARWU … | P |
| `ranking_edition` | publisher × ranking_name × year | Methodology version, licence, `display_allowed` (**D8**) | P |
| `ranking_entry` | edition × university × optional discipline | Rank value or band | P |
| `entity_alias` | alias of an entity | Former names, trade names, external IDs (**B6**) | P |
| `fact_absence` | entity × collection_path × optional applicant_scope | Explicit "official source publishes no such facts here" (**D15**) | P |

### 5.3 Canonical history — append-only

| Table | Grain — one row per… | Purpose | Kind |
|---|---|---|---|
| `entity_version` | root entity × version_no | Full `state_snapshot` of that root's published state, plus `diff_summary`. No `is_current` column (**C1**) | I |
| `field_provenance` | narrow row × field_path × root version_no | The audit spine: status, value, source, snapshot, claim, reviewer, all four timestamps, `risk_level`. No `superseded_at` (**C1**) | I |
| `entity_relationship` | directed assertion between two entities | `SUPERSEDED_BY` / `MERGED_INTO` / `SPLIT_INTO` (**B6**) | I |
| `audit_log` | actor action | Hash-chained; every create/edit/review/publish/return/correct/rollback | I |
| `change_event` | published field change | **C7.** Immutable business history and the sole source of Latest Updates (**D12**). Read directly by the feed — it is not a delivery queue | I |

### 5.4 Canonical pointers — projections

| Table | Grain — one row per… | Purpose | Kind |
|---|---|---|---|
| `entity_head` | root entity | Current `version_no` + `published_at`. Replaces `entity_version.is_current` | P |
| `field_current` | entity × field_path | Current `field_status` + pointer to the authoritative `field_provenance` row, for fields not carrying an inline status column (**D14**) | P |

### 5.5 Evidence plane

| Table | Grain — one row per… | Purpose | Kind |
|---|---|---|---|
| `source` | official URL | Registry: type, authority tier, fetch strategy, robots/ToS record, `access_state` (**D6**) | W |
| `source_field_binding` | source × entity_type × field_path | Field responsibility matrix (字段归责), executable | W |
| `source_authorization` | authorization grant | Scope, grantor, evidence, expiry, `display_allowed` | W |
| `fetch_run` | fetch attempt | Outcome ledger for health and the consecutive-failure rule | I |
| `snapshot` | distinct fetched content (by sha256) | Raw bytes + rendered text + screenshot in S3; the legal evidence | I |
| `extraction` | snapshot × extractor × extractor_version | Parse attempt and its trace | I |
| `field_claim` | extracted assertion about one field | Normalized value + raw text + **character offsets into the snapshot** | I |
| `claim_resolution` | claim × resolved entity | Authoritative entity binding, replacing the mutable `resolution_status` (**C1**) | I |
| `resolution_candidate` | unresolved claim awaiting a human | Mapping queue | W |

### 5.6 Governance plane

| Table | Grain — one row per… | Purpose | Kind |
|---|---|---|---|
| `change_proposal` | proposed change set on one subject | Status, risk, SLA clock, detection type, `correction_of_version_id` | W |
| `change_proposal_item` | field within a proposal | old/new value + status, claim refs, `corrected_by` (drives **D7**) | W |
| `review_task` | assignment of a proposal to a reviewer for one round | `review_round` 1 or 2, SLA, escalation | W |
| `review_decision` | decision by one reviewer on one item | approve / return / correct + reason code + `reviewed_at` | I |
| `field_conflict` | field with competing primary sources | Holds the competing claim references | W |
| `conflict_resolution` | resolution of a conflict | Adopted claim, adoption rule, rationale, resolver | I |

### 5.7 Delivery infrastructure (C7)

| Table | Grain — one row per… | Purpose | Kind |
|---|---|---|---|
| `outbox_message` | side effect to be delivered at least once | Notifications, cache invalidation, CRM/integration webhooks, translation-staleness recomputation. Carries `topic`, `payload`, `dedup_key`, `available_at`, `attempts`, `status`, `last_error`, lease columns | W |

`outbox_message` is **deliberately mutable** and exempt from the C1 immutability rule: it is infrastructure, not history. A relay worker claims rows with `FOR UPDATE SKIP LOCKED`, and messages are pruned after successful delivery plus a retention window. Losing the table loses no business fact — `change_event` holds the history, and an outbox row can be regenerated from it.

The separation matters because the two have opposite lifecycles: business events must never change and never be deleted; delivery records must change on every retry and should be deleted once drained. Revision 3 conflated them by describing `change_event` as "the outbox", which would have forced retry bookkeeping onto an append-only table.

### 5.8 Localization, identity, operations

| Table | Grain — one row per… | Purpose | Kind |
|---|---|---|---|
| `translation` | entity × field_path × locale | **B3.** Derived value, method (`human`/`mt`/`mt_post_edited`), translator, optional review metadata, `source_version_no` for staleness | P |
| `user`, `role`, `permission`, `role_permission`, `user_role` | as named | RBAC | W |
| `api_client` | internal consumer | Hashed key, scopes, rate limit, IP allowlist | W |
| `session` | login session | BFF-held session | W |
| `alert` | open operational condition | `extractor_drift`, `source_blocked`, `consecutive_failures`, `field_stale`, `review_overdue` | W |

---

## 6. Immutable provenance approach (C1)

### 6.1 The rule

No row in `field_provenance`, `entity_version`, `audit_log`, `change_event`, `entity_relationship`, `snapshot`, `extraction`, `field_claim`, `claim_resolution`, `review_decision` or `conflict_resolution` is ever updated or deleted. Not by a service, not by an admin, not by the table owner. `UPDATE` and `DELETE` are revoked from every application role, and a `BEFORE UPDATE OR DELETE` trigger raises an exception as defense-in-depth.

### 6.2 How supersession is expressed without mutation

**Field-level supersession is version chronology.** `field_provenance` is keyed `(entity_type, entity_id, field_path, root_version_no)`. The current provenance for a field is the row with the greatest `root_version_no` for that key prefix — read either by index (`ORDER BY root_version_no DESC LIMIT 1`) or, on the hot path, by following the pointer in `field_current`. Nothing is marked superseded; being older *is* being superseded.

```
field_provenance  (append-only)
──────────────────────────────────────────────────────────────────────────────
entity            field_path     root_ver  field_status  value      snapshot
app_deadline:#a1  deadline_at    7         PUBLISHED     2027-01-15  snap_88
app_deadline:#a1  deadline_at    12        PUBLISHED     2027-01-29  snap_140   ← current
app_deadline:#a1  deadline_at    15        WITHDRAWN     NULL        snap_161   ← current after v15
```

Reading the field's history is one index scan. Reading its current state is one lookup. No row was ever rewritten.

**Entity-level supersession is an append-only relationship.** When one real entity replaces another — a program merged into another, a faculty restructured — an `entity_relationship` row asserts `MERGED_INTO` or `SUPERSEDED_BY` with an `effective_from` and the proposal that authorized it. The superseded entity's `canonical_id` keeps resolving (B6); the API reports the relationship rather than 404-ing or silently redirecting.

**Current-state pointers are projections, not history.** `entity_head` holds the current version per root; `field_current` holds the current status and provenance pointer per field. Both are mutable by design and rebuildable from history — which is exactly what D9 says projections are for. The distinction that matters: *a projection may be recomputed and overwritten; a history row may not.*

### 6.3 What this costs and why it is worth it

The cost is one extra table (`field_current`) and the discipline of never reaching for an `UPDATE` on history. The benefit is that "每条已发布字段可定位来源、快照、审核与版本" becomes structurally true rather than procedurally true: there is no code path, and no privilege, by which a published provenance record can be altered after the fact. That is the property the PRD is actually buying.

### 6.4 Rebuild semantics

A damaged `field_current` row or `entity_head` row is recomputed from `field_provenance` / `entity_version` by a maintenance task. A damaged canonical projection row is recomputed from the root's latest `entity_version.state_snapshot`. Per D9 this is a supported repair operation, not the read path, and no full-system replay is designed or tested.

---

## 7. Publication transaction after corrections

`publication.publish(proposal, actor)` — the only writer of canonical state. One database transaction:

1. **Authorize and re-validate segregation of duties (C2).** Confirm `actor` holds `proposal:publish`. Re-evaluate, against current data rather than values cached at assignment: (a) for a manually created proposal, `proposal.created_by != actor`; (b) for every item, if `item.corrected_by == actor` and `item.risk_level = high`, this must be review round 2 with a distinct round-1 corrector (**D7**). This check lives here and in `policy.authorize` — not in a `CHECK` constraint.
2. **Validate content.** Proposal status `approved`; every item decided; every high-risk item has ≥1 `field_claim` with a `snapshot` and a `review_decision`; value/status agreement per D2 and the kind/date matrix per C4; money items carry currency and billing_unit; no item referencing an unresolved `field_conflict`; hierarchy containment intact (offering belongs to the program, intake to the offering, round to the intake); and no round left in a state its deadlines contradict (§4.5).
3. **Lock the root and allocate a version (C3).** `SELECT … FOR UPDATE` on `entity_head` for the affected root, then `version_no = entity_head.current_version_no + 1`. Because the allocation and the matching `entity_head` update are both inside this transaction, a rollback rolls back the allocation — **no version number is consumed by an aborted publish**. The invariant delivered is: `version_no` is **unique per root and monotonically increasing**. Gaplessness is neither required nor claimed, so no future change (partitioning, archival, a bulk import path) is constrained by it.
4. **Append `entity_version`.** Full `state_snapshot` of the root's published state (including its offerings, intakes, rounds, deadlines, requirements and tuition) plus `diff_summary`. INSERT only; no `is_current` flag to flip.
5. **Append `field_provenance`.** One row per changed field, keyed `(entity_type, entity_id, field_path, root_version_no)`, carrying `field_status`, value, `risk_level`, source, snapshot, claim, `reviewed_by`/`reviewed_at`, `observed_at`, `published_at`, `effective_from`/`effective_to`, `trust_status`. **No prior row is touched (C1).**
6. **Append `fact_absence` rows** for any collection newly asserted as `OFFICIALLY_NOT_PUBLISHED` (**D15**).
7. **Update projections.** Canonical tables (values + inline `*_status` columns on high-risk tables per D14), `field_current` (status + provenance pointer for the rest), `entity_alias` if names changed, and `entity_head.current_version_no`. These are the only mutations in the transaction, and every one of them is recomputable.
8. **Append `entity_relationship`** if this publish enacts a supersession, merge or split (**B6**).
9. **Append `audit_log`** with before/after, reason, actor, request_id and the hash-chain link.
10. **Append `change_event`** — one immutable business-history row per published field change, carrying root + version, entity + field_path, old/new value and status, `change_kind`, `risk_level`, denormalized `destination_code` for feed filtering, and `published_at` (**D12**).
11. **Insert `outbox_message`** rows for the side effects this publish requires — reviewer/ops notification, cache invalidation, CRM or integration delivery, translation-staleness recomputation (**C7**). Same transaction, so no side effect can be lost and none can half-publish. Latest Updates does **not** go through the outbox: the feed reads `change_event` directly.

Commit. Then, outside the transaction: the relay worker drains `outbox_message` with `FOR UPDATE SKIP LOCKED` and retries with backoff; alerts related to the published fields are closed.

**Rollback** constructs a new proposal from a prior `state_snapshot`, publishes it forward through this same path, and records `rollback_of_version_id`. There is no delete path at any role.

---

## 8. Database constraints and indexes that are genuinely enforceable

Split honestly into what PostgreSQL can guarantee and what it cannot.

### 8.1 Enforceable by the database

**Referential integrity, including hierarchy containment.** Composite foreign keys make cross-parent mismatch impossible rather than merely unlikely. With `UNIQUE (id, program_id)` on `program_offering` and `UNIQUE (id, offering_id)` on `intake`:

```sql
-- prerequisites
ALTER TABLE program_offering ADD CONSTRAINT uq_offering_program UNIQUE (program_id, id);
ALTER TABLE intake           ADD CONSTRAINT uq_intake_offering  UNIQUE (offering_id, id);

-- nested-scope requirement ownership (C6) — no polymorphic columns
ALTER TABLE admission_requirement
  ADD CONSTRAINT fk_req_program  FOREIGN KEY (program_id)  REFERENCES program (id),
  ADD CONSTRAINT fk_req_offering FOREIGN KEY (program_id, offering_id)
        REFERENCES program_offering (program_id, id),
  ADD CONSTRAINT fk_req_intake   FOREIGN KEY (offering_id, intake_id)
        REFERENCES intake (offering_id, id),
  ADD CONSTRAINT ck_req_nesting  CHECK (intake_id IS NULL OR offering_id IS NOT NULL);

-- the rest of the chain
intake               (offering_id) → program_offering (id)
application_round    (intake_id)   → intake (id)
application_deadline (round_id)    → application_round (id)
tuition              (offering_id) → program_offering (id)
```
An offering can never attach to a program it does not belong to; a requirement can never point at an offering of a different program, or at an intake of a different offering; and `intake_id` without `offering_id` is rejected outright. `language_requirement` carries the identical four constraints. The `grain` column is `GENERATED ALWAYS AS (CASE WHEN intake_id IS NOT NULL THEN 'INTAKE' WHEN offering_id IS NOT NULL THEN 'OFFERING' ELSE 'PROGRAM' END) STORED`, so resolution queries can index and filter on grain without recomputing it.

**Unique natural keys.**

| Table | Unique on |
|---|---|
| `university`, `program` | `canonical_id` (immutable, B6) |
| `program_offering` | `(program_id, study_mode, campus_id, delivery_mode, duration_value, duration_unit)` with `UNIQUE NULLS NOT DISTINCT` — PG 15+, so a null campus (fully online) collapses correctly |
| `intake` | `(offering_id, academic_year, intake_season_id)` |
| `application_round` | two partial unique indexes, not one key (**C10**): `(intake_id, round_code)` where the code is controlled, and `(intake_id, round_label_norm)` where it is `INSTITUTION_DEFINED` |
| `application_deadline` | `(round_id, applicant_scope_id)` |
| `tuition` | `(offering_id, academic_year, student_category_id)` |
| `language_requirement` | `(scope_entity_type, scope_entity_id, applicant_scope_id, test_type_id)` |
| `entity_version` | `(entity_type, entity_id, version_no)` — **C3**'s uniqueness half |
| `field_provenance` | `(entity_type, entity_id, field_path, root_version_no)` |
| `field_current`, `entity_head` | primary keys on their grain |
| `source` | `url_hash` |
| `translation` | `(entity_type, entity_id, field_path, locale)` |
| `audit_log` | `(prev_hash)` where not null — makes chain forks impossible |

**Single-row CHECK constraints** — this is where D14 pays off:

```sql
-- ===================================================================
-- SOURCE-DATE COLUMN GROUP (C9, D17) -- shown with the deadline_ prefix;
-- identical constraints apply to every other use of the group.
--
-- The revision-4 form "PUBLISHED iff deadline_at IS NOT NULL" is WRONG
-- and is not used: it forced a date-only fact to invent a time and zone.
-- ===================================================================

-- Calendar parts degrade in one direction only: no month without a year,
-- no day without a month, no time without a day.
CHECK (deadline_month IS NULL OR deadline_year  IS NOT NULL)
CHECK (deadline_day   IS NULL OR deadline_month IS NOT NULL)
CHECK (deadline_time  IS NULL OR deadline_day   IS NOT NULL)
-- A timezone qualifies a time; alone it says nothing.
CHECK (deadline_timezone IS NULL OR deadline_time IS NOT NULL)
-- "mid-January" is a month-level fact, so it excludes a day.
CHECK (deadline_month_part IS NULL OR (deadline_month IS NOT NULL AND deadline_day IS NULL))
-- Range sanity, and a real calendar date (make_date is IMMUTABLE, so it may be
-- used here; it rejects 30 February rather than silently accepting it).
CHECK (deadline_month IS NULL OR deadline_month BETWEEN 1 AND 12)
CHECK (deadline_day   IS NULL OR deadline_day   BETWEEN 1 AND 31)
CHECK (deadline_day   IS NULL OR
       make_date(deadline_year::int, deadline_month::int, deadline_day::int) IS NOT NULL)

-- Precision is GENERATED, so it cannot contradict the parts:
deadline_precision text GENERATED ALWAYS AS (
    CASE
        WHEN deadline_year  IS NULL       THEN NULL
        WHEN deadline_time  IS NOT NULL   THEN 'DATETIME'
        WHEN deadline_day   IS NOT NULL   THEN 'DATE'
        WHEN deadline_month_part IS NOT NULL THEN 'MONTH_PART'
        WHEN deadline_month IS NOT NULL   THEN 'MONTH'
        ELSE 'YEAR'
    END
) STORED

-- An exact instant exists if and only if the source gave date + time + zone.
-- This is the constraint that makes "no invention" a database guarantee. There is
-- no UTC range: a source that stated no timezone gets no UTC representation at all.
CHECK ((deadline_instant_utc IS NOT NULL)
       = (deadline_time IS NOT NULL AND deadline_timezone IS NOT NULL))

-- CALENDAR-LOCAL query range (C13). A daterange, not a tstzrange: no time, no
-- zone, nothing about instants. GENERATED, so it cannot contradict the parts.
-- MONTH_PART never narrows the range: conservative whole-month coverage (C14).
deadline_cal_range daterange GENERATED ALWAYS AS (
    CASE
        WHEN deadline_year IS NULL THEN NULL
        WHEN deadline_day IS NOT NULL THEN daterange(
            make_date(deadline_year, deadline_month, deadline_day),
            make_date(deadline_year, deadline_month, deadline_day) + 1)
        -- MONTH_PART deliberately does NOT narrow the range (C14/N5). "mid-January"
        -- covers the whole of January here; month_part is preserved as structured
        -- metadata, and any narrower window is a configurable product policy.
        WHEN deadline_month IS NOT NULL THEN daterange(
            make_date(deadline_year, deadline_month, 1),
            make_date(deadline_year + deadline_month / 12,
                      deadline_month % 12 + 1, 1))
        ELSE daterange(make_date(deadline_year, 1, 1),
                       make_date(deadline_year + 1, 1, 1))
    END
) STORED
-- make_date, daterange, date + int and integer arithmetic are all IMMUTABLE, which
-- is what allows this to be a generated column rather than application-computed.

-- ===================================================================
-- DEADLINE KIND vs DATE (C4, revised for C9)
-- ===================================================================
CHECK ((deadline_field_status = 'PUBLISHED') = (deadline_kind IS NOT NULL))
CHECK (deadline_field_status = 'PUBLISHED' OR deadline_year IS NULL)
CHECK (deadline_kind IS NULL OR
       CASE deadline_kind
         WHEN 'FIXED_DATE'             THEN deadline_year IS NOT NULL
         WHEN 'ROLLING'                THEN TRUE   -- optional final cut-off
         WHEN 'UNTIL_FILLED'           THEN TRUE   -- optional nominal close
         WHEN 'NO_FIXED_DEADLINE'      THEN deadline_year IS NULL
         WHEN 'NOT_CURRENTLY_ACCEPTING'THEN deadline_year IS NULL
       END)
-- For every non-date kind the wording IS the value, so require it when published.
CHECK (deadline_field_status <> 'PUBLISHED' OR deadline_text IS NOT NULL)

-- value/status agreement on the other high-risk narrow facts (D14, D16)
CHECK ((amount_field_status    = 'PUBLISHED') = (amount           IS NOT NULL))
CHECK ((lifecycle_field_status = 'PUBLISHED') = (lifecycle_status IS NOT NULL))

-- ===================================================================
-- APPLICATION ROUND IDENTITY (C10)
-- sequence_no is display metadata and appears in NO unique constraint.
-- ===================================================================
CHECK (round_code <> 'INSTITUTION_DEFINED' OR round_label IS NOT NULL)

round_label_norm text GENERATED ALWAYS AS (
    lower(regexp_replace(btrim(coalesce(round_label, '')), '\s+', ' ', 'g'))
) STORED

-- A controlled code occurs at most once per intake.
CREATE UNIQUE INDEX uq_application_round_intake_code
    ON application_round (intake_id, round_code)
    WHERE round_code <> 'INSTITUTION_DEFINED';

-- Institution-defined rounds are identified by the institution's own wording,
-- normalised. Several may coexist in one intake, which the previous
-- UNIQUE (intake_id, round_code, sequence_no) NULLS NOT DISTINCT forbade.
CREATE UNIQUE INDEX uq_application_round_intake_label
    ON application_round (intake_id, round_label_norm)
    WHERE round_code = 'INSTITUTION_DEFINED';

-- Ordering support only. Not unique: two rounds sharing a sequence_no is a
-- display ambiguity, not an identity collision.
CREATE INDEX ix_application_round_intake_sequence
    ON application_round (intake_id, sequence_no NULLS LAST);

-- opens_* must precede that round's deadlines: cross-row, enforced in the
-- publication transaction, not here (see §8.2)

-- money is never a bare number
CHECK (amount IS NULL OR (currency_code IS NOT NULL AND billing_unit_id IS NOT NULL))
CHECK (amount IS NULL OR amount >= 0)

-- high-risk provenance must carry evidence and a reviewer (risk_level is denormalized
-- onto the immutable row at insert, which makes this a single-row check)
CHECK (risk_level <> 'high' OR
       (snapshot_id IS NOT NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))

-- temporal sanity (D3)
CHECK (effective_to IS NULL OR effective_to > effective_from)
CHECK (observed_at <= published_at)

-- a scope criterion must carry exactly one of value / value_ref
CHECK ((value IS NULL) <> (value_ref IS NULL))

-- an applicant_scope with zero criteria is the universal scope, flagged explicitly
CHECK (is_universal = false OR criterion_count = 0)
```

**Exclusion constraints** (`btree_gist`) where overlap is genuinely invalid — for example a status-history table per program, where two rows must not claim overlapping effective periods:

```sql
EXCLUDE USING gist (program_id WITH =, effective_period WITH &&)
```
Deliberately **not** applied to `field_provenance`: a correction legitimately produces two rows claiming the same effective period at different versions, and an exclusion constraint there would reject valid corrections.

**Four database identities (C11).** Separation of privilege only works if the
application never has the option of connecting as the owner. Four identities exist
from the outset, each with its own credentials:

| Identity | Used by | Owns schema? | Privileges once Step 3 lands |
|---|---|---|---|
| migration role (database owner) | Alembic only | **yes** | full DDL |
| `app_api` | request handling | no | `SELECT` on canonical projections |
| `app_worker` | Celery workers | no | `SELECT` on catalog; write evidence + proposals |
| `app_publisher` | `publication.publish()` | no | `INSERT`/`UPDATE` on canonical projections; `INSERT` on history |

Enforced now, before any privileges exist:

- There is **no shared `POSTGRES_USER`/`POSTGRES_PASSWORD`** for services to fall back
  on. Each role has its own `POSTGRES_<ROLE>_USER`/`_PASSWORD`.
- Settings validation rejects any configuration where `app_api`, `app_worker` or
  `app_publisher` resolves to the owning user, in **every** environment including
  local — a separation that only holds in production is the one that gets copied
  wrong.
- The async engine factory refuses `DatabaseRole.MIGRATION` outright, so the owning
  role cannot reach an application connection pool.
- Each role connects with its own pool and its own `application_name`, so
  `pg_stat_activity` attributes a session to a privilege level.
- The publisher session is deliberately *not* exposed as a FastAPI dependency; it is
  acquired inside `publication.publish()` only.

The bootstrap migration grants the runtime roles `SELECT` on `alembic_version` and
nothing else, because readiness must verify that migrations have been applied while
connected as its own role.

**Privilege-based immutability** — the backbone of C1:

```sql
REVOKE UPDATE, DELETE ON field_provenance, entity_version, audit_log, change_event,
       entity_relationship, snapshot, extraction, field_claim, claim_resolution,
       review_decision, conflict_resolution FROM app_reader, app_writer, app_publisher;
GRANT  INSERT, SELECT ON (those tables) TO app_publisher;
-- canonical projections: only the publisher may write
REVOKE INSERT, UPDATE, DELETE ON (canonical projection tables) FROM app_reader, app_writer;
GRANT  INSERT, UPDATE        ON (canonical projection tables) TO app_publisher;
```
This is what makes I1 and I2 real, which is why `infra/postgres/{roles,grants}.sql` is versioned beside the migrations.

**Triggers as defense-in-depth** — genuinely able to do cross-row work, unlike `CHECK`:
- `BEFORE UPDATE OR DELETE` on every append-only table → raise exception (catches even a mistaken migration).
- `BEFORE INSERT` on `entity_version` → reject `version_no <= current` for that entity, delivering **C3**'s monotonic half.
- `AFTER INSERT` on `review_decision` → recompute the SoD condition and reject a round-1 `approve` whose actor matches that item's `corrected_by`. **Defense-in-depth only; the authoritative check is §7 step 1 (C2).**
- `AFTER INSERT/UPDATE` on high-risk canonical tables → verify the inline status column agrees with the newest `field_provenance` row.
- `BEFORE INSERT` on `audit_log` → compute `row_hash` from `prev_hash` so the chain cannot be forged by the caller.

### 8.2 Not enforceable by the database — must live in the transaction and policy layer

| Rule | Why the DB cannot own it | Where it lives |
|---|---|---|
| Segregation of duties (**C2**) | Cross-row, cross-user authorization depending on role state at decision time | `policy.authorize` + §7 step 1; trigger only as a backstop |
| "Only `publication.publish()` writes canonical data" | Grants restrict the *role*, not which function used it | Single code path + code review + the grant as the outer fence |
| Risk classification | Reads a configurable matrix table | `detection` |
| The drift guard (**D4**) | Requires HTTP outcome, document validity, region diff and multi-run history | `detection` |
| SLA computation and escalation | Time-based scheduling | `review` + Beat |
| Ranking feature gate (**D8**) | Depends on licence validity at read time | `sources` + read-model filter |
| Applicant-scope resolution for a given applicant | Ranked matching over criteria (see **U1**) | `search` / read model |
| Translation staleness (**B3**) | Comparison against the field's current published version | `localization`, driven by an `outbox_message` post-commit |
| Round openness vs its deadlines (**C4/C5**) | Cross-row: a round's `lifecycle_status` must not contradict `NOT_CURRENTLY_ACCEPTING` or a passed `FIXED_DATE` on its deadlines | §7 step 2; a deferred constraint trigger as a backstop |
| `opens_at` precedes that round's deadlines | Cross-row across two tables | §7 step 2 |
| Version monotonicity (**C3**) | Compares against another row | §7 step 3 under row lock, plus the `BEFORE INSERT` trigger in §8.1 |

### 8.3 Indexes

| Purpose | Index |
|---|---|
| Field history and current provenance | `field_provenance (entity_type, entity_id, field_path, root_version_no DESC)` |
| Whole-program provenance load (**D13**) | `field_provenance (root_type, root_id, root_version_no DESC)` |
| Version timeline | `entity_version (entity_type, entity_id, version_no DESC)` |
| Latest Updates feed (**D12**) | `change_event (published_at DESC)`; `change_event (destination_code, published_at DESC)`; partial on `risk_level = 'high'` |
| Review queue | partial `change_proposal (sla_due_at)` WHERE `status IN ('pending','pending_second_review')` |
| Source health | `fetch_run (source_id, started_at DESC)`; partial WHERE `status <> 'ok'` |
| Snapshot dedupe | `snapshot (content_hash)` |
| Program search | GIN `pg_trgm` on `program.name_en`; generated `tsvector` column + GIN; `program_discipline (discipline_id, program_id)` |
| Institution search | GIN `pg_trgm` on `university.name_en`; `entity_alias (value)` trigram for alias hits |
| Deadline filters (**C9**, **C13**) | GiST on `application_deadline (deadline_cal_range)` for calendar-overlap filters across mixed precision — a `daterange`, so nothing in the index implies a timezone; B-tree on `(deadline_year, deadline_month, deadline_day, deadline_time)` for ordering; partial B-tree on `(deadline_instant_utc)` WHERE it is non-null, for exact-instant comparison on facts that have one; partial on `(round_id)` WHERE `deadline_kind IN ('ROLLING','UNTIL_FILLED')` so "still open" filters do not scan dated rows |
| Round lookup | `application_round (intake_id, sequence_no NULLS LAST)` — ordering only, not unique (**C10**) |
| Requirement resolution (**C6**) | `admission_requirement (program_id, grain, applicant_scope_id)`; partial `(offering_id)` WHERE `offering_id IS NOT NULL`; partial `(intake_id)` WHERE `intake_id IS NOT NULL` |
| Outbox relay (**C7**) | partial `outbox_message (available_at)` WHERE `status = 'PENDING'`; unique `dedup_key` |
| Fee filters | `tuition (offering_id, academic_year, student_category_id)`; `(currency_code, amount)` for range filters |
| Offering rollup | `program_offering (program_id)`; `(campus_id)` |
| Audit lookup | `audit_log (object_type, object_id, at DESC)`; `(actor_id, at DESC)` |
| Stale translations | partial `translation (entity_type, entity_id)` WHERE review/staleness flag set |
| Field QA sweeps | partial `field_current (field_status)` WHERE `field_status <> 'PUBLISHED'` |

---

## 9. Change detection, review, security

Unchanged from revision 2 except as noted:

- **Detection** (§8 of revision 2) keeps the three-layer cascade (content hash → region diff → semantic field diff), the DB-stored risk matrix, and the **D4** drift guard with its five conditions. Status transitions are compared as changes, so `PUBLISHED → OFFICIALLY_NOT_PUBLISHED` is detected rather than read as an absence. Withdrawal of a high-risk field is always a reviewed human decision.
- **Review** (§9 of revision 2) keeps the state machine `draft → pending → [pending_second_review] → published`, `returned` with mandatory reason codes and scoped single-source re-fetch, the 06:00/12:00/18:00 → 22:00 SLA, and **D7**'s second-review flow. Per **C2**, both SoD rules are policy-layer rules re-validated in the publication transaction.
- **Security / RBAC** (§10 of revision 2) is unchanged: OIDC preferred with 2FA for reviewer/admin, BFF-held sessions, six roles, private evidence behind signed URLs, sandboxed snapshot rendering on a separate origin, **D6** crawler compliance, no PII by design. Database identity separation is specified in §8.1 (**C11**).

### 9.1 Evidence integrity (C12)

Evidence integrity is guaranteed at the **application and database** level and does
not depend on any storage provider feature:

- every `snapshot` row carries the `sha256` of the stored bytes, so tampering with an
  object is detectable by re-hashing;
- `snapshot`, `extraction` and `field_claim` are append-only, with `UPDATE`/`DELETE`
  revoked from every application role (**C1**);
- storage keys are content-addressed (`…/{sha256}`) and the application writes **only
  if the key is absent** — an existing key is never overwritten, so a published
  field's evidence cannot change underneath it;
- a periodic verification job re-hashes a sample of objects referenced by published
  fields and raises an alert on mismatch;
- a snapshot referenced by any published field is never garbage-collected.

**Immutable retention at the storage layer is a deployment decision, not an
architectural assumption.** Local development enables bucket versioning only. WORM /
Object-Lock — whether compliance or governance mode is appropriate, and the retention
period — depends on the selected production object-storage provider and hosting
environment, so it is blocked on assumption **A16**. Object Lock must be enabled at
bucket creation and cannot be added afterwards, so it must be settled before the
first deployed bucket exists. It is recorded as an open deployment decision rather
than designed against MinIO's particular behaviour.

---

## 10. Repository tree

Unchanged from revision 2, with these additions:

```
scripts/verify-docker-stack.sh                  # asserts the Compose stack behaves
.node-version .nvmrc .npmrc                     # pinned Node, engine-strict
apps/api/src/app/domains/localization/          # B3: translation model + staleness
apps/api/src/app/domains/catalog/offerings/     # B2
apps/api/src/app/domains/taxonomy/scopes/       # B1: dimensions, scopes, criteria, qualification groups
apps/api/src/app/workers/tasks/reconcile.py     # C1: projection rebuild from history
infra/postgres/{roles.sql,grants.sql,triggers.sql}   # I1/I2/C1/C3 enforcement, versioned
docs/adr/                                       # D1–D15, B1–B7, C1–C3
```

---

## 11. Unresolved assumptions

### 11.1 Needed before or during schema work

| # | Question | Impact | My default if you do not decide |
|---|---|---|---|
| **U1** | **Applicant-scope resolution precedence.** When two scopes match one applicant — "China" and "China, 985-listed institution" — which requirement governs? | Read model, comparison UI, API semantics. Not the table shape, but the query layer built on it | Explicit integer `precedence` on `applicant_scope`, defaulting to most-specific-wins by criterion count, ties broken by precedence |
| **U2** | **Override or merge across grains.** A requirement exists at program level and again at offering level: does the narrower row fully replace the broader one, or merge field by field? | Resolution logic and how comparison renders partial overrides | Full replacement at record level — simpler to explain to a reviewer and to a consultant |
| **U5** | **Translation display gate.** Is Chinese shown immediately with a "machine translated" badge, or withheld until a human confirms it? | UI, editor workload; `translation.review_state` is already in the model either way | Shown with a visible provenance badge; official-language value always available alongside |
| **U6** | **Cross-destination fee comparison.** Is comparing UK "International" tuition with HK "Non-local" a valid operation in the compare view, and does it need an equivalence mapping? | Compare UI. The PRD forbids equivalence for grading systems; fees are a separate question | Comparable but never normalized — show both with their category labels and no conversion |
| **U7** | **Superseded-ID API semantics.** For a `SUPERSEDED_BY`/`MERGED_INTO` entity, does the internal API return 200 with the relationship, a 301, or a 410? | The CRM contract; interacts with A13 | 200 with `supersededBy` in the envelope; never a silent redirect |
| **N1** | **Controlled-vocabulary governance.** Who may add a `round_code`, a `scope_dimension` or a `student_category` — admin only, or editors with review? Does adding a term need an audit entry? | Whether vocabulary changes are config, governed data, or a deploy | Admin-only, audited, no deploy required |
| **N2** | **Does `NOT_CURRENTLY_ACCEPTING` belong on the deadline or on the round?** It is currently a published deadline statement, with round openness derived and contradictions blocked at publish (§4.5) | Where "is this round open?" is authoritative | Keep it on the deadline — it records what the page states; round lifecycle stays derived |
| **N3** | **Requirements applying to some but not all offerings** are duplicated per offering in MVP (§4.6) | Editor workload if the case is common | Accept duplication; upgrade to `requirement_set` + `requirement_applicability` only if measured pain |
| **N5** | Should `EARLY`/`MID`/`LATE` ever narrow a calendar filter window? | **Not in the database (C14).** The derived range covers the whole stated month; `month_part` is kept as structured metadata. If the business wants narrower windows, implement them as an explicit, configurable product policy in the query layer, never as immutable source semantics |
| **N4** | **Date-precision rendering convention.** Stored as the last instant of the period in `declared_timezone`; `deadline_at_precision` drives display so "mid-January" never renders as a false 31 Jan 23:59:59 | UI and API display contract | As described in §4.5 |

### 11.2 Carried forward, not blocking schema

| # | Question | Needed by |
|---|---|---|
| A4 | PRD §10 "待确认事项" has a heading and no content — likely a cut section | One question to the client |
| A5 | Denominator for "高风险变更发现率 ≥95%"; a shadow-check sample would add a small `verification_audit` table | Before metrics reporting |
| A6 | Performance thresholds, deferred by the PRD to engineering | I will propose p95 defaults with the implementation plan |
| A9 | Confirm low-risk `auto_publish_allowed` stays false by default | Before the monthly sweep ships |
| A13 | Which CRM/matching system consumes the internal API, with what auth and read volume | Before internal API v1 is frozen |
| A14 | Does "120 学院或校区关系" mean faculties, campuses, or `faculty_campus` rows | Before Day-N acceptance counting |
| A16 | Hosting region — crawl egress to UK/HK, S3 choice, cross-border rules | Before infra provisioning |
| A17 | Reviewer headcount and working hours; the 22:00 SLA is unfalsifiable without it | Before committing to the SLA (drives R5) |
| A18 | 3mir.cc treated as a UX reference only | Confirm at UI review |
| D5 | The 14-day / Day-3 timeline remains an **assumption**, recorded in `docs/assumptions.md` | Re-confirm at the Day-3 checkpoint |

---

## 12. Risks

Revision 2's R1–R13 stand. Three are re-scored by revision 3:

| # | Change |
|---|---|
| R4 | **Reduced.** B6's immutable `canonical_id` + alias + `entity_relationship` gives renames, merges and splits an explicit, auditable path instead of ad-hoc repair |
| R7 | **Reduced.** B5's `student_category` table plus the money CHECK constraints make a category- or unit-less fee unrepresentable |
| R10 | **Reduced but not eliminated.** C1 removes the mutable-history contradiction, and the temporal CHECKs catch inverted ranges. The remaining exposure is correct *interpretation* of the four timestamps in read models, which property-based tests in `tests/invariants/` must cover |

One new risk:

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| **R14** | **Offering multiplication.** B2's offering layer multiplies rows per program (study mode × campus × delivery × duration), which multiplies intakes, rounds, deadlines and tuition — and, because **C8** makes offerings governed, it multiplies reviewable facts too | Pilot may hit 600 programs while offerings, deadlines and fees lag; review capacity strains earlier than modelled | **U3** now fixed at 600 `program` rows with offerings reported separately. Measure real offering fan-out across the first five institutions in week 1 and re-forecast review load before committing to the SLA |

---

## 13. Status

Revision 13 adds safe acquisition and immutable evidence capture: decisions
**D33–D45** and corrections **C30–C43**, on top of revision 12's **D30–D32** / **C29**,
revision 11's **D25–D29** / **C28**, revision 10's **D18–D24** / **C22–C27**,
revision 9's **C18–C21** / **D17**, and **C1–C17**. Schema decisions **B1–B7** stand;
**U3**, **U4**, **U8**, **U9**, **U12**, **U13**, **U14** and **U15** are resolved.

The client's 181-institution target list is imported and governed. **The pilot is
fixed at 35 institutions**, named by the client's own file, with 385 claimed source
responsibilities over 319 distinct pages staged and every one of them `PENDING` in the
verification queue.

The 319 physical pages behind those claims are registered as acquisition targets and
can be fetched, with lease fencing, fetch-time SSRF validation, per-host politeness and
content-addressed storage. Every one is `FETCHABLE` and `NOT_ELIGIBLE`.

Twelve of those pages have been fetched from the real web under the controlled smoke set
(D38) — three cycles, 30 requests, the eight usable pages fetched three times each and
the blocked ones not re-asked. **Eight hold genuine content evidence** (7 HTML, 1 PDF);
one returned only a WAF challenge and is now `BLOCKED`, two are refused outright with
403, and one is a dead 404 in the client's workbook. Health over the twelve is
`HEALTHY 8 · BLOCKED 3 · FAILING 1`. Twenty snapshots resolve to eight distinct bodies,
because every page served byte-identical content across all three cycles. The object
store came back with zero missing objects and zero hash or size mismatches. Trust state
was unchanged by the run — still 319 `NOT_ELIGIBLE`, still 385 `PENDING`.

The run found three defects in our own code (C32–C34). Reconciling the *report* of it
against the database then found three more, all of them grain errors in the reporting
code rather than in the acquisition itself: the counts had been written from scrollback
(C35), "redirected off host" had been counting any redirect at all over the wrong grain
(C36), and the summary had mixed a pilot numerator with fleet-wide totals (C37).
`acquisition_smoke.py audit` now prints these counts from SQL rather than from anyone's
recollection.

Those two findings are **closed** by Step 5B.2 (D39–D42, C38): a `429` earns a cooldown
and a later retry rather than a permanent block, a resolver failure is distinguished
from an SSRF refusal, `cycle_key` is a filter rather than a label, one broken page no
longer ends a cycle, and there is an audited way back for every permanent state. See
[ACQUISITION §14](ACQUISITION.md#14-recovery-and-cycle-isolation-step-5b2).

Nothing about any institution is published. No domain is verified, no mapping is
promoted, no source has earned an eligibility class, and **no extraction exists** — no
`field_claim`, no `change_proposal`, no canonical fact has been produced from any page.

Remaining open items are **U1, U2, U5, U6, U7** and **N1–N3**, plus **N5** (§11.1);
**N4** is withdrawn, since C9 removed the convention it asked about. None changes table
shape, and each has a stated default recorded in `docs/assumptions.md`.

**Outstanding verification:** the Docker Compose stack has never been executed (no
Docker on the development machine). `scripts/verify-docker-stack.sh` asserts all
fourteen properties the Step 2 review listed; until it has been run, container
behaviour is an assumption. This now extends to the evidence store: **filesystem
storage has been exercised against real responses; `S3EvidenceStore` and MinIO have
not.** Tracked in `docs/assumptions.md`.

The two blockers this section used to name are closed. Fetch-time SSRF validation is
implemented and exercised against live DNS ([ACQUISITION §5](ACQUISITION.md#5-ssrf-and-the-limit-we-are-not-hiding));
the D6 access decision is enforced by refusing to work around a site that says no, which
the smoke run tested for real when two hosts said it.

**The full pilot cycle has run once, watched.** `pilot-full-001`: 319 pages, 343
requests, 30.3 minutes, 175 bodies captured over 174 distinct blobs, and zero internal
errors, zero lease losses, zero abandoned attempts and zero rate limits provoked from
120 hosts. The object store reported 0 missing objects, 0 hash mismatches and 0 orphans.
Trust state is unchanged: 0 publication-eligible sources, 0 verified domains, 0 promoted
mappings, and no `field_claim`, `extraction`, `change_proposal`, `field_provenance` or
canonical row of any kind.

**55% of the fleet yielded evidence**, and the other 45% is mostly not our problem to
fix: 64 refusals, 61 dead workbook URLs, 8 challenges, 7 TLS chain failures, 6 timeouts,
5 HTTP 202s and 2 NXDOMAINs. Full breakdown and the three decisions it raises in
[ACQUISITION §15](ACQUISITION.md#15-the-first-full-pilot-cycle-step-5b3).

**Document normalisation has run over the whole fleet.** All 175 body-bearing sources
are extracted at extractor version `1.0.0`: 173 `OK`, 2 `PARTIAL`, **0 `FAILED`**, into
174 distinct content-addressed artifacts totalling 11 MB. Every one resolves its full
lineage — `extraction → snapshot → fetch_run → source → pilot claims → institution` —
and artifact integrity is clean: 0 missing, 0 hash mismatches, 0 size mismatches, 0
orphans. A second pass created nothing.

Trust is untouched by it: 0 publication-eligible sources, 0 verified domains, 0 promoted
mappings, and `field_claim`, `extraction`-derived facts, `change_proposal`,
`field_provenance` and every canonical table still empty. **Parsing earns nothing.**

Next step: Step 5C.2, field-level business claims, built on 169 static-HTML documents of
which 51 carry JSON-LD (`BreadcrumbList` 31, `WebSite` 22, `CollegeOrUniversity` 21,
`WebPage` 20). It has not begun. See [EXTRACTION.md](EXTRACTION.md).
