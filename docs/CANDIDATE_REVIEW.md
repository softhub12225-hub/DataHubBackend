Written for: whoever runs the first candidate review pass, and whoever builds Step
5C.4's promotion into `field_claim`.

# Candidate quality, grouping and review preparation

`field_claim_candidate` → quality evaluation → agreement grouping → conflict
identification → scope reconciliation → a human review queue.

**Not** candidate → canonical fact. No `field_claim`, no `change_proposal`, no
`field_provenance`, no canonical row, no LLM, no browser, no network request.

---

## 1. What the candidate plane already guarantees

Confirmed against the schema rather than assumed:

| Property | How it is enforced |
|---|---|
| append-only | `field_claim_candidate_forbid_mutation` trigger; an `UPDATE` raises *"this table is append-only history"* |
| publication-ineligible | every pilot source is `NOT_ELIGIBLE`, and C27 refuses a `field_claim` from one |
| evidence-backed | `evidence_text` and `value_raw_text` NOT NULL, both refused blank by CHECK |
| versioned by rule | `extractor_name` + `extractor_version`, with the fingerprint built over both |
| traceable to a document location | `locator` NOT NULL, `<> '{}'`, and it **resolves** — see §3 |

No second candidate table was added. Review decisions are a separate log, and every
grouping, quality class and scope proposal in this step is computed on read.

---

## 2. Why the existing review and conflict structures could not be reused

Section 3 asks for reuse where it fits. Each candidate was tried against the database,
because *"it does not fit"* is a claim that should cost something to make.

| Structure | What the database said |
|---|---|
| `review_task` | **Refused.** `proposal_id` is NOT NULL and FK to `change_proposal`, which this step forbids creating. A candidate id fails the FK; NULL fails the not-null |
| `review_decision` | **Refused.** `item_id` is FK to `change_proposal_item`. And `review_decision_kind` is `APPROVE \| RETURN \| CORRECT` — what you do to a proposed *change* — with no member for `REJECTED` or `NEEDS_SCOPE_MAPPING` |
| `claim_resolution`, `resolution_candidate` | **Refused twice.** Both FK to `field_claim`, which is empty; and C27 refuses to create one from a `NOT_ELIGIBLE` source |
| `field_conflict`, `conflict_resolution` | **Not refused.** No FK on `entity_id` or `competing_claim_ids`, so a row naming twelve candidate ids inserts happily |

That last row matters. *"The database refuses it"* is a much easier claim to rely on
than *"the database would accept a row that means nothing"*, and only the second is true
of `field_conflict`. It is keyed on `(entity_type, entity_id, field_path)` — the
**canonical entity** a conflict is about — and every canonical table is empty, so the
only `entity_id` available is a fiction. `change_proposal` is the same shape: its
subject and root columns carry no foreign key either, so only the instruction stops that
one, not the schema.

All five verdicts are asserted by
[`test_candidate_review_plane.py`](../apps/api/tests/integration/test_candidate_review_plane.py),
including the one that is not a refusal.

---

## 3. The review model

One table, `field_claim_candidate_review`: an append-only log of human decisions with
`candidate_id`, `actor_id`, `decision`, `reason_code`, `reason_text`, `decided_at`,
`recorded_at`. Several rows per candidate are expected and correct — a reviewer may
revisit a decision, and the earlier one is history rather than an error.

One view, `candidate_review_state`, projecting the latest decision.

### The suggested state set is split, deliberately

The instruction suggested `UNREVIEWED | ACCEPTED | REJECTED | NEEDS_CONTEXT |
NEEDS_SCOPE_MAPPING | SOURCE_NOT_VERIFIED | SUPERSEDED`. **Three of those are not
decisions.** Whether a candidate is superseded, whether its source has been verified and
whether its scope resolved are things a machine knows at any moment.

Collapsing them into the human's decision would make one column answer two questions —
the mistake D39 corrected for `fetch_eligibility` — and it would mask a reviewer's
judgement behind a fact about the source, which is what §30 forbids. A reviewer who
accepts a candidate from an unverified source has made a real judgement about the
extraction, and it has to survive the source being unverified.

So the stored decision is `ACCEPTED | REJECTED | NEEDS_CONTEXT | NEEDS_SCOPE_MAPPING`
(plus `UNREVIEWED`, the absence of a decision), and the view exposes `is_superseded`,
`source_not_verified` and `scope_unresolved` beside it as separate columns. Nothing is
lost and nothing is conflated.

