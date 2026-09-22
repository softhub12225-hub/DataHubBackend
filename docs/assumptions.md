# Assumptions and open questions

Every item here is an assumption **we** made, not a requirement the client stated.
Each is implemented as described unless overridden. Review at the first client
checkpoint.

Resolved decisions live in [ARCHITECTURE.md](./ARCHITECTURE.md) §1 (D1–D17, B1–B7,
C1–C13) and in [adr/](./adr/).

---

## Timeline — the one that affects planning most

**A1 / D5.** The PRD contradicts itself on the pilot duration: the title says 两周
(two weeks), the header table says 试点周期 = 3 个自然日 (3 calendar days), §0 refers
to a "第14天的交付门槛" and §8 to "第3天硬性验收". The chat history says 5 days, then
3.

**Working assumption:** 14 days is the overall MVP/pilot implementation period, and
day 3 is the first hard milestone / review checkpoint.

**Status:** unconfirmed. Re-confirm at the day-3 checkpoint before any commitment
depends on it.

---

## Open questions with stated defaults

None of these change table shape. All are implemented as the default below.

| # | Question | Default applied |
|---|---|---|
| **U1** | Applicant-scope precedence when two scopes match one applicant (e.g. "China" and "China, 985-listed institution") | Explicit integer `precedence` on `applicant_scope`; most-specific-wins by criterion count, ties broken by `precedence` |
| **U2** | A requirement exists at program level *and* offering level — replace or merge? | Full replacement at record level |
| **U5** | Is Chinese text shown immediately with a "machine translated" badge, or withheld pending human confirmation? | Shown with a visible provenance badge; the official-language value is always available alongside |
| **U6** | Is UK "International" vs HK "Non-local" a valid tuition comparison? | Comparable but never normalised — both rendered with their category labels, no conversion |
| **U7** | Internal API semantics for a superseded or merged entity id | HTTP 200 with `supersededBy` in the envelope; never a silent redirect |
| **N1** | Who may extend controlled vocabularies (`round_code`, `scope_dimension`, `student_category`)? | Admin only, audited, no deploy required |
| **N2** | Does `NOT_CURRENTLY_ACCEPTING` belong on the deadline or on the round? | On the deadline — it records what the page states. Round openness is derived, and the publication transaction blocks contradictions |
| **N3** | Requirements applying to some but not all offerings of a program | Duplicated per offering in MVP. Upgrade path (`requirement_set` + `requirement_applicability`) is additive and loses no constraint |
| ~~**N4**~~ | ~~Date-precision storage and rendering~~ | **Resolved by C9** — no longer an open question. Temporal facts store the calendar parts the source published, derive precision from them, and hold an exact UTC instant only where the source gave date + time + zone. Nothing is manufactured, so there is no convention to agree |
| **N5** | Should `EARLY`/`MID`/`LATE` narrow a calendar filter window? | **Not in the database (C14).** The derived range covers the whole stated month; `month_part` stays as structured metadata. Narrower windows would be an explicit configurable product policy in the query layer, never immutable source semantics |

---

## Carried forward — needed before a specific feature, not before the schema

| # | Question | Needed by |
|---|---|---|
| **A4** | PRD §10 "待确认事项" has a heading and no body — likely a cut section with unresolved decisions | Worth one question to the client |
| **A5** | How the denominator of "高风险变更发现率 ≥95%" is established. A manual shadow-check sample would add a small `verification_audit` table | Before metrics reporting |
| **A6** | Performance thresholds; the PRD defers these to engineering | Proposed with the implementation plan |
| **A9** | Confirm low-risk `auto_publish_allowed` stays `false` by default | Before the monthly sweep ships |
| **A13** | Which CRM / matching service consumes the internal API, with what auth (mTLS vs API key) and what read volume | Before internal API v1 is frozen |
| **A14** | Does "120 学院或校区关系" mean 120 faculties, 120 campuses, or 120 `faculty_campus` rows? | Before day-N acceptance counting |
| **A16** | Hosting region — affects crawl egress to UK/HK sources, S3 choice, latency and cross-border data rules | Before infrastructure provisioning |
| **A17** | Reviewer headcount and working hours. The 22:00 review SLA is unfalsifiable without this | Before committing to the SLA (drives risk R5) |
| **A18** | 3mir.cc is treated as a UX reference only; no requirement was derived from it | Confirm at UI review |

---

## Outstanding verification (Step 2)

| Item | Status | How to close it |
|---|---|---|
| **Docker Compose stack** | **UNVERIFIED.** No Docker, Docker Desktop, Podman or WSL exists on the development machine, so the stack has never been executed. Image builds, healthchecks, `depends_on` conditions, volume permissions, the Postgres init path and non-root users are all unconfirmed. | On a Docker-capable machine: `bash scripts/verify-docker-stack.sh`, then `make check`. The script asserts all fourteen properties and prints PASS/FAIL. Record the result here. |
| Object-lock / WORM retention | Open deployment decision (**C12**), dependent on the production provider and hosting environment (**A16**). Must be settled before the first deployed bucket exists, because Object Lock cannot be enabled after bucket creation. | Decide provider, mode (compliance vs governance) and retention period alongside A16. |

Everything else in Step 2 was executed and passed; see the Step 2 report for the
evidence. Local verification substituted portable PostgreSQL 16.9 and Redis for the
Compose dependencies, which exercises the application but **not** the container
topology.

