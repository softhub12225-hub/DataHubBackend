# Target onboarding — client scope to verified sources

How the platform gets from "the client sent us a spreadsheet" to "we know where this
university publishes its tuition fees, and a named person confirmed it".

This is Step 4. It produces **no university facts whatsoever**, and it starts no
collection. What it produces is scope, official identity, and a map of where to look.

---

## 1. The rule this phase exists to protect

> University, programme and admission data must come from official university or
> authorised official sources.

The client supplied `QS_2027_世界前500_指定地区院校.xlsx`. It is a QS-derived list of
181 institutions. It answers exactly one question:

**which universities are in the project.**

It is authoritative for that. It is authoritative for nothing else — not for a
university's name, not its programmes, not its fees, not its deadlines. Treating a
ranking file as a description of a university is precisely the failure this platform
is built to prevent, so the separation is structural rather than a matter of
discipline:

| Concern | Where it lives | Who may write it |
|---|---|---|
| What the QS file said | `target_list_entry` (append-only, version-scoped) | `app_api` (INSERT only) |
| Whether we were asked to cover an institution | `target_institution` | `app_api` |
| What is true about an institution | `university` and the canonical tables | `app_publisher` **only** |

### A correction: grants are not the mechanism

An earlier version of this document claimed that role separation meant *"there is no
database identity capable of copying a value from the client's spreadsheet into
published university data."* **That was wrong.** `app_publisher` could `SELECT` the
onboarding tables and `INSERT`/`UPDATE` the canonical ones, so one identity was
enough. It was verified against a live database:

```
app_publisher SELECT qs_name FROM target_list_entry        → ALLOWED
app_publisher INSERT INTO university, no provenance at all → ALLOWED
app_publisher INSERT field_provenance citing no evidence   → ALLOWED
```