`reason_code` reuses `review_decision.reason_code`'s six values and adds eight from what
the Step 5C.2 audit actually found — `SITE_CHROME`, `MARKETING_PROSE`,
`NOT_A_FACT_OF_THIS_KIND` and so on. Two spellings of one reason cannot be reported
together, so the existing vocabulary is extended rather than replaced.

---

## 4. Confidence is not review state

§4. `HIGH` describes how unambiguous the *extraction* was. A labelled table cell can
hold a number the university has since changed, and a `LOW` sentence can be exactly
right. Nothing maps a band to a decision:
`test_no_band_implies_a_decision` asserts an unreviewed `HIGH` candidate is still
blocked, and `test_a_high_band_is_not_promotable_and_a_low_one_is_not_rejected` asserts
an accepted `LOW` one is not.

The band does affect **queue order** (§27–28), which is a statement about where to spend
a reviewer's attention, not about truth.

---

## 5. What "current" means, and why one frozen list was not enough

6,129 superseded candidate rows sit beside 1,936 current ones — more than three to one,
because a corrected rule leaves its earlier output in place as history (D48). A review
total that silently included them would be wrong by a factor of four and would look
entirely plausible.

The first attempt froze the current `(extractor, version)` pairs into
`candidate_review_state`. **Four rule corrections later the view reported every current
candidate as superseded**, silently, with a total that happened to look right. A frozen
copy of something that changes is wrong by construction.

So the pairs live in `claim_rule_version`, one row per extractor, which the claim runner
rewrites on every pass from its own registry. `test_the_view_and_the_registry_agree_on_current_versions`
asserts the two match, and `test_a_superseded_candidate_is_excluded_from_active_review`
shows the same row flipping from current to superseded when the version moves.

---

## 6. Grouping: two keys, not one

Grouping answers two different questions, and one key cannot answer both:

* a **context key** — *what is this claim about?* institution, field, applicant scope,
  programme, period, and per-field additions (the test for a language claim, the round
  for a deadline);
* a **value key** — *what does it say?*

Group by context; compare by value. §5's prohibition — *"do NOT group solely by
normalized value"* — becomes structural rather than a rule somebody has to remember, and
§6's three worked examples fall out without special cases:

| §6 says | Why it happens |
|---|---|
| same institution + field + scope + programme + value → AGREES | same context key, same value key, two sources |
| institution-wide vs programme-specific → do not merge | different `program=` component, so they never meet |
| same value, different academic years → separate | different `period=` component |

**Cross-institution grouping never happens.** Two universities publishing one fee is a
coincidence, not corroboration. Grouping by value alone would have merged 103
`LANGUAGE_TEST` candidates across up to nine institutions.

### Sentinels, and one deliberate asymmetry

A key component that says *"we do not know"* — `UNRESOLVED` scope, `PROGRAM_UNKNOWN`,
`PERIOD_NOT_STATED`, `ROUND_NOT_STATED` — is a sentinel. The sentinel gate applies to
**conflicts and not to agreement**, and the asymmetry is the point:

* A conflict asserts that two sources answered the **same question** differently. A key
  of unknowns has not established that the question was the same — Caltech's
  admit-reply date and its application deadline are not rival answers. So a thin context
  yields `INSUFFICIENT_CONTEXT` with `unconfirmed_disagreement` set, reported and never
  dropped.
* Agreement verifies nothing (§20) and must never auto-accept. Two pages agreeing about
  a score neither of them scopes is still corroboration a reviewer can use, so the group
  is `AGREES` with `context_is_thin` set — they are told it agrees, not that the scope
  was established.

Without that gate, a fee table's ten budget line items read as one ten-way
disagreement. With it on both sides, `AGREES` became unreachable.

### Fields that are never grouped

Two field kinds carry nothing that could make two of their claims the *same* claim, and
inventing a key would produce agreement and conflict numbers that mean nothing:

* `ADMISSION_REQUIREMENT` — the value is prose.
* `ACADEMIC_CALENDAR_EVENT` — the identity is the event's label, and the stored evidence
  is a 160-character window around the date that routinely spans two or three adjacent
  entries. Grouping by (institution, year) put eight of Cornell's 2027 events in one
  group and reported them as eight competing values.

### A conflict can be in the context