## Step 3 (domain schema) notes

| Item | Status |
|---|---|
| Migrations, privileges, invariants, indexes, seeds | Implemented and verified against PostgreSQL 16.9: 14 revisions, 63 tables, 172 tests passing |
| `<p>_precision` / `grain` as generated `text` not enum | **C15.** Forced by PostgreSQL: a generated expression must be IMMUTABLE and the text-to-enum cast is STABLE. A CHECK constrains the values |
| `lifecycle_status` nullable on program and offering | **C16.** `NOT NULL DEFAULT 'ACTIVE'` asserted that an unverified programme was open. Found by the invariant tests |
| Query-layer indexes excluded from autogenerate | **C17.** One shared name list in `app.db.classification` keeps the migration and the `include_object` exclusion in step |
| Impossible calendar dates (30 February) | Rejected by `make_date` inside the generated column rather than by the named CHECK, because generated columns evaluate first. The row is refused either way; only the message is less specific |
| `alembic upgrade --sql` on Windows | Needs `PYTHONIOENCODING=utf-8`: migration comments contain Chinese and the console defaults to cp1252. Set in the Makefile target and in the test harness |
| `user` / `session` table names | Renamed `app_user` / `user_session`: both collide with SQL reserved words, and an unquoted `SELECT ... FROM user` returns the current role rather than the table |

## Step 4 (target onboarding) notes and open items

Design: [`docs/ONBOARDING.md`](ONBOARDING.md).

| Item | Status |
|---|---|
| Import, domain registry, source mapping, coverage | Implemented and verified against PostgreSQL 16.9: revision 0016, 75 tables, 403 tests passing. The client's real workbook imports to exactly 181 targets with the stated regional distribution |
| Migrations 0002 and 0012 read live application state | **C22.** Same defect as C21, in two more places. Both froze; completeness now asserted against the built schema by tests. Worth noting as a pattern: three separate revisions made this mistake, so a *fresh-database* build is the only check that catches it |
| `FetchStatus` was missing `ABANDONED` | **C23.** The enum test compared type names, not members. Now compares members in both directions |
| `Faculty.parent` mapper warning | **C24.** Latent since Step 3; invisible because the integration tests speak raw SQL and nothing configured the mappers until the importer used the ORM |
| JSONB `None` stored as JSON `null` | **C25.** SQLAlchemy's default. Fixed with `none_as_null=True` on the onboarding columns; worth checking if a future CHECK depends on a JSONB column being SQL NULL |

### Open items arising from Step 4

| # | Question | Stated default until answered |
|---|---|---|
| ~~**U8**~~ | ~~Which institutions form the pilot wave?~~ | **RESOLVED — see the decisions table below** |
| ~~**U9**~~ | ~~Is QS ranking data licensed for republication?~~ | **RESOLVED — see the decisions table below** |
| **U10** | Does the client want a renamed institution recognised across list versions, or surfaced as an added/removed pair for a human to merge? | Surfaced as a pair. Name normalisation is deliberately conservative (see ONBOARDING §4): a wrong merge is silent and corrupts scope, a visible pair is a worklist item |
| **U11** | Which authorised third-party platforms (application portals, fee calculators) does the client already know of? Each needs an `authorization_reference` before its host can be trusted at all | None pre-registered. They surface during source mapping and require an explicit `authorize_external_domain` decision |
| **N6** | Should a target dropped from a later list remain visible in the onboarding worklist? | Hidden by default (`only_current=True`), never deleted. Its sources, evidence and matched university are untouched and it reappears if a later list names it again |

### Decisions taken at Step 4 hardening

These are no longer open. Recorded here because the reasoning matters later.

| # | Decision | Consequence now |
|---|---|---|
| **U8 — pilot wave** | **The client selects the pilot institutions manually and will supply a workbook naming them.** All 181 imported institutions are kept. The pilot is **not** inferred — not by rank, not by destination, not by any other derivable ordering | **Resolved in Step 5A.** The client supplied `university_official_sources.xlsx`, and exactly the 35 institutions it names now carry `pilot_wave = 1`. The other 146 keep `pilot_wave` NULL and remain valid future targets; nothing was deleted or demoted. Note that the selection spans six destinations (US 13, UK 7, AU 6, HK 4, CA 3, SG 2), not the UK/HK/Macau the earlier framing assumed |
| **U9 — QS licensing** | **QS rank and score stay associated only with target-list history.** No canonical `ranking_entry` row is created from any supplied workbook. Ranking publication requires an explicit authorisation/licensing confirmation, later | `RANKINGS_ENABLED` remains `false`. `qs_rank`/`qs_score` live on `target_list_entry` only. A `ranking_entry` insert now additionally requires an `AUTHORIZED_RANKING` source with a live, display-allowed `source_authorization` — so the gate is a database rule, not only a feature flag |

## Step 4 pilot-readiness notes

Design: [`docs/PILOT_COLLECTION.md`](PILOT_COLLECTION.md).