Grants classify *tables*. "This value may determine scope but may not become a fact"
is a statement about a value's **origin**, which no grant can express. So origin is
now explicit and checked — see [§11 Publication eligibility](#11-publication-eligibility-c27).

What role separation *does* establish, and it is still worth having:

- `app_api` and `app_worker` cannot write any canonical table at all.
- `app_publisher` now holds **no privilege** on the onboarding plane — not write, and
  since C27 not read either, including through the `target_source_coverage` view. It
  publishes reviewed claims and has no business seeing the client's scope list.

### QS ranking values specifically

`qs_rank` and `qs_score` are stored — on `target_list_entry`, tied to
`target_list.list_version` and a file SHA-256. They are *provenance for the client's
scope decision*, not a published ranking.

They reach `ranking_entry` only through an explicit mapping step that is **not
implemented**, and which requires both:

1. the `rankings` feature gate to be on (architecture D8), and
2. a licensing position that permits republication.

Neither is assumed. Onboarding does not wait on either: an institution progresses
through identity and source mapping with its QS rank sitting untouched in the import
record.

---

## 2. What the supplied workbook actually contains

Established by reading it, before any code was written:

| Property | Value |
|---|---|
| Sheet | `QS 2027 前500` (single populated sheet) |
| Excel table object | `QS2027Top500SelectedRegions`, `A13:F194` |
| Header row | 13 |
| Data rows | 181 (rows 14–194) |
| Preamble | rows 1–6, column A |
| Region summary | `H2:I11`, ending `合计 = 181` |
| Declared source | `QS World University Rankings 2027 官方 Excel v1.1（2026-06-18 发布）；共筛得 181 所院校。` |
| SHA-256 | `89f90407d99aa0e1162465709567c560c98237948469ae0712f7ed2904601993` |

Columns: 序号, QS 2027 世界排名, 院校名称（QS 官方英文）, 地区,
官方 Country/Territory, 综合得分.

Regional distribution, matching the client's stated figures exactly:

| | US | GB | AU | CA | NZ | HK | SG | MO |
|---|---|---|---|---|---|---|---|---|
| Institutions | 68 | 48 | 27 | 17 | 8 | 7 | 4 | 2 |

Three facts about the data that shaped the design:

- **149 distinct ranks across 181 rows.** Ties are normal — there is a four-way tie
  at 411. Rank is not an identifier and carries no uniqueness constraint.
- **QS naming is inconsistent.** The list contains `Essex, University of` (inverted)
  alongside `University of Bradford`, and `UCL` as a three-character name. A QS name
  is a match hint, never an institutional identity.
- **The file states its own totals twice** — in the preamble text and in the region
  summary block. Both are cross-checked against the rows actually read, which is
  what catches a truncated or re-filtered file.

The 181 institutions are **not written down anywhere in this codebase**. They are the
client's scope decision; hardcoding them would mean a corrected list could disagree
with the software, and the software would win.

---

## 3. Data model

Eight tables. Two are append-only history; six are mutable human working state.

```
target_list ──────────┐  one imported version of a client scope list
  │                   │  (name, version, source_description, source_url,
  │                   │   published_at, file_sha256, declared counts)
  │                   │
  ├─ target_list_entry │  APPEND-ONLY. One spreadsheet row, verbatim.
  │    (qs_name, qs_rank, qs_score, region_label, country_territory,
  │     source_row, destination_code)   ← the only home of QS values
  │                   │
  ├─ target_list_diff  │  APPEND-ONLY. ADDED_TARGET / REMOVED_FROM_NEW_LIST /
  │                    │  RANK_CHANGED / SCORE_CHANGED / NAME_CHANGED /
  │                    │  REGION_CHANGED
  │                   │
  └─ target_institution   client scope + onboarding progress. No QS attribute,
       │                  no institutional fact.
       │   match_key, onboarding_status, pilot_wave, is_in_current_list,
       │   matched_university_id ──────────────────→ university (canonical)
       │
       ├─ official_domain      hosts asserted or confirmed to be the institution's
       │     host, verification_status, verification_method, verified_by/at,
       │     covers_subdomains, authorization_reference, superseded_by_id
       │        │
       └─ source_mapping ──────┘  which official URL carries which category
             source_category, url, normalized_url, url_sha256, host,
             official_domain_id, verification_status,
             fetch_strategy, collection_priority, collection_frequency,
             access_state, promoted_source_id (NULL throughout Step 4)
                │
                ├─ source_degree_scope       UNDERGRADUATE / TAUGHT_POSTGRADUATE /
                │                            RESEARCH_POSTGRADUATE
                └─ source_discipline_scope    no rows = all disciplines
```

### Why the list is two tables

The specification's recommended `target_institution` shape carries `list_version`
alongside `onboarding_status`. Implemented literally, an institution appearing in both
QS 2027 and QS 2028 becomes two rows — and its onboarding progress, verified domains
and matched university fork with it. Re-importing a list would then either duplicate
the work or silently discard it.

Split instead:

- **`target_list_entry`** — immutable, one row per (list version, spreadsheet row).
  What that file said.
- **`target_institution`** — one row per institution in scope, carrying governance
  state only.

Every field the specification names is present; the QS-originated ones are reached
through the entry. That split is also what makes the required difference report and
the multi-version history possible at all.

### Onboarding states

```
NOT_STARTED → IDENTITY_VERIFICATION → DOMAIN_CANDIDATE → DOMAIN_VERIFIED
            → SOURCE_MAPPING → READY_FOR_COLLECTION → ACTIVE

              BLOCKED          ← terminal until a human acts
              NEEDS_MANUAL_REVIEW
```

`SOURCE_MAPPING` and beyond require `matched_university_id IS NOT NULL`, enforced by
CHECK. That constraint is the structural reason a QS row cannot become a collectable
institution on its own.

`BLOCKED` and `NEEDS_MANUAL_REVIEW` require a `blocked_reason`, so the state is
actionable rather than a dead end nobody can interpret.

---

## 4. Importing a list

```bash
# See what a new list would change, without committing it:
uv run python apps/api/scripts/import_target_list.py path/to/list.xlsx --dry-run

# Commit it:
uv run python apps/api/scripts/import_target_list.py path/to/list.xlsx
```

### Structural, not positional

The parser finds the worksheet by name (or takes the only populated one), then finds
the header row by **matching column labels against patterns**. `QS 2027 世界排名` and
`QS 2028 世界排名` both resolve to the rank column, so next year's file needs no code
change. Chinese and plain-English headers are both recognised.

List metadata is transcribed from the file's own preamble — name, version,
`source_description`, `source_url`, `published_at`. Nothing is asserted on the file's
behalf: a file stating no publication date leaves `published_at` NULL rather than
acquiring today's date, consistent with the platform's refusal to manufacture
precision (D17).

### Validation

Every problem is collected and reported together, because an operator fixing a
supplied file wants the whole list, not the first line that failed.

| Check | Why |
|---|---|
| Expected sheet | importing the wrong sheet of a multi-sheet file imports a different scope, silently |
| Required columns present | a file with no rank or name column is not this kind of list |
| No duplicate institutions | double-counts scope; two rows race to own one match key |
| Region recognised | an unmapped region cannot be placed, and guessing puts an institution in the wrong market |
| Rank numeric | `501+` is not a rank we can store. Ties are legal and expected |
| Score numeric if present | absent is fine; `n/a` is not |
| Row count vs the file's own totals | catches a truncated or re-filtered paste |

A region that cannot be mapped is a **warning, not a refusal**: the institution is
still in scope, it just cannot be placed yet, so it is imported with
`destination_code = NULL` and moved to `NEEDS_MANUAL_REVIEW`.

An unrecognised region is never approximated to a neighbour. Adding a destination is
a seed change; adding a synonym is a change to `domains/onboarding/regions.py`.

### Idempotency and versioning

- **Same bytes** → recognised by SHA-256, nothing written, a report describing the
  existing import. "Run it again" is a normal operational reflex and must be safe.
- **Same `(name, version)`, different bytes** → refused. Overwriting destroys the
  history the difference report is computed from, and a second silent "v1.1" makes
  "which v1.1?" unanswerable. Supply an explicit `--list-version`, which is also an
  accurate description of what a corrected file is.
- **New version** → new `target_list` row, new entries, and a diff against the
  previous list of the same name.

### Removal is never deletion

A later list that omits an institution sets `is_in_current_list = false` and records
`removed_from_list_id`. Nothing is deleted:

- its historical `target_list_entry` rows stay untouched;
- its verified domains and mapped sources stay;
- its matched `university` and every piece of that university's evidence and history
  stay.

If it reappears in a later list, the accumulated onboarding work is still there.
Scope shrinking is a statement about scope, not a licence to destroy governed data.

### A rename surfaces as a pair, deliberately

Name normalisation (`domains/onboarding/naming.py`) folds case, Unicode form,
whitespace and decorative punctuation — and nothing else. No stopword removal, no
parenthetical stripping, no abbreviation expansion, no fuzzy matching.

So `Essex, University of` becoming `University of Essex` between versions produces an
`ADDED_TARGET` plus a `REMOVED_FROM_NEW_LIST`, which a human resolves by matching
both targets to the same university. That is the accepted cost: a wrong merge is
silent and corrupts scope, whereas a visible pair is a worklist item. Given the
choice, this errs toward the visible failure.

---

## 5. Establishing official identity

```
target institution
   → candidate university (human research)
   → candidate domain          official_domain, status CANDIDATE
   → verification              a named person, a method, evidence
   → matched canonical university   target_institution.matched_university_id
```

Nothing in this chain is automatic.

**A similarly named domain is a `CANDIDATE` and nothing more.** Leaving `CANDIDATE`
for a trusted status requires a `verification_method`, a `verified_at` and a
`verified_by`, enforced by CHECK. `DomainVerificationMethod` has no member meaning
"the name matched" or "it was the first search result" — a reviewer must name a
government registry, an accreditation body, a certificate subject, a self-declaration
page, or their own documented review.

**Third-party directory data is never publishable evidence**, and nothing in this
module ingests any.

### Statuses

| Status | Meaning |
|---|---|
| `CANDIDATE` | proposed, unconfirmed. The default. Counts toward nothing |
| `VERIFIED_OFFICIAL` | confirmed to be the institution's own host |
| `AUTHORIZED_EXTERNAL` | a third party the institution authorised. **Not official** |
| `REJECTED` | confirmed not to be the institution's. Kept, inactive |
| `LEGACY` | was official, now retired. Evidence from its period stays valid |

`AUTHORIZED_EXTERNAL` is the load-bearing one. Many universities run admissions on
third-party SaaS — application portals, fee calculators, prospectus hosts. Such a host
legitimately carries official information *because a university authorised it*, which
is a recorded human decision, **not because the university links to it**. So:

- it is a distinct status from `VERIFIED_OFFICIAL`;
- it requires an `authorization_reference` naming what was actually relied on
  (a delegation, a contract, a written confirmation), enforced by CHECK;
- a database trigger refuses to let an `AUTHORIZED_EXTERNAL` host back a
  `VERIFIED_OFFICIAL` source mapping.

No code path can promote "linked from the official site" into "is the official site".

### One institution, many hosts

A main domain, `www`, a postgraduate subdomain, faculty subdomains, a legacy domain
that still resolves, an application portal. All are registered; what differs is
status. `covers_subdomains` makes subdomain trust explicit rather than inferred.

A partial unique index gives a host **at most one trusted owner** — two institutions
cannot both hold a host as officially theirs. It is partial so rejections accumulate
freely: keeping a rejection is what stops the same wrong domain being re-proposed
every time someone searches for the institution.

---

## 6. Mapping sources

A `source_mapping` row says: *this URL carries this category of official information
for this institution, for these applicant audiences.*

Fifteen categories: `UNIVERSITY_HOME`, `UNDERGRADUATE_ADMISSIONS`,
`POSTGRADUATE_ADMISSIONS`, `PHD_ADMISSIONS`, `PROGRAM_CATALOG`, `FACULTY_OR_SCHOOL`,
`PROGRAM_PAGE`, `ENTRY_REQUIREMENTS`, `LANGUAGE_REQUIREMENTS`, `TUITION_FEES`,
`APPLICATION_DEADLINES`, `OFFICIAL_PDF`, `ACADEMIC_CALENDAR`, `GOVERNMENT`,
`AUTHORIZED_RANKING`.

These are categories of **content we need**, not path templates. Universities do not
share a website structure: one may satisfy `ENTRY_REQUIREMENTS` from a page per
programme and another from one table for the whole institution. Both are mapped, and
coverage is satisfied either way.

### Bachelor, Master and PhD are mapped separately

`DegreeScope` — `UNDERGRADUATE`, `TAUGHT_POSTGRADUATE`, `RESEARCH_POSTGRADUATE` — is a
different axis from `degree_level`. The award a student receives and the admissions
audience a university's site is organised around are not the same thing, and
conflating them is how a taught-Masters fee page gets read as authoritative for PhD
funding.

Audience lives in a child table, so **absence is meaningful**: a mapping with no
`RESEARCH_POSTGRADUATE` row does not cover doctoral study, and the coverage report
says so rather than assuming a postgraduate page speaks for PhD applicants. At nearly
every institution in the client's list these are different pages run by different
offices.

### Governance record, not crawler target

`source_mapping` is the governance record of where an institution publishes what. Its
counterpart in the evidence plane is `source`, the crawler's registered target with
its robots/ToS decisions and fetch history.

A mapping becomes a source when it is verified *and* collection is authorised.
`promoted_source_id` records that crossing, is unique, and is **NULL for every row
throughout Step 4**. A CHECK additionally forbids promoting an untrusted mapping.

Acquisition configuration is carried but inert: `fetch_strategy` (HTTP / BROWSER /
DOCUMENT / MANUAL), `collection_priority`, `collection_frequency`, `host`,
`access_state`, `is_active`. There is deliberately **no university-specific parser
logic** anywhere in this module — per-institution extraction rules belong to the
extraction phase, and putting them here would make onboarding a place where scraping
code accumulates.

---

## 7. Coverage

`target_source_coverage` is a view answering, per institution in scope: is its
identity settled, and is there a verified official source for each category the
product needs?

```sql
SELECT match_key, destination_code, coverage_status
  FROM target_source_coverage
 WHERE is_in_current_list AND coverage_status <> 'SOURCE_MAPPING_COMPLETE'
 ORDER BY coverage_status, destination_code;
```

Columns: `identity_verified`, `has_homepage`, `has_undergraduate_admissions`,
`has_taught_postgraduate_admissions`, `has_research_postgraduate_admissions`,
`has_program_catalog`, `has_entry_requirements`, `has_language_requirements`,
`has_tuition`, `has_deadlines`, plus verified/candidate counts.

`coverage_status` is a funnel, so the report names the *actual* blocker:

| Status | Meaning |
|---|---|
| `BLOCKED` | onboarding is blocked or escalated; see `blocked_reason` |
| `IDENTITY_NOT_VERIFIED` | no matched university yet. Sources are not the problem |
| `NO_SOURCES_MAPPED` | identity settled, nothing verified |
| `SOURCE_MAPPING_INCOMPLETE` | some categories verified, some missing |
| `SOURCE_MAPPING_COMPLETE` | every required category has a verified source |

**Only active, trusted mappings count.** A candidate URL is a guess, and counting it
would make the report claim readiness to collect from a page nobody has confirmed —
exactly the failure the platform exists to prevent.

The required-category contract lives in Python (`coverage.REQUIRED_COVERAGE`) and the
view lives in the migration; a test asserts the two agree.

---

## 8. Manual verification

Service functions in `domains/onboarding/verification.py`. No HTTP endpoints and no
review console yet — exposing them now would mean shipping a UI against an unreviewed
API surface.

| Action | Function |
|---|---|
| VERIFY | `verify_domain`, `verify_source_mapping` |
| VERIFY (authorised third party) | `authorize_external_domain` |
| REJECT | `reject_domain`, `reject_source_mapping` |
| REPLACE | `replace_domain` |
| MARK LEGACY | `mark_domain_legacy` |
| REQUEST_REVIEW | `request_review` |

Each records the actor, the time, the reason, the candidate and the decision, and
appends to the existing hash-chained `audit_log`. A reason is mandatory; a blank one
is refused.

A rejection is **not reversed in place**. Register a fresh candidate instead, so the
rejection stays on the record.

### Lock order

Every operation takes locks in one direction only:

```
official_domain → target_institution → source_mapping → audit_chain_head
```

- Work rows first, the audit chain **last**. `audit_chain_head` is the most contended
  lock in the system and is held to end of transaction (C18); taking it first would
  make every verification queue behind every other write.
- Consistency matters as much as order. `reject_domain` goes host-then-mappings, so
  `verify_source_mapping` locks the host *before* the mapping it is changing — a
  second caller going mapping-then-host would close a deadlock cycle.
- Two hosts in one operation (a replacement) are locked ordered by primary key, so
  two reviewers replacing hosts in opposite directions cannot deadlock.
- The `source_mapping_requires_trusted_host` trigger takes `FOR SHARE` on
  `official_domain`, which fits inside this order rather than adding to it.

---

## 9. URL safety

`domains/onboarding/urls.py` validates every URL before it is stored. It refuses:

- any scheme but `http`/`https`;
- embedded credentials (`user:pass@`, and `host@1.2.3.4` — where everything before
  the `@` is credentials and the request goes to the address after it);
- IP literals, including bracketed IPv6, and the abbreviated, octal and decimal
  spellings (`127.1`, `0177.0.0.1`, `2130706433`) that Python's `ip_address` rejects
  as invalid but the OS resolver happily accepts as loopback;
- `localhost`, `.internal`, `.local`, `.intranet`, `.onion` and similar, matched on
  the whole name or a dot-suffix so `localhost-college.ac.uk` is not caught;
- ports outside 80/443/8080/8443;
- whitespace and control characters.

The database enforces the floor independently: `url ~* '^https?://'` is a CHECK on
`source_mapping`, and on `target_list.source_url` too. **Importing a spreadsheet or
pasting a link is not a way past the guard.**

### What this does *not* establish

> Registration-time validation is a necessary filter and an insufficient one.

It cannot establish that a hostname resolves to a public address. DNS is deliberately
not consulted: a resolution performed at registration says nothing about the
resolution performed at fetch time. `evil.example.com` may answer `93.184.216.34`
today and `169.254.169.254` when the crawler runs. Nor can it establish that a
redirect chain stays public — a permitted URL may 302 to `http://127.0.0.1:6379`.

**The acquisition phase must therefore apply its own SSRF controls at fetch time**:
resolve the hostname, reject non-public addresses across *every* A/AAAA answer, pin
the connection to the vetted address, re-run the check on every redirect hop, and cap
the hop count. Those are the real defence. This module only ensures a URL that was
never allowed to be stored cannot reach them at all.

### Reading a workbook

An `.xlsx` is untrusted input even from the client. `domains/onboarding/workbook.py`
inspects the archive before extraction (entry count, per-entry and total uncompressed
size, compression ratio, traversing names), and reads with `data_only=True` so a
`=WEBSERVICE(...)` cell yields its stored value and nothing is dereferenced. Excel
`~$` lock files are recognised and refused with a clear message rather than a parse
error — the client's real file arrived beside one.

**No network access, at any point in the import.**

---

## 10. What Step 4 does not do

- No university, programme, campus, faculty, intake, requirement, fee or deadline is
  created. Asserted by test across 31 tables.
- No `field_claim`, `change_proposal`, `review_task` or `claim_resolution`.
- No `ranking_entry`. QS values stay on the import record.
- No `source` row, no `fetch_attempt`, no `snapshot`. **No crawler has been started**,
  and no mapping has been promoted for collection.
- No pilot wave assigned.
- No HTTP endpoints, no frontend.
- No fetching of any kind, including of the QS download URL the workbook names.

Still true after the collection workbook is imported. The returned workbook lands in
a separate staging plane (`pilot_*`), which creates none of the above either — see
[`PILOT_COLLECTION.md` §8](PILOT_COLLECTION.md#8-the-staging-plane-u12) and
architecture decision D25. A collected URL becomes a `PENDING` candidate; it is not a
`source`, and nothing has been fetched.

---

## 11. Publication eligibility (C27)

Every `source` carries a class saying what it may substantiate. Triggers refuse
evidence whose class does not permit the fact being asserted.

| Class | May support | Notes |
|---|---|---|
| `TARGET_SCOPE_ONLY` | **nothing** | The client target workbook; QS name/rank/score. Sets project scope and no more |
| `OFFICIAL_VERIFIED` | any fact about the institution | A verified university or government/regulator source |
| `AUTHORIZED_EXTERNAL` | only its authorisation scope | A third party the institution authorised. Scope is `source_field_binding` rows |
| `AUTHORIZED_RANKING` | ranking facts only | Needs a live, display-allowed `source_authorization`. Nothing holds this class today (U9) |
| `NOT_ELIGIBLE` | nothing | **The default.** A newly registered source substantiates nothing until classified |

### How the class is obtained

It is earned, not asserted. `source_mapping.publication_eligibility` is a
**generated** column derived from `verification_status` and `source_category` —
PostgreSQL refuses to write a generated column for every role including the table
owner, so that link is unassertable rather than merely constrained. Category wins, so
a ranking page is a ranking source wherever it is hosted.

`source_eligibility_is_earned` then requires that a class above `NOT_ELIGIBLE` has an
active promoted mapping vouching for it, whose derived class matches and whose
`url_sha256` equals the source's `url_hash`. That last equality closes the swap where
a legitimately classified source is later repointed at a spreadsheet.

The promotion order is forced by the design, and there is no ordering that avoids it:

1. the source exists, unclassified — the mapping's `promoted_source_id` is a foreign
   key to it;
2. the mapping is promoted, pointing at that source;
3. only then is the source classified.

### Where it bites

- `field_claim` (written by `app_worker`) and `field_provenance` (written by
  `app_publisher`) both check it. Every anchor resolves to exactly one source:
  `snapshot.source_id` is `NOT NULL`, and `claim → extraction → snapshot → source` is
  `NOT NULL` end to end, so there is no anchor from which a source cannot be resolved.
- A citation must also hang together — a provenance row may not name one source and a
  snapshot belonging to another, nor a claim about a different field.
- Anything other than `NOT_CHECKED` must cite *something* (CHECK). Before C27 a
  `PUBLISHED` row could cite nothing at all, which made the whole concept bypassable.
- Eight canonical columns require matching published provenance before commit, via
  `DEFERRABLE INITIALLY DEFERRED` constraint triggers: `university.name_en`/`name_zh`,
  `entity_alias.value`, `ranking_entry.rank_value`/`rank_low`/`rank_high`/`score`,
  `tuition.amount`.
- `target_list_entry` and `target_institution` are not merely refused as evidence —
  they are **unnameable**. `field_provenance` can only anchor to `source`, `snapshot`
  or `field_claim`, and a test asserts the audit spine has no foreign key into the
  onboarding plane.

Triggers are real enforcement here because `app_publisher` owns no table
(`ALTER TABLE ... DISABLE TRIGGER` fails with "must be owner") and cannot
`SET session_replication_role`. Both were tested, not assumed. The gates are
additionally `ENABLE ALWAYS`.

### What this does **not** enforce

**Value fidelity.** Nothing in a database can tell where a string came from. A
publication service that reads a QS name, writes it to `university.name_en` and cites
a genuinely official source *for that exact string* produces a row indistinguishable
from an honest one. The governed-column triggers require *a* citation; they cannot
know the citation is truthful.

**Coverage is eight columns, not two hundred.** Every other canonical column can still
be written without provenance. Extending the set is one line in
`GOVERNED_CANONICAL_COLUMNS` plus one trigger, but the honest statement today is that
provenance is mandatory for the eight values most likely to carry a laundered string.

### What Step 9 (publication) must therefore do

Not optional, and not inferable from the schema:

1. **Compare every published value against the cited claim's `value_normalized`** and
   refuse a mismatch. This is the real defence against hand-copying, and it is the
   only place it can live.
2. **Re-check eligibility at publication time**, not only at claim time. A licence can
   expire between the two; `AUTHORIZED_RANKING` is checked against `expires_at` on
   every insert, but a source's class is not re-derived when its mapping changes.
3. **Write provenance in the same transaction** as the canonical row. The
   governed-column triggers are deferred precisely so either order works, but both
   must be present at commit.
4. **Never read the onboarding plane.** It has no grant to; if a future change needs
   one, that is the moment to ask why.

---

## 12. Verified against the client's real file

```
181 rows read, 181 entries written, 181 new institutions
differences: ADDED_TARGET=181
by destination: US 68, GB 48, AU 27, CA 17, NZ 8, HK 7, SG 4, MO 2
pilot_wave set: 0        matched_university: 0
university: 0   field_claim: 0   change_proposal: 0   ranking_entry: 0
coverage: IDENTITY_NOT_VERIFIED = 181
re-run: already imported, nothing written
```

`IDENTITY_NOT_VERIFIED = 181` is the honest state immediately after import: scope
known, nothing verified, nothing published.