Harvard's two pages both give January 1 as a deadline; one calls it Regular Decision and
the other Early Action. Keyed by context those are two unrelated groups and the
contradiction disappears, so there is a second detector for one value carrying
contradictory context. It requires the component the two sides **differ on** to be
stated on both — differing only because one page named its round and the other did not
is a gap, not a contradiction.

### What the real corpus contains

| verdict | groups | candidates |
|---|---|---|
| AGREES | 3 | 6 |
| POSSIBLE_DUPLICATE | 21 | 95 |
| CONFLICTS | 0 | 0 |
| INSUFFICIENT_CONTEXT | 1,405 | 1,447 |
| SINGLE | 388 | 388 |
| **total** | **1,817** | **1,936** |

Plus **7 unconfirmed disagreements** over 49 candidates, all `APPLICATION_DEADLINE`.

The three `AGREES` groups are Harvard's undergraduate and graduate admissions pages
agreeing that IELTS and TOEFL are accepted, and one deadline. Two genuinely different
sources, and the most corroboration 1,936 candidates across 27 institutions could
produce.

**This corpus contains essentially no corroboration, and that is the finding.** 75% of
candidates cannot be compared safely — 985 admission requirements because the value is
prose, 242 calendar events because they have no identity, 133 degree levels whose value
is null, 38 tuition amounts whose currency, billing unit or student category is
unresolved. Do not build a merge or voting mechanism on this. Build the review queue the
LOW bucket actually needs.

---

## 7. Program context groups

479 program candidates fall into exactly **226 groups** keyed on `(extraction, locator)`
— 199 of size 2 (`PROGRAM_NAME` + `DEGREE_LEVEL`) and 27 of size 3 (plus
`DISCIPLINE_HINT`). That key is the only complete one: 133 of 226 `DEGREE_LEVEL` rows
are `DEGREE_LEVEL_AMBIGUOUS` with a null value, so they carry no `_context.program_name`
to join on.

`DURATION`, `STUDY_MODE`, `CAMPUS` and `FACULTY_OR_SCHOOL` are in §8's list and have
**zero rows at any rule version** — the regexes never fired on 175 documents. Reported
rather than left to look like an oversight.

### §9: 210 of 226 groups are quarantined

| body confirmation | groups |
|---|---|
| `NOT_RECORDED` (link-derived) | 180 |
| `UNCONFIRMED` (no semantic container) | 30 |
| `CONFIRMED` (`main`, `article`, PDF) | 16 |

§9 asks that program grouping exclude navigation *"unless the document parser explicitly
labelled the content as real body content"*. For 180 of them the parser **cannot**:
`Link` records no container at all, so a catalogue anchor and a site-wide course picker
are indistinguishable on the row. C47's chrome guard reads `block.container` and the
link loop never reaches it.

The 30 `UNCONFIRMED` ones are visibly contaminated: Cambridge's are all
`["Courses for 2027 entry", "Vertical menu", <letter>]` — a site-wide A–Z course picker
— and Northwestern contributes 115 link-derived names from one page including "The
Graduate School", "Graduate Programs" and "Graduate Academic Policies and Procedures".

So they are held back from review rather than silently trusted, and **"`Link` carries no
container" is a named blocker** (§13 below).

---

## 8. Language groups

Anchored on `(extraction, block, list item, test)` — 103 groups over 113 candidates, of
which 6 carry an overall score, 2 a component score, and 1 both.

The **test is part of the anchor**, which is what enforces §16's "never merge IELTS with
TOEFL". The block alone is not enough: UNSW's block 21 list item 0 is one
~13,200-character list item naming IELTS, TOEFL, PTE, CAE and CPE, and grouping on the
block would merge five separate statements.

§18's requested exemplar — an IELTS overall with its component minimum — has **no
instance in this corpus**. There are zero IELTS component scores; the four component
rows are TOEFL ×2 and CPE ×2, and the single overall-plus-component group is CPE at
UNSW. The grouping is tested on fixtures that do contain the IELTS shape.

One known duplicate: UNSW's "min. 169 in each" is recorded twice at different character
spans, once attributed by binding wording and once by position. Two spans are two
pieces of evidence, so the group reports both rather than collapsing them (§5).

---

## 9. Deadline groups

§18 asks for institution, programme context, intake, round, applicant scope and academic
year. **Four of those six are absent from every deadline candidate**: zero carry
programme context, applicant scope or intake, and one carries an academic year. The key
is what the data supports, and the rest is reported as missing rather than invented.