| Item | Status |
|---|---|
| Export, validator, discipline seeds | Implemented: revision 0018, and extended by revisions 0019-0021 (588 tests passing). All 57 GB/HK/MO candidates export with `selected` blank; `--pilot-target` is validation-only and a named test asserts it never reduces or pre-fills the export |
| `program_ref` / `source_ref` | Workbook-local collection identifiers, `P0001` / `S0001`. Duplicate, unknown, malformed and wrong-institution references are all errors. No name-text or URL matching anywhere |
| Applicant scope | Only the literal `UNIVERSAL` resolves; everything else, blank included, parks as `SCOPE_MAPPING_REQUIRED`. A warning rather than an error, so the row is held and not the file |
| Disciplines | Three top-level codes only. `COMPUTER_AND_DATA` merges the PRD's Computer Science and Data because institutions disagree where the boundary sits; `discipline_hint` keeps the real subject verbatim |
| `selected` parsing | Originally treated any value but `YES` as "not selected", which would have dropped an institution silently for a collector writing `Y`. Now `SELECTED_VALUE_UNRECOGNISED`, an error |
| `Official_Sources.degree_level` | The first draft's help text inverted `SourceDegreeScope`: absence means NOT covered, never "covers everyone". A diligently filled workbook would have reported zero coverage across all 36 |
| openpyxl `cell(..., value=None)` | Does **not** clear a cell — it means "no value argument". Use attribute assignment. This made one test pass vacuously before it was caught |

### Decisions taken at pilot staging (U12, U14, U15)