Of 77 current deadline candidates: 20 state an entry year, 17 state a round in their own
wording, 11 carry a round taken only from a section heading, 1 an academic year. The
groups come out 21 `SINGLE`, 7 `INSUFFICIENT_CONTEXT`, 2 `POSSIBLE_DUPLICATE`,
1 `AGREES`.

A round label the page only implied by section heading does **not** separate a group.
Caltech's "Early Action" heading stamped that label on a date whose own wording reads
*"January 4, 2027 for Regular Decision"*, so `round_label_source` is now recorded and
only a round the wording stated is a separator.

---

## 10. Academic calendar separation

242 `ACADEMIC_CALENDAR_EVENT` candidates; **0** `APPLICATION_DEADLINE` candidates from a
calendar page. 168 from HTML, 74 from the one registered PDF. 19 of 242 state a year.

**Zero of the 242 contain application-deadline wording** — checked against both the
event text and the evidence, for `deadline`, `applicat*`, `apply by` and `closing date`,
with a positive control showing `due` (27) and `last day` (36) matching. So §19 has
nothing to promote, and nothing is converted. The four `admission` hits are Caltech's
*"last day for admission to candidacy"*, a degree milestone.

---

## 11. Admission requirement quality

985 candidates, classified by structural evidence only, in a cascade that partitions:

| class | n | share | bands |
|---|---|---|---|
| `GENERIC_REQUIREMENT_PROSE` | 358 | 36.3% | LOW 358 |
| `NAVIGATION_OR_CHROME` | 178 | 18.1% | LOW 176, **MEDIUM 2** |
| `EXPLICIT_REQUIREMENT_SENTENCE` | 161 | 16.3% | LOW 47, MEDIUM 114 |
| `QUALIFICATION_EXAMPLE` | 148 | 15.0% | LOW 148 |
| `INSUFFICIENT_CONTEXT` | 96 | 9.7% | LOW 96 |
| `REQUIREMENT_LIST_ITEM` | 44 | 4.5% | LOW 16, MEDIUM 28 |
| `REQUIREMENT_TABLE_ROW` | 0 | 0.0% | — |
| **total** | **985** | | |

`REQUIREMENT_TABLE_ROW` is structurally unreachable: `admission.extract` builds units
only from list items and paragraph/quote blocks, so no requirement candidate has ever
come from a table. The branch stays, because *"we looked and there are none"* is a
different statement from *"we never looked"*.

### C47's fix works, and 178 chrome rows still got through

Not one of the 985 comes from a `nav`, `header`, `footer` or `noscript` container. They
sit in `main` (517), untagged (243), `article` (223), `aside` (1) and `form` (1). The 178
were found by three disjoint structural tests:

* **146** whose evidence is character-identical to a link label in the same document —
  navigation, calls to action and news teasers rendered as paragraphs inside `main`;
* **30** raw inline `<script>` / JSON payloads read as paragraph text;
* **2** navigation menus long enough to saturate the stored evidence at 4,000 characters
  (the real list items are 30,326 characters of "Open submenu …").

**Two of them carry MEDIUM**, which makes them indistinguishable by band from a real
requirement sentence: Manchester's "International entry requirements" and UCL's
"Further information about the Engineering Foundation Year eligibility and entry
requirements". Both are link labels that matched the requirement pattern under a
requirements heading.

Both durable fixes belong upstream and are named as blockers below.

---

## 12. Applicant scope reconciliation

**910 current candidates carry `SCOPE_MAPPING_REQUIRED`, all of them
`ADMISSION_REQUIREMENT`** — 92.4% of that field. The 1,066 figure quoted from Step 5C.2
was correct for the rule version current then and is stale: the admission rule has moved
twice since, and across all versions the count is now higher still.

| resolution | n | share |
|---|---|---|
| `UNRESOLVED` | 859 | 44.4% |
| `NOT_APPLICABLE` | 721 | 37.2% |
| `PARTIALLY_RESOLVED` | 356 | 18.4% |
| **resolved to an `applicant_scope_id`** | **0** | |

`NOT_APPLICABLE` is the 721 programme names, degree levels, discipline hints and
calendar events, which carry no applicant scope at all. Counting them as unresolved
would inflate the figure with rows that were never going to have one.

Country and qualification-system hints are kept apart and counted separately, because
they are different claims about different things.