Design: [`PILOT_COLLECTION.md` §8–10](PILOT_COLLECTION.md#8-the-staging-plane-u12).
Architecture: D25–D29, C28.

| # | Decision | Consequence now |
|---|---|---|
| **U12 — where collected facts land** | **Their own plane.** `pilot_submission`, `pilot_selected_university`, `pilot_collected_program`, `pilot_collected_source`, `pilot_collected_fact`. The rejected alternative — registering each URL as a `MANUAL` source with a snapshot over the collector's pasted wording — would have made a person's retyping load-bearing evidence, and the evidence chain means "a machine read this stored page" or it means nothing | Workbook staging is **not** evidence and **not** publication eligible: no FK path reaches it from `field_provenance` (asserted by a graph-walking test), nothing there can become a `field_claim` without real acquisition, and `app_publisher` holds no privilege on it. The collected rows are append-only; a correction is a new submission (`file_sha256` UNIQUE), with the previous one `SUPERSEDED` and untouched. `program_ref`/`source_ref` are submission-local by composite key, not by convention |
| **U13 — does the import create `university` rows** | **No**, as previously positioned. Confirmed by construction rather than by intent | The import writes no `university`, `program`, `source` or `source_mapping` row; a test asserts thirteen such tables are still empty afterwards. The collector's official name sits on `pilot_selected_university` until identity resolution |
| **U14 — fee ranges** | **The shape of the amount is data.** `amount_kind` ∈ {`EXACT`, `RANGE`, `FROM`, `UP_TO`, `VARIABLE`} with `amount_min`/`amount_max` and a CHECK per kind. `tuition.amount` is dropped, not deprecated — the table held zero rows, and a nullable legacy column would have been filled in eventually | A `RANGE` keeps both ends and **no midpoint is ever derived**; averaging is a query-layer presentation choice that must show its working. `amount_kind` is not a status: `OFFICIALLY_NOT_PUBLISHED` still means the page says nothing, `VARIABLE` means it addresses fees without a figure and `official_text` becomes mandatory. The governed-column trigger moved to the three new columns (C28) |
| **U15 — the ~200 source verifications** | **A queue, worked by a person, with the decision recorded.** `pilot_collected_source.verification_state` starts `PENDING`; `verify` / `reject` / `needs_review` each require an actor and a reason and append to the audit chain | No URL is auto-classified. `VERIFIED` means "worth registering", never "official" — registration creates a `CANDIDATE` `source_mapping` and no `source`, so C27's `source_eligibility_is_earned` still has to be satisfied separately. A matching verified host is surfaced as **evidence for the reviewer** and never acts on its own; there is no bulk verify |

### Decisions taken at Step 5A (the final source list)

Design: [`PILOT_COLLECTION.md` §11](PILOT_COLLECTION.md#11-the-final-source-list-step-5a).
Architecture: D30–D32, C29.

| # | Decision | Consequence now |
|---|---|---|
| **Pilot size** | **35 institutions, fixed by the client.** The PRD asked for ≥36; the supplied file names 35 and the client confirmed it as final | `PILOT_INSTITUTION_COUNT = 35` is the one constant the importer, the CLI and the tests read. A file of 34 or 36 rows is refused with a message saying it is a question for the client, never a row to add or drop. The PRD's "≥36 institutions" and ADR-0002's "36 institutions" are left exactly as written, and a test fails if either is edited |
| **Pilot destinations** | **Six, not three.** The selection is US 13, UK 7, AU 6, HK 4, CA 3, SG 2. The earlier "pilot = GB/HK/MO, 57 candidates" framing was an inference from the PRD, not a client statement | Nothing in the schema assumed three destinations, so no migration was needed. The export template's `--destination` default is a convenience flag and stays a flag. `docs/PILOT_COLLECTION.md` §1 no longer describes the pilot as UK/HK/Macau |
| **URL vs responsibility** | **Separate rows.** One page answers for several categories; 66 of 385 URL cells in the real file repeat a URL given under another heading | Every cell is a claim; `duplicate_of_source_ref` marks repeats; a partial unique index gives one physical page per URL per institution. 385 claims over 319 pages. Acquisition will fetch 319 times; review works 385 claims, because a page being on the right domain and being the right authority are different questions |
| **Unclassified sources** | **A page with no stated category stays `UNCLASSIFIED`.** Three of the additional-source URLs are genuinely new and the workbook says nothing about them | `UNCLASSIFIED` is not a `SourceCategory` member, so nothing canonical can carry it. A CHECK permits rejection but refuses verification; `classify_candidate` is a recorded decision with an actor and a reason, audited like every other |
| **README arithmetic** | **Documentation metadata, reported and moved past.** The file claims "455 populated / 455 expected"; it counted columns A:M rather than the eleven URL columns | The correct figure is 385 = 350 core + 35 optional, recorded in the import's validation summary. It is not a database invariant: the additional column is optional, so 385 populated cells is not something to enforce |

### Decisions taken at Step 5B (safe acquisition)

Design: [`ACQUISITION.md`](ACQUISITION.md). Architecture: D33–D37, C30–C31.

| # | Decision | Consequence now |
|---|---|---|
| **Trust boundary** | **Fetchable ≠ publication eligible.** Step 5A's coupling was a real design error, not a simplification: it made the source review pass depend on evidence the review pass was needed to unlock | `source.fetch_eligibility` is computed by machine; `publication_eligibility` is untouched C27. All 319 pilot sources are FETCHABLE + NOT_ELIGIBLE. Lineage runs through `pilot_collected_source.acquisition_source_id`, not `source_mapping`, because a mapping needs a verified host |
| **Lease fencing** | **A token per claim**, checked on every heartbeat and finalisation, taken as the transaction's first write | A stalled worker that lost its lease writes nothing at all. Deferred from Step 3.5 and now closed; concurrency tests cover the sweeper/finaliser race with two real connections |
| **SSRF** | **Fetch-time validation is the defence; storage validation is a floor.** Every A/AAAA answer checked, connection pinned to a vetted address, every redirect hop revalidated | `is_global` is the gate (C30). Numeric host spellings, IPv4-mapped/6to4/Teredo v6, and five cloud metadata endpoints are refused. The DNS rebinding residual is stated in ACQUISITION §5 rather than glossed |
| **Storage ordering** | **Object first, then the transaction.** An orphan is recoverable; a dangling reference is not | 24-hour orphan age floor, no automatic GC, `find_missing_objects` for the direction that matters |
| **Politeness** | **One request in flight per host**, configurable floor, `Retry-After` obeyed | No proxy rotation, no user-agent cycling, no CAPTCHA handling — and their absence is the design, not an omission |
| **Schedule** | **Not the PRD's 06:00/12:00/18:00 yet.** `beat_schedule` is empty | Cycles are enqueued by hand for the pilot. Turning on a three-a-day sweep of 319 pages across 120 universities before anyone has reviewed a result is a load test the universities pay for |
| **Browser fetching** | **Interface only.** No page has been shown to need rendering | Adding Playwright costs ~400 MB and a second failure mode; the evidence to justify it does not exist yet |

### Decisions taken at Step 5C.1 (document normalisation)

Design: [`EXTRACTION.md`](EXTRACTION.md). Architecture: D43–D45, C41–C43.
Migration `c8d9e0f1a2b3`.

| # | Decision | Consequence now |
|---|---|---|
| **No new table** | **The existing `extraction` table carries document-level results** | It already had the lineage Step 5C.2 needs (`field_claim → extraction → snapshot → source`) plus append-only immutability and versioning. Five columns added, nothing re-modelled |
| **Payload in the object store** | **11 MB of derived documents does not belong in PostgreSQL** | It is reproducible from bytes we already store, so keeping it in the database would put derived data in every backup and replica. `output` keeps a summary; `derived/` and `evidence/` are separate key prefixes so extraction structurally cannot overwrite raw evidence |
| **Artifact carries no identifiers** | **The hash is a function of (bytes, version) and nothing else** | Determinism becomes checkable, and identical bytes dedupe to one artifact (175 extractions → 174 artifacts). Lineage lives on the row, not in the payload |
| **Idempotency without a run record** | **`UNIQUE (snapshot, extractor, version)`; a repeat pass is a no-op** | An extraction *is* the result, so a second row could only be a duplicate. The cost: a `FAILED` caused by something environmental needs a version bump rather than a retry, because the table is append-only |
| **Navigation kept, not deleted** | **`nav`/`header`/`footer`/`aside` are preserved and labelled** | Universities put fee tables in asides and deadlines in footers. Each block records its container so Step 5C.2 can prefer the main column; the judgement belongs to the step that knows what it is looking for |
| **Charset by fixed ladder** | **HTTP charset → document declaration → UTF-8 → cp1252** | A detection library guesses, and differently between versions, which would make the artifact hash depend on a dependency's heuristics. Real fleet: 172 UTF-8, 1 ISO-8859-15, 1 fallback, **0 undecodable characters** |
| **pypdf, not PyMuPDF** | **The instruction suggested PyMuPDF; it is AGPL-3.0** | A licence decision on a commercial internal product belongs to the client, not to a silent import, and the fleet contains exactly one registered PDF. pypdf (BSD-3) read its text layer, 2 pages, 4,660 characters, no OCR needed. Swapping later is a one-module change |
| **JSON-LD is not authoritative** | **Present is not true** | It is a claim the page makes in a convenient format. Parsed and inventoried; nothing becomes a `field_claim` in this step |
| **Embedded JSON only where safely identifiable** | **`application/json` blocks and the exact ids `__NEXT_DATA__`/`__NUXT_DATA__`** | A heuristic over arbitrary script bodies is how a parser turns into a JavaScript interpreter. Everything else is inventoried by type and left alone |
| **Still no browser** | **5 of 175 pages plausibly need rendering, and Playwright is not built** | Down from an implied "4 of 5 have recoverable embedded JSON": the three Chicago pages' JSON-LD is metadata only, and McGill — previously flagged — yields 3,320 characters to a real parser (C43) |

### Decisions taken at Step 5B.3 (the first full pilot cycle)

Design: [`ACQUISITION.md` §15](ACQUISITION.md#15-the-first-full-pilot-cycle-step-5b3).
Architecture: C40. No migration.

| # | Decision | Consequence now |
|---|---|---|
| **A dead URL stays fetchable** | **A 404 stops retries within a cycle and says nothing about the next one** | Correct per the current rules, and impolite in aggregate: a scheduled run would re-request all 61 known-dead URLs. Parking a confirmed-dead page in `NEEDS_MANUAL_REVIEW` after N consecutive 404s is the obvious fix and is **left open** — it changes what the scheduler does to a university's server, which is not a side effect to take on while counting |
| **HTTP 202 is an error** | **Only `200` is treated as carrying a body** | Two hosts answer `202 Accepted` with what is probably content. Whether that is evidence is a question about what counts as an observation, so it is reported rather than quietly widened |
| **TLS is never relaxed** | **7 pages failed certificate verification and stay failed** | "Unable to get local issuer certificate" on 3 hosts. Characteristic of a server omitting its intermediate — browsers hide this by caching intermediates — but **unconfirmed from here**, and a gap in our own trust store would look identical. A reviewer checks; if it is their chain, it goes to the institution. No `verify=False`, ever |
| **Linked PDFs are not fetched** | **29 pages link 174 PDFs between them; none were downloaded** | A linked PDF is not a registered acquisition target. Following links is crawling, not fetching a named source, and the source list is the client's decision |
| **No browser, on 5 pages of evidence** | **`POSSIBLE_BROWSER_REQUIRED` is a label, not a trigger** | 5 of 169 stored pages carry almost no visible text, and 4 of those embed JSON in the initial response — so reading that JSON is likely cheaper and more stable than rendering. Playwright stays unbuilt |
| **Sampling** | **A set chosen for technical variety does not give you the structural distribution** | C40: the 12-page smoke set found no JSON-LD and suggested a structured-data parser was pointless; 50 of 169 pages carry it. Step 5C gets a structured-data path as an accelerator |

### Decisions taken at Step 5B.2 (recovery and cycle isolation)

Design: [`ACQUISITION.md` §14](ACQUISITION.md#14-recovery-and-cycle-isolation-step-5b2).
Architecture: D39–D42, C38. Migration `b7c8d9e0f1a2`.

| # | Decision | Consequence now |
|---|---|---|
| **Cooldown is a timestamp, not an eligibility state** | **"May we fetch this at all?" and "may we fetch it now?" are different questions.** One column was answering both, so a single `429` blocked a page permanently and nothing could undo it | `source.cooldown_until` is operational timing, set and cleared automatically, carrying no judgement. `fetch_eligibility` is a durable judgement. Making cooldown an eligibility member would force every reader of that column to know one of its values expires. `source_health` answers both as `health` and `schedule_state` |
| **429 escalation threshold** | **Four consecutive 429s, then `NEEDS_MANUAL_REVIEW`** — never `BLOCKED` | One throttle is ordinary traffic shaping; four in a row is a question about our cadence, which a person should answer. Strikes count a *streak* and any success resets them: a lifetime counter would eventually escalate every source in the pilot |
| **429 fallback cooldown** | **900 seconds when `Retry-After` is absent or unparseable** | Deliberately generous. The site said "too many" and gave no number, so guessing small guesses in the direction that gets us blocked. `Retry-After` always wins when present, in either legal form |
| **Host-level cooldown** | **A `429` quiets the whole host**, via a `host_cooldown` table | One pilot host serves eleven pages; honouring a throttle on only the page that received it is not honouring it. A table rather than in-process state because `HostGate` is empty in the next worker process. The smallest safe thing: one timestamp and a reason, no host scheduler |
| **Unknown DNS error codes** | **Treated as temporary** | Wrong in that direction costs one retry. Wrong the other way parks a working page in a review queue waiting for a human with no reason to look. Both platforms' code families are pinned numerically, since Windows lacks `EAI_AGAIN` and glibc lacks `WSATRY_AGAIN` |
| **`cycle_key` is required, not defaulted** | **`claim_next` takes it as a mandatory keyword and filters on it** | It was a label the query ignored, so `run_cycle(K)` drained every due attempt and reported them as K's. Required rather than defaulted so no future call site can quietly drain the wrong cycle. Claiming never rewrites `cycle_key`, so a retry stays in its own cycle |
| **Exception containment boundary** | **`except Exception` at the per-attempt boundary; never `BaseException`** | One broken page no longer ends a cycle, and Ctrl-C still stops a 319-page run. An unkillable worker is a worse failure than the one being fixed |
| **`INTERNAL_ERROR` is not retried** | **Our defect is recorded, not re-attempted** | The cost of a bug in this repository should not be paid in requests to someone else's server. It is also not `HTTP_ERROR` or `TIMEOUT`: a health model that cannot tell "the site is broken" from "we are broken" sends an operator to email a university about our bug |
| **Unrecordable failures are not invented** | **If the lease is gone or the database broke, no `fetch_run` is written** | The attempt stays `RUNNING` and the existing sweeper closes it as `ABANDONED` — the history that describes what actually happened, rather than a terminal record we could not write |
| **Re-enable is technical, not trust** | **`publication_eligibility` is never written by recovery** | C27 is untouched; a re-enabled source is exactly as publication-ineligible as before. The `source-status` output prints that line every time, because "re-enabled" invites the other reading. Re-enabling bypasses no safety check — the next fetch resolves, validates and revalidates as always |
| **What is audited** | **The four manual transitions, and not automatic cooldowns** | Each override needs an actor and a reason of at least eight characters and appends to the hash chain. A cooldown expiring is the clock, not a decision; auditing thousands of them would bury the handful that are decisions |
| **Effective concurrency** | **1, whatever `global_concurrency` says** | `run_cycle` awaits each page before claiming the next, so the semaphore never binds. Left as it is: safe rather than fast, and it sets a ~27-minute floor for a 319-page cycle, which is the number to know before watching one. Changing it would increase load on universities and is not a side effect to take on while fixing counters |

### Decisions taken at Step 5B.1 (the first real-web run)

Design: [`ACQUISITION.md` §13](ACQUISITION.md#13-what-the-first-real-web-run-found-step-5b1).
Architecture: D38, C32–C34.

| # | Decision | Consequence now |
|---|---|---|
| **First contact** | **Twelve deliberate requests, checked in, before 319.** The stack had only ever met a fixture server, and a fixture agrees with whatever the test assumed | `apps/api/smoke/acquisition_smoke_set.toml` is reproducible input, not business configuration: nothing reads it at runtime and the sources it names are as publication-ineligible as the other 307. Twelve pages found three defects in our own code |
| **A 200 is not evidence** | **A response can carry a WAF challenge and a success status at once.** NUS returned 212 bytes of Imperva interstitial, which we hashed, stored and called `HEALTHY` | Challenge bodies are detected (HTML, <8 KiB, known marker), recorded `BLOCKED` with `error_class = ChallengeInterstitial`, and **discarded**. The size bound is what keeps it honest — a real page mentioning Cloudflare is not a challenge, asserted by test. Detection is not circumvention: a blocked source stops being scheduled and waits for a person |
| **Permanent vs transient** | **404 and 410 end the attempt.** PolyU's fee URL is dead and was retried four times | A permanent status is a finding for source review — the workbook URL is wrong — not a condition that improves by asking again. 429, 5xx and timeouts still back off and retry |
| **The redirect trail belongs to the attempt** | **`fetch_run` carries `effective_url` and `redirect_chain` too**, not only `snapshot` | A fetch that redirects four times and then times out now records where it was going. Without it, "the site moved" and "the site is down" are the same row |
| **Reporting** | **A count in a report is a query, not a recollection.** The first Step 5B.1 write-up stated two counts the database contradicted, because it was assembled from scrollback of an earlier cycle after the development data had been purged | `acquisition_smoke.py audit` prints every count from SQL with each grain named (sources / snapshots / distinct blobs / observed-but-discarded), and four regression tests assert the counts the prose claimed. Demonstration data still has to be purged — several Step 3/4 tests assert a globally clean schema — so the defence is that the numbers are regenerable, not that they are retained. See C35 |
| **"Off host" means the host changed** | **Not "the URL changed".** The summary and the `--moved` filter both used `effective_url IS NOT NULL`, which a trailing slash or an http→https upgrade satisfies | Both share one SQL host expression and count `DISTINCT` pages; the field is `pages_redirecting_off_host`, named for its grain. A same-host redirect is recorded and is not a review finding. See C36 |
| **Verified backend** | **Filesystem only.** The real run used `FilesystemEvidenceStore` | REAL HTTP acquisition tested · FILESYSTEM evidence store tested · **S3/MinIO still not tested**. Docker remains the open gate |
| **Browser fetcher, revisited with evidence** | **Still not justified.** The page most likely to be a JS shell — HKUST's flipbook — returned 15.2k characters of static text; all 7 stored HTML pages carry 4.0k–15.2k characters of it and not one carries a single JSON-LD block | Step 5C needs a static HTML parser and a PDF text parser. It does not need Playwright, and it has nothing to gain from a structured-data parser yet |
| **A candidate claim is not a fact** | **Nothing here is verified, canonical or publishable.** `field_claim_candidate` asserts only that one extractor found one candidate in one exact region of one document | 1,941 current candidate claims exist and 0 `field_claim` rows do, because C27 refuses every one of them. Step 5C.3 must earn the eligibility before promoting anything |
| **Applicant scope of a requirement** | **Unresolved, not universal.** 1,066 of 985 admission-requirement claims carry `SCOPE_MAPPING_REQUIRED` across all rule versions — the page stated no country and no qualification system | The client decides what scope a requirement with no country wording has. Defaulting it to `UNIVERSAL` would show a rule written for A-level applicants to a Chinese applicant as their requirement |
| **Confidence** | **Three bands with a stated reason. No decimal.** There is no calibration set behind these rules, so a number would be decoration over a judgement | A labelled calibration set exists. Until then the band plus its reason is the honest form, and a HIGH band is still not verified |
| **Extractor precision, measured not assumed** | **Audited on real pages, and it found six defects.** Two of them produced no visible failure at all: `ON CONFLICT DO NOTHING` dropped 34 claims and the pass reported success | Residual imprecision is reported rather than filtered away — 841 LOW admission-requirement rows are a reviewing workload, not a bug. See [CLAIMS §9](CLAIMS.md#9-what-the-audit-found) |
| **Cost-of-attendance line items** | **Recorded as `TUITION` candidates.** Housing, food and fee-adjacent figures under a fee heading are located and quoted correctly; deciding which line is *tuition* is a reading | Step 5C.3, where a reviewer with the evidence in front of them can say. Section 26 says not to make that call here |
| **A review decision is not permission** | **`ACCEPTED` means the extraction is right about what the page says.** Whether that page may support a published fact is the source's `publication_eligibility`, which no reviewer of candidates can change | `claim_promotion_ready` is 0 of 1,936, and the one accepted candidate's only remaining blocker is `SOURCE_NOT_ELIGIBLE`. C27 refuses the `field_claim` insert, demonstrated rather than asserted |
| **Corroboration** | **There is none to lean on.** 0 `AGREES` groups, 0 confirmed conflicts, 74% of candidates not safely comparable | Step 5C.4 must not assume a voting or merge mechanism helps. The cross-source repeats that exist are one site's overview page echoing its own detail page |
| **Programme candidates** | **180 of 226 groups cannot be confirmed as body content**, because `Link` records no container and the chrome guard reads `block.container` | A Step 5C.1 document-schema change, which alters every document hash and supersedes every candidate. Until then they are quarantined out of program grouping, not silently trusted |
| **Applicant scope** | **894 unresolved, 0 resolvable to an id.** `applicant_scope` contains exactly one row, `UNIVERSAL`, which section 12 forbids inferring from silence | The client decides which scopes matter. Every proposal is a hint with a null id until that taxonomy is seeded |
| **The privilege suite** | **A hard gate before any promotion or publication work.** 15 tests skip here because the runtime-role passwords are not configured in this environment | Before Step 5C.4 touches `field_claim`. The separation between the role that reviews and the role that publishes is exactly what those tests cover, and they must be run rather than weakened |

### Still open for the pilot import

| # | Question | Position until answered |
|---|---|---|
| **N7** | Two entry routes for one programme collide on `uq_admission_requirement_scope_kind` | Put alternatives in one row's `official_text`. A `route_label` column plus key change is the alternative. Needed before the first reconciliation, not before collection |
| **N8** | `campus_name` and `faculty_or_school` are free text, so only a name join could resolve them — the failure `program_ref` exists to prevent, one level down | Staged verbatim on `pilot_collected_program` and joined on by nothing. Give them their own refs only if the pilot shows it matters |
| **N9** | A second workbook re-creates every programme, because `program` has no natural key | `pilot_collected_program.reconciled_program_id` records the link when a human makes it, and the importer never sets it. Pre-filling a grey `program_id` column on re-export is still to do, and is only needed once real `program` rows exist |

## Bootstrap-stage assumptions (Step 2)

| Assumption | Rationale | Revisit when |
|---|---|---|
| Python pinned to 3.12 (`<3.13`) | asyncpg and the wider async stack have the most mature wheels here; the spec requires 3.12+ | A dependency needs 3.13 |
| Node pinned to 22.23.2, floor `>=22.13.0` | `.node-version`/`.nvmrc` + `engines` + `engine-strict=true`, one version for local and CI. The floor is not arbitrary: a transitive dependency (`eslint-visitor-keys`) declares `^22.13.0`, which `engine-strict` turns into a hard requirement | Node 24 becomes LTS, or a dependency raises the floor again |
| No UTC representation without a declared timezone | **C13.** An exact `instant_utc` exists only with date + time + zone. Imprecise facts sort by calendar components and filter via a calendar-local `daterange`; no UTC interval is ever derived or labelled as such | — |
| `U3` fixed: "600 programs" means 600 `program` rows, offerings reported separately | Client decision | — |
| Postgres search via `pg_trgm` + `tsvector`, no search engine | ~600 programs; the PRD says the same | >50k programs, or facet latency >300 ms p95 |
| One Python image serves api/worker/beat | Architecture D11; the roles share models and differ only by command | The browser worker, which needs its own image |
| No `tests/e2e/` directory yet | There is no UI flow to exercise, and the Playwright worker is explicitly out of scope for this step | Step 3, alongside the first real page |
| Readiness treats Postgres and Redis as required, object storage as opt-in | MinIO is not needed until snapshots exist | Acquisition module lands |
| No deployment manifests (Kubernetes, Terraform) | Explicitly out of scope; premature | A deployment target is chosen (see A16) |
| Test-only runtime role credentials are generated and restored, not stored | **D57.** The real passwords do not exist in any recoverable form on a developer machine, and a skipped privilege test is not a passing one. Loopback only, generated per run, original verifier restored byte-for-byte | A secret store exists and CI-style `APP_*_PASSWORD` variables are always set |
| "Current candidate" means current rule **and** current document artifact | **D55.** Measured: four of the six rules produce byte-identical output across the parser change, so bumping their versions to express it would mark 472 correct claims superseded | A rule and its document version are ever genuinely the same question |
| The applicant-scope taxonomy is not yet decided | **Section 21.** The observed vocabulary conflates qualification systems (A-level, IB, AP) with applicant jurisdictions (US, HK, SG, MY, CN), and the client's decision is which axes exist | The taxonomy is chosen from the returned vocabulary |
| A registrable domain is computed from an explicit suffix list, not a public-suffix library | Used only to *describe* a redirect, never to decide one. A guess by a general library is worse than a short list covering the suffixes this fleet actually contains | A fleet outside the current 11 suffixes, or trust that ever depends on the answer |
| Verification runs in MODE B until a real reviewer identity exists | **D58.** The machinery, the constraints and the audit chain are all in place; what is missing is a person who has looked at the hosts. Proposals are persisted and nothing is applied | An authenticated reviewer exists, or the client names one |
| Promotion readiness of 0 is the correct answer, not a shortfall | No domain is verified, so no source is eligible, so nothing is promotable — and C27 enforces that rather than merely expecting it | Domains are verified in MODE A |
| `promoted_source_id` has no writer, so no source can be made eligible yet | **C63.** The rule is complete and enforced; the function that performs the last arrow does not exist. The first blocker for Step 5C.6 | A promotion function is written and tested |
| Publication authority is a separate table from extraction routing | **D64.** Different questions; tied by a subset invariant rather than a shared definition, so widening one cannot silently widen the other | Ever genuinely the same question |
| Promotion is explicit, never automatic on verification | **D65.** Verification says a page is what it claims; promotion says we now rely on it. Separating them makes the trust transition observable, testable and separately revocable | The architecture requires automatic promotion |
| `source:verify` is a permission on the existing reviewer role | **D66.** No second role, and not `source:manage`: registering a URL must not confer the power to declare it official | — |
| CLI review authentication is local-password, operator-only | **D68.** `AppUser.password_hash` is the documented Argon2id fallback; OIDC is preferred and unconfigured. No token is issued and `user_session` is untouched | An identity provider is configured |
| `proposal:publish` is held by no role | **D69.** Review and publication are separate authorities and no application publisher role exists. Free today: publication is unbuilt | The publication transaction is built and an owner is chosen |
| Dejan's reviewer grant is an unaudited bootstrap row | **D70.** No authenticated administrator existed to attribute it to, and inventing one would be a fabricated approval. Every subsequent grant is audited | — |
| Initial credential enrolment is unauthenticated, once per account | **C70, superseded in part by C71.** Somebody with no password has nothing to authenticate with, so the proof is possession of a challenge issued for that account. `WHERE password_hash IS NULL` still makes it happen at most once | OIDC replaces the local credential path |
| Administrative password reset exists and is currently unusable | No identity holds `admin:roles`, and creating one to make the path work would be inventing the authority it checks | An administrator is provisioned and enrols |
| Claiming an account requires a one-time challenge, not just an email | **C71.** An email address is a routing label, not a secret. Provisioning says the account exists; the challenge says you are the person it was provisioned for | An invite-token or OIDC enrolment path replaces it |
| Issuing a challenge needs the owner connection; claiming needs only `app_api` | That asymmetry is the fix. `app_api` holds no `INSERT` on `credential_enrollment` and no `UPDATE` on `app_user`, so it can spend a token and never mint one | The runtime grant matrix changes |
| `POSTGRES_API_PASSWORD` is configured wherever a reviewer enrols | `reviewer-enrol-password` connects as `app_api` and refuses rather than falling back to the owner. **Settled for this machine in 5C.7E:** a fresh 256-bit credential was provisioned for `app_api` alone and stored in the gitignored `.env` | The credential is rotated again, or a new machine is set up |
| `app_worker` and `app_publisher` passwords remain unrecoverable | Not rotated in 5C.7E on purpose: only `app_api` was needed, and rotating a credential nobody asked about is an outage nobody asked about. `pg_authid` keeps only SCRAM verifiers, so the old values cannot be recovered — a future need means rotating them then | Either role must run on the host |
| Redis always starts empty locally | No RDB or AOF file exists. It carries Celery queues and cache entries, both rebuildable; no plane's state lives there | Redis is given a persistence configuration |
| `datahub_gates` is an orphan database | Left at revision `f1a2b3c4d5e6` by Step 5C.3. Nothing in the repository references it, so 5C.7E neither migrated nor dropped it | Somebody establishes what it was for |
| `datahub.audit_log` contains exactly 10 rows, nine of them from an incident | **C72.** One `TARGET_LIST_IMPORTED`, plus nine `CREDENTIAL_ENROLLMENT_ISSUED` from the 5C.7E test-against-real-database incident. Append-only and correctly un-deletable; kept as truthful evidence rather than erased to tidy a count. See [SOURCE_VERIFICATION §20](SOURCE_VERIFICATION.md) | A legitimate operation appends more |
| Database identity, not configuration, decides whether tests may write | **C72.** The 5C.7E incident had a *correct* `POSTGRES_DB`; the intent was wrong. `app.db.safety` queries `SELECT current_database()` and consults no environment variable at all | — |
| `app_claim_enrollment` is owned by a role that cannot log in | **C73.** It is SECURITY DEFINER, so the owner is the blast radius. `app_credential_definer` holds column-level `UPDATE` on four columns across two tables and nothing else | The function's body needs a privilege it does not currently have |
| The definer role is never dropped by a downgrade | Roles are cluster-wide and this cluster holds three databases using the function. Downgrading one would break the others, so `downgrade()` returns ownership and revokes privileges, leaving an inert role | The cluster holds only one database |