A country hint and a qualification hint are kept **separate** because they are different
claims: an International Baccalaureate candidate may hold any passport, and mapping IB
to a country would invent a nationality the page never mentioned. Where the scope came
from is recorded too — the sentence saying so and its section heading saying so are
different strengths of evidence.

**Zero resolve to an id, and not because the mapper is weak.** `applicant_scope`
contains exactly one row, `UNIVERSAL`, which §12 forbids inferring from silence;
`qualification_group` is empty. Until the client's scope taxonomy is seeded, every
proposal is a hint with a null id.

### Two live bugs this found

Auditing the stored scopes found the marker patterns wrong in opposite directions:

* `\bUSA?\b` compiled with `re.I` matched the English pronoun **"us"**, and had asserted
  a United States applicant scope on pages reading "contact us", "study with us" and
  "Explore what makes us special". Worse than a wrong label: a non-empty scope list sets
  `unresolved_reason` to NULL, so those rows read as *scope resolved* — §10's failure
  mode arriving through the back door, from pages that said nothing about scope at all.
* `china(?:ese)?` is "chin" + "a" + an optional "ese". It matched "china" and the
  non-existent "chinaese", and could never match **"Chinese"** — the single most
  important scope form for a product built for Chinese students.

The rule is now per-alternative: a NAME is case-insensitive, an ACRONYM is not. Fixing
that the first time by making the whole pattern lowercase broke every country name, so
there is a test for each direction.

---

## 13. Tuition

All 40 current candidates are dumped in full by `make claims-tuition-review`. The
financial-context class is a **secondary review hint** derived from the labels the
publisher supplied; the candidate is unchanged, and no class is promoted automatically —
a cost-of-attendance total is frequently the number a student actually needs.

| class | n | share |
|---|---|---|
| `TUITION_FEE` | 27 | 67.5% |
| `HOUSING` | 5 | 12.5% |
| `COST_OF_ATTENDANCE_COMPONENT` | 3 | 7.5% |
| `ESTIMATED_TOTAL_COST` | 3 | 7.5% |
| `MEAL_PLAN` | 1 | 2.5% |
| `OTHER_MANDATORY_FEE` | 1 | 2.5% |
| `UNKNOWN_FINANCIAL_AMOUNT` | 0 | — |

Classification reads, in order: a table's column label, then its row label, then the
**wording by proximity to the amount**, then the heading path deepest-first. Getting that
order wrong is not cosmetic — the first run returned 18 `ESTIMATED_TOTAL_COST` and zero
`HOUSING`, because Berkeley's housing line sits under a page heading reading "Student
Budgets (Cost of Attendance)" and the heading was consulted before the sentence. After
the correction it is 3 and 5.

Proximity matters for the same reason C44 needed binding attribution: Harvard's
cost-of-attendance table collapses to `Tuition$56,550Fees$5,126Housing$12,922Food$8,268`,
where every class matches somewhere. The label beside the amount is the one that belongs
to it. Those labels are found with letter-only lookarounds rather than `\b`, because
there is no word boundary before "Housing" when the preceding character is a digit.

Only **2 of 40** carry both a billing unit and a student category, which is why 38 are
`INSUFFICIENT_CONTEXT` for grouping and why a coarser key would have reported Berkeley's
ten budget line items as a ten-way conflict.

---

## 14. Review priority and queues

Priority is a sum of named, signed factors, and every one that applied is returned with
it. §27 forbids a black-box score, so there is no weight here that cannot be read off
the row:

| factor | points |
|---|---|
| high-risk field (tuition, deadline, language, admission requirement) | +40 |
| HIGH / MEDIUM / LOW extraction confidence | +25 / +15 / −20 |
| corroborated by another source | +20 |
| parser labelled the block as page body | +10 |
| the source has stored evidence | +5 |
| body confirmation missing | −10 |
| in conflict | −15 |
| applicant scope unresolved | −20 |
| thin/static-insufficient source | −25 |

| queue | n | share | bands |
|---|---|---|---|
| `ONLY_CANDIDATE` | 690 | 35.6% | LOW 690 |
| `PRIMARY` | 665 | 34.3% | HIGH 8, MEDIUM 657 |
| `LOW_CONFIDENCE` | 580 | 30.0% | LOW 580 |
| `NOT_QUEUED` | 1 | 0.1% | HIGH 1 |

LOW candidates are stored, kept and never deleted (§28). They stay out of the primary
queue unless nothing better exists for the same field on the same page — 690 of them
are the only answer their page gives, which is §28's stated exception.

The one `NOT_QUEUED` row is the candidate accepted below: a decided candidate leaves the
queue, and its decision is what took it out rather than its band.

---

## 15. Promotion readiness

`claim_promotion_ready` is **0 of 1,936**, and that is correct.

| blocker | n |
|---|---|
| `SOURCE_NOT_ELIGIBLE` | 1,936 |
| `REVIEW_STATE_IS_UNREVIEWED` | 1,935 |
| `SCOPE_UNRESOLVED` | 910 |
| `UNRESOLVED_CONFLICT` | 0 |

No official domain has been verified, so every source is `NOT_ELIGIBLE` and nothing can
be promoted whatever a reviewer decides. One candidate has been accepted to demonstrate
the loop — Stanford's `$13,545` under a column labelled "Quarterly Tuition" — and its
only remaining blocker is `SOURCE_NOT_ELIGIBLE`. `test_an_accepted_candidate_is_still_refused_by_c27`
attempts the `field_claim` insert and reads the refusal.

---

## 16. Nothing was published

`field_claim` 0 · `field_provenance` 0 · `change_proposal` 0 · `change_proposal_item` 0
· and all 21 canonical tables 0. All 319 pilot sources still `NOT_ELIGIBLE`. Zero
verified official domains. One candidate review decision, which §34 permits.

---

## 17. Integrity, over everything and not a sample

**Locator resolution: 1,936 / 1,936.** Every current candidate's locator resolves to
wording deterministically related to what the claim quotes:

| relationship | n |
|---|---|
| `EXACT_RAW` | 1,434 |
| `CONTAINS_RAW` | 499 |
| `ALL_RAW_TOKENS_PRESENT` | 3 |

§24 asks for a provable relationship rather than byte equality, and the three
`ALL_RAW_TOKENS_PRESENT` rows are why the distinction is needed: a deadline's
`value_raw_text` joins the date match to the time match ("13 January 2027 6pm") while
the page reads "13 January 2027 at 6pm", so the raw text is not a substring of its own
evidence — but every whitespace-separated token of it is present, which is provable.
`MISMATCH` and `UNRESOLVED` are not treated as proof.

**Routing: 0 violations** across all 1,936, over 19 distinct
(responsibility → field kind) pairs.

---

## 18. Rule corrections this step made

Auditing the persisted candidates found six more defects. Each is fixed, versioned and
regression-tested; superseded rows are retained.

| Rule | Version | What this step corrected |
|---|---|---|
| `admission-rule-extractor` | 4 | two scope markers wrong in opposite directions (§12) |
| `program-rule-extractor` | 4 | a heading's preceding **sibling** was recorded as its ancestor, so "Archaeology, BA (Hons)" sat under "Anglo-Saxon, Norse, and Celtic, BA (Hons)" |
| `language-rule-extractor` | 5 | a number inside a longer token became a score — Toronto's postcode "M5R 0A3" became IELTS 5.0 and the ZIP "08541-6151" became TOEFL 85, both plausible scores no range check could catch; and a comma was not coordination, so in "7.0 in IELTS, 100 in TOEFL" the 100 bound to IELTS and TOEFL got nothing |
| `deadline-rule-extractor` | 4 | a time anywhere in the block was attached to the block's first date ("15 October 18:00" from a sentence whose 18:00 belonged to 13 January); a bracketed timezone was missed; a round label from a heading was stamped on another round's date; a year range was read as a day |
| `calendar-rule-extractor` | 4 | "winter term 2026-27 December 4" produced the date "27 December", and the bad match then made "4 November" out of the text after it |
| `tuition-rule-extractor` | 4 | unchanged this step |

---

## 19. Commands

```bash
make claims-integrity          # locator resolution over ALL current candidates + routing
make claims-groups             # agreement, conflict and unconfirmed-disagreement groups
make claims-programs           # program context groups and the section 9 quarantine
make claims-language-groups    # language requirement groups
make claims-deadline-groups    # deadline groups and context coverage
make claims-calendar           # academic calendar separation
make claims-admission-quality  # the 985, classified structurally
make claims-admission-sample   # a stratified manual-review sample [limit=100]
make claims-scope              # applicant scope reconciliation
make claims-tuition-review     # all 40 tuition candidates, in full
make claims-queue              # review queues and explainable priority
make claims-readiness          # promotion blockers
make claims-sources            # source verification assistance
make claims-review-list        # the queue [kind= queue= state= limit=]
make claims-review-show        # one candidate in full [candidate=<id>]
make claims-review-accept      # [candidate= actor= code= reason=]
make claims-review-reject
make claims-review-context
make claims-review-untouched   # prove nothing publishable or canonical moved
```

Offline throughout. The only thing any of them writes is a `field_claim_candidate_review`
row.

---

## 20. Exact blockers before promotion or source verification

1. **No official domain is verified.** Every source is `NOT_ELIGIBLE`, so
   `claim_promotion_ready` is 0 and will stay 0 until Step 5A's verification workflow
   runs. `make claims-sources` assembles the evidence a reviewer needs for it: submitted
   and effective URL, final host, page title, responsibility, fetch status, content type,
   candidate counts by type, and whether the host matches a verified domain.
2. **The `applicant_scope` taxonomy does not exist.** One row, `UNIVERSAL`. Until the
   client decides which scopes matter, 894 admission requirements cannot be resolved and
   §12 forbids the one shortcut available.
3. **`Link` records no container**, so 180 of 226 programme groups cannot be confirmed
   as body content. Fixing it is a Step 5C.1 change to the document schema, which alters
   every document hash and supersedes every candidate — a decision to take deliberately,
   not as a side effect.
4. **Inline `<script>` text reaches paragraph blocks**, putting 30 JavaScript payloads
   into the admission candidates. Dropping `<script>` and `<style>` subtrees in the
   normaliser removes them from every extractor at once; same re-extraction cost as (3).
5. **A link label rendered as a paragraph is indistinguishable from prose** on the
   candidate row. The normaliser already holds `document.links` when it builds blocks, so
   the identity test is cheap at extraction time — 146 rows, including the two MEDIUM
   ones.
6. **The privilege suite does not run in this environment.** 15 tests skip because the
   runtime-role passwords are not configured. This does not affect deterministic
   extraction, and it is a **hard gate before any promotion or publication work**: the
   separation between the role that reviews and the role that publishes is exactly what
   those tests cover. They must be run, not weakened.
7. **841 LOW admission requirements need a human pass, not a better regex.** Narrowing
   the class by requiring requirement wording would drop real requirements — "We also
   accept the Cambridge Pre-U Diploma", "You need to have undertaken a significant
   research project" — so the band carries the distinction instead. That is a reviewing
   workload, and the quality classes exist to make it orderable.
8. **There is no corroboration to lean on.** 0 `AGREES` groups, 0 confirmed conflicts,
   74% of candidates not safely comparable. Step 5C.4 should not assume a voting or
   merge mechanism will help.


---

## 21. Supersession and stranded decisions

Step 5C.4. A candidate can be stale in two unrelated ways, and conflating them would
repeat the mistake D39 corrected.

### 21.1 Two axes

`claim_rule_version` answers "is this what the rule says now?". The new
`document_artifact_version` answers "is this what the page says now?". Re-extraction
keeps the previous artifact, so after the normaliser reached `2.0.0` every snapshot had
two extractions and a candidate read from the superseded parse would otherwise have
counted as live for ever.

`candidate_review_state` exposes `rule_superseded` and `document_superseded` as separate
columns, with `is_superseded` as their combination. Over the whole table:

| rule_superseded | document_superseded | rows |
|---|---|---|
| false | false | 1,399 |
| false | true | **472** |
| true | true | 7,593 |

That 472 is the arithmetic proof the axes are independent: 242 calendar + 113 language +
40 tuition + 77 deadline, exactly the four rules that did not change. Had the rule
versions been bumped to express the parser change, those 472 correct claims would have
been relabelled as a new rule's output.

Both registries are tables the code rewrites on every pass, never literals in the view.
That is not tidiness: the first attempt froze the rule versions into the view and
reported *every* current candidate as superseded, silently, with a plausible total
(D53).

### 21.2 A decision is surfaced, never transferred

One decision exists: `ACCEPTED` / `CORRECT_AS_EXTRACTED` on a `$13,545` tuition figure.
It is now `review_superseded` -- `rule_superseded=false`, `document_superseded=true`,
because the page was re-parsed although the tuition rule did not change.

It was **not** copied to the candidate that replaced it. Section 11 forbids inferring
that two candidates are the same observation because their normalized values match, and
that is the right prohibition: a reviewer who accepted one has not looked at the other,
and transferring the decision would attribute a human judgement to evidence nobody read.
`decision_state` still reads `ACCEPTED` on the row where it was decided; the live corpus
is 1,399 `UNREVIEWED`.

---

## 22. The privilege gate is executable

Fifteen tests that exercise the `app_api`, `app_worker` and `app_publisher` roles used to
skip. They skip when `APP_API_PASSWORD` / `APP_WORKER_PASSWORD` /
`APP_PUBLISHER_PASSWORD` are unset, and on this machine all three are unset and
**unrecoverable**: no `.env` exists anywhere in the repository, and `pg_authid` stores
only a SCRAM-SHA-256 verifier.

A skipped privilege test is not a passing one. Those grants are the mechanism that stops
a worker writing canonical facts, and "we could not check" is the same evidence as "it
does not work".

`tests/runtime_credentials.py` generates one password per role per run against a
**loopback** database, using the owner connection the suite already has, and restores
each original verifier byte-for-byte afterwards -- a stored verifier can be re-applied
verbatim, so the rotation is reversible without anyone learning a plaintext. Tests then
authenticate as the real roles over the same `scram-sha-256` path production uses, with
the same grants and the same `NOINHERIT`.

Three alternatives were rejected:

- **`SET ROLE` from the owner** reproduces the privileges but not the authentication,
  and would not detect a role that cannot log in at all.
- **A member role** granted `app_api` authenticates as the member. `NOINHERIT` alone
  makes a member's privileges different from the role's.
- **Catalog assertions** prove a grant was written, not that the server enforces it.
  Section 13 forbids that substitution, and the interesting failures are exactly the
  ones where the catalog looks correct.

**0 privilege tests skipped**, from 15. `make privilege-gate` runs them.

One cost, stated plainly: the restore runs in a `finally`, so an exception or an
interrupt is safe, but `SIGKILL` is not -- and that happened once during development,
leaving all three roles carrying generated passwords whose predecessors existed nowhere
else. The remedy is `make runtime-roles-reset`. The verifiers are deliberately not
backed up anywhere: a copy of a credential, written to make a test tidier, is a worse
thing to own than an interrupted test run.

---

## 23. Source verification preparation

Step 5C.4 sections 15-21. `scripts/verify_sources.py` prepares the decision that C27
requires before any candidate can become a `field_claim`. **It writes nothing.**

- `packet` -- one block per physical page: what was requested, what answered, the
  redirect chain hop by hop, the stored title, the claimed responsibility, and the two
  questions a reviewer must answer.
- `domains` -- the 120 hosts, which is the grain a domain decision is made at. Each host
  maps to exactly one institution, which makes the grouping tidy and proves nothing.
- `redirects` -- the 15 pages that ended on a host they did not request, and the 21
  cross-host hops that occur inside chains where the endpoint hides them. Every pair
  stays inside one registrable domain, which is the most persuasive-looking case there
  is and still confers nothing.
- `responsibilities` -- what the workbook claims each page is, beside what the rules
  actually produced from it. A mismatch is a question, never a reclassification.
- `authority` -- proof that the decision has a home already: `official_domain`,
  `source_mapping`, `source_field_binding`, `source_degree_scope` and
  `pilot_collected_source` all carry the actor / reason / timestamp columns section 17
  requires, and all are empty of decisions.
- `scopes` -- the applicant-scope vocabulary the pages actually use.
- `untouched` -- proof that nothing canonical or publishable moved.

### 23.1 What the scope vocabulary turned out to be

Of 873 current admission requirements, **799 state no applicant scope at all** and are
`SCOPE_MAPPING_REQUIRED` -- unresolved, never universal. The remaining 82 markers span
18 distinct wordings, and the finding is that they are **two different axes wearing one
name**:

- *Qualification systems*: A-level (61 across seven spellings), IB (7), AP (2).
- *Applicant jurisdictions*: United States (5), Hong Kong (2), Singapore (2),
  Malaysia (1), China (2).

"A-levels" is not an applicant scope; it is the qualification an applicant holds. A
taxonomy that treats them as one field will be wrong about both. Two false positives are
visible in the same output and are reported rather than patched: `Chinese` matched
"Chinese medicine" (a programme subject) and `China` matched "Beijing, China" (a
location). Section 21 forbids fixing these with a China-shaped special case, and the
general problem -- a country name is not a scope marker unless it describes the
applicant -- is what the vocabulary is being returned for.
