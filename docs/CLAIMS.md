Written for: whoever builds Step 5C.3's promotion into `field_claim`, and whoever has
to explain later why a candidate value was never published.

# Evidence-backed field claim extraction

Normalised document → specialised deterministic extractors → append-only candidate
`field_claim_candidate` rows, with a locator, the evidence wording, a confidence band
and a stated reason.

**Not** candidate → canonical fact. Nothing here creates a `field_claim`, a
`change_proposal`, a `field_provenance` row, a university, a programme, a tuition, a
deadline or a requirement. No LLM is involved and no page is fetched.

---

## 1. `field_claim` could not hold these claims, and that is C27 working

The instruction asked for `field_claim` rows. Attempting one raises:

```
source ... is NOT_ELIGIBLE, which may not support a published fact
```

`field_claim_requires_eligible_evidence` refuses any claim whose source is not
`OFFICIAL_VERIFIED`, `AUTHORIZED_EXTERNAL` or `AUTHORIZED_RANKING`. All 319 pilot
sources are `NOT_ELIGIBLE`, because nobody has verified a single official domain yet.

Two ways to comply, both forbidden: weaken C27, or fake the verification. So there is a
third table.

> `field_claim` is the **publication** claim plane: a row there asserts that a value
> may become a published fact, gated on eligibility somebody earned.
> `field_claim_candidate` asserts something weaker and earlier — *this extractor found
> this candidate in this exact region of this document* — which is true of a page
> nobody has verified.

This is D33 one plane further up. Step 5B had to separate `FETCHABLE` from
`PUBLICATION_ELIGIBLE`; this step separates **finding a value** from **permission to
publish it**. `test_field_claim_still_refuses_a_pilot_source` attempts the insert and
reads the refusal, so if C27 is ever relaxed the design note above fails a test rather
than quietly becoming false.

---

## 2. What a claim carries

| Column | Why |
|---|---|
| `extraction_id` | lineage to the document, the snapshot, the run, the source, the institution |
| `pilot_collected_source_id`, `source_responsibility` | which workbook claim authorised this extractor to read this page |
| `field_kind` | one of fourteen, a CHECK not an enum — extractor output categories grow |
| `value_normalized` | the structured candidate, or `NULL` |
| `unresolved_reason` | why it is `NULL` or incomplete. A CHECK requires one or the other |
| `value_raw_text` | the exact wording that produced the value |
| `evidence_text` | the surrounding sentence, item or row a reviewer needs |
| `locator` | where in the document, precisely — see §3 |
| `extractor_name`, `extractor_version` | which rule, at which version |
| `confidence_band`, `confidence_reason` | HIGH/MEDIUM/LOW **with the reason beside it** |
| `claim_fingerprint` | identity, for idempotency — see §4 |

Append-only, via the same `app_forbid_mutation` trigger the rest of the evidence plane
uses. A claim records that an extractor said something; revising it in place would
erase what it said, which is the only thing the row is for.

The schema refuses what §33 would otherwise merely flag: blank evidence, blank raw
text, an unexplained band, an unknown field kind, a band outside the three, a
fingerprint that is not sha256 hex, a `{}` locator, and a null value with no reason.

---

## 3. A locator names the region, not the page

`extraction_id` alone says *"somewhere in this 40,000-character page"*, and a reviewer
asked to confirm a £38,000 fee against that is being asked to re-read the page — which
is the work the extractor was supposed to have done.

So a locator carries the block index and the heading path, plus whichever of
`list_item_index`, `table_index`/`row_index`/`column_index`, `pdf_page`, `link_index`
and `char_start`/`char_end` the region needs. `resolve(document, locator)` returns the
text it points at, and the lineage proof asserts that text is the wording the claim
quotes.

**A locator that does not resolve is decoration**, and two claims that share one are
worse than that — see C45 and C46.

---

## 4. Identity is the region, not the value

```
fingerprint = sha256(extraction_id, field_kind, locator, extractor, version)
```

Deliberately **not** over the value. Two official pages stating the same fee are two
pieces of evidence and must stay two claims (§26): corroboration is the most useful
thing a second source gives you, and it only exists as two rows. And a rule change that
alters a number should produce a second *version* of the same claim, not a brand-new
one.

`ON CONFLICT (claim_fingerprint) DO NOTHING` on insert, so a repeat pass is a no-op.
That is also why a collision is dangerous: it is silent. See §9.

---

## 5. Responsibility decides what may be read

`ROUTING` maps the workbook's `source_type` to the extractors it authorises:

| Responsibility | Extractors |
|---|---|
| `TUITION_FEES` | tuition |
| `LANGUAGE_REQUIREMENTS` | language |
| `APPLICATION_DEADLINES` | deadline |
| `PROGRAM_CATALOG` | program |
| `PROGRAM_PAGE` | program, admission, language |
| `ENTRY_REQUIREMENTS` | admission, language |
| `UNDERGRADUATE_ADMISSIONS` / `POSTGRADUATE_ADMISSIONS` / `PHD_ADMISSIONS` | admission, language, deadline |
| `ACADEMIC_CALENDAR` | calendar |
| `UNIVERSITY_HOME`, `FACULTY_OR_SCHOOL`, `OFFICIAL_PDF`, `GOVERNMENT`, `AUTHORIZED_RANKING`, `UNCLASSIFIED` | **none** |

The empty sets are written out rather than omitted, so the intent is visible: these
pages are fetched and normalised, and no business extractor may read them. A homepage
saying "from £28,000" is marketing, and treating it as a fee claim is how a brochure
number becomes a published fact.

Responsibilities are a **union** — a page claimed for postgraduate admissions and for
deadlines runs both sets — but each extractor runs **once**, attributed to the first
responsibility that authorised it. Running it twice would create two identical claims
for one sentence.

The authorising responsibility is stored on every claim, so *"why did a language claim
come off this page"* is answerable from the row rather than from reading the routing
table in the code.

---

## 6. The refusals

Most of the rules here exist to **not** produce something.

| Rule | What it refuses |
|---|---|
| §11 | *"English proficiency required"* produces **no score**. A fabricated 6.5 is indistinguishable downstream from a real one |
| §12 | "at least 7.0" and "above 7.0" are different; "no component below 6.5" is a floor on every component, not a score for one |
| §13 | No midpoint, ever. A range keeps both endpoints |
| §13 | No currency from the country. "30,000" on a UK page has not said pounds |
| §13 | No billing unit from convention. A displayed fee is not annual because annual is common |
| §14 | No student category from the currency. "£38,000" does not say who pays it |
| §16 | No year, time or timezone that the page did not state. C9/C13: a date-only fact converted to midnight UTC is a fabricated instant |
| §17 | "Round 1" has a number; "Priority" does not, and inventing one publishes an ordering nobody stated |
| §7 | "Graduate" is not a doctorate and "Advanced" is not a level. An ambiguous award keeps its label with a null level |
| §10 | A requirement with no country wording is `SCOPE_MAPPING_REQUIRED`, **never** `UNIVERSAL`. Asserting universality from silence turns a rule written for A-level applicants into a Chinese applicant's requirement |
| §18 | A page that does not mention tuition produces **no tuition claim** — and certainly not `OFFICIALLY_NOT_PUBLISHED` |
| §22 | An academic calendar's "Beginning of instruction" is an `ACADEMIC_CALENDAR_EVENT`, not an application deadline |
| §26 | Two conflicting figures stay two claims. No winner is chosen here |
| §28 | A fee on page A and the word "international" on page B do not combine into anything |

`VARIABLE` is not the same as absent: a page that addresses fees and gives no figure has
said something. A page that never mentions fees has not.

---

## 7. Confidence is a band with a reason, never a decimal

`0.873421` would imply a calibrated probabilistic model. There is no calibration set
behind these rules, so the number would be decoration over a judgement — worse than the
judgement stated plainly.

* **HIGH** — an explicitly labelled table cell, or a field whose label is unambiguous.
* **MEDIUM** — wording that is itself about the field, under a heading that matches.
* **LOW** — the right shape of value with thin surrounding context.

Every band carries `confidence_reason` in the same row, and the schema refuses a blank
one. **A HIGH band is still not verified and still not publishable.**

The bands were re-calibrated after the first audit, and the correction is the point: an
*ancestor heading alone* used to earn MEDIUM, which on an admissions page is every
paragraph on it. See C47.

---

## 8. What the real pass produced

175 stored documents, offline, no network request:

| | |
|---|---|
| documents considered | 175 |
| authorised for at least one extractor | 145 |
| authorised for nothing (routing) | 30 |
| insufficient static text (<400 chars) | 4 |
| authorised and readable, rules found nothing | 46 |
| produced at least one claim | 95 |
| **candidate claims, current rule versions** | **1,941** |
| failures | 0 |

Re-running the pass creates nothing: 0 created, 1,941 already present.

| field kind | claims | HIGH | MEDIUM | LOW | with an unresolved reason |
|---|---|---|---|---|---|
| `ADMISSION_REQUIREMENT` | 985 | 0 | 144 | 841 | 894 |
| `ACADEMIC_CALENDAR_EVENT` | 242 | 0 | 242 | 0 | 223 |
| `PROGRAM_NAME` | 226 | 0 | 46 | 180 | 0 |
| `DEGREE_LEVEL` | 226 | 0 | 30 | 196 | 133 |
| `LANGUAGE_TEST` | 103 | 0 | 93 | 10 | 0 |
| `APPLICATION_DEADLINE` | 78 | 0 | 73 | 5 | 74 |
| `TUITION` | 40 | 9 | 24 | 7 | 30 |
| `DISCIPLINE_HINT` | 27 | 0 | 0 | 27 | 0 |
| `LANGUAGE_OVERALL_SCORE` | 10 | 0 | 10 | 0 | 0 |
| `LANGUAGE_COMPONENT_SCORE` | 4 | 0 | 4 | 0 | 0 |
| **total** | **1,941** | **9** | **666** | **1,266** | **1,354** |

Nine HIGH claims in 1,941. That is the point of the band: HIGH means a labelled table
cell, and nine of them is what 175 real pages actually offered.

The `ADMISSION_REQUIREMENT` split — 144 MEDIUM against 841 LOW — is the useful
part of the re-calibration: the MEDIUM rows state requirements, and the LOW rows are
prose that happened to sit under a requirements heading.

27 of 35 institutions produced claims, across 95 documents and 100 workbook claims.
Seven of the eight that produced none — Oxford, Columbia, Penn, NUS, Melbourne, Monash,
Queensland — are the institutions whose pages Step 5B could not fetch, so there is
nothing to read. **No claim was produced for a page we never read**, which is the
property that matters.

The eighth is Edinburgh, and it is a finding rather than a gap: its ten pages were
fetched and extracted, and every claim the earlier rule versions produced for it turned
out to be one the audit removed. That is what the corrections in §9 cost in recall,
stated rather than buried.

### Where the gaps are

A page with no claim is one of five different things, and conflating them is how a
coverage number stops meaning anything:

| | pages |
|---|---|
| never extracted (no stored body) | 144 |
| extraction failed | 0 |
| authorised for no business extractor | 30 |
| insufficient static text | 4 |
| authorised, readable, rules found nothing | 46 |
| produced at least one candidate claim | 95 |
| **total** | **319** |

---

## 9. What the audit found

All fourteen automated sanity checks pass on the current rule versions: no non-positive
fee, no inverted range, no implausible score, no component above its overall, no
impossible date, no time on a date-only deadline, no timezone without a time, no
`UNIVERSAL` scope, no blank evidence, no missing locator, no unexplained null, no
unexplained band, no fake decimal, **no two claims sharing a fingerprint**. Zero routing
violations. 500 of 500 sampled locators resolve to the wording their claim quotes.

The manual precision audit (≥20 samples per category, deterministic sampling so two
people audit the same rows) found six real defects, each fixed and each with a
regression test. They are recorded as C44–C47 and in the version history of the rules
they belong to. Two of them — the fingerprint collisions — had produced **no visible
failure at all**: `ON CONFLICT DO NOTHING` dropped 34 claims and the pass reported
success.

Residual, reported rather than fixed:

* `ADMISSION_REQUIREMENT` LOW (841 rows) is a broad bucket. It holds prose that sits
  under a requirements heading without stating a requirement. Narrowing it by requiring
  requirement wording would drop real requirements — "We also accept the Cambridge Pre-U
  Diploma", "You need to have undertaken a significant research project" — so the band
  carries the distinction instead of the filter.
* `PROGRAM_NAME` from link text (LOW) includes award-shaped page titles that are not
  programmes: "Master's Degree Requirements", "Postgraduate Admissions". A catalogue
  link naming an award is a weak signal and is banded as one.
* Several `TUITION` claims are cost-of-attendance line items — housing, food — rather
  than tuition. They are correctly located and correctly quoted; deciding which line is
  *tuition* is a reading, and §26 says not to make it here.

---

## 10. Rule versions, and why there are several

Each extractor carries **its own** version constant. Sharing one meant that correcting
the calendar rule would have relabelled 81 unchanged deadline claims as a new version,
which is the opposite of what versioning is for.

Superseded rows are **retained**, because the table is append-only and because
comparing two versions over one document is how rule drift becomes visible rather than
being mistaken for a source change (D4). The current-version predicate is derived from
the runner's own registry, so a bump cannot leave a report quietly counting old rows.

| Rule | Version | What each bump corrected |
|---|---|---|
| `admission-rule-extractor` | 3 | v2: stopped reading site navigation as requirement prose. v3: an ancestor heading alone no longer earns MEDIUM |
| `program-rule-extractor` | 3 | v2: a link locator that `resolve` could not follow. v3: chrome exclusion |
| `language-rule-extractor` | 3 | v2: chrome exclusion. v3: an overall-score floor, so an institution code is not a requirement |
| `deadline-rule-extractor` | 2 | v2: chrome exclusion |
| `calendar-rule-extractor` | 3 | v2: two dates in one block shared a locator. v3: chrome exclusion |
| `tuition-rule-extractor` | 4 | v2: bare digit runs, heading-only bands, descending pairs. v3: digit-run fragments and ambiguous table shapes. v4: chrome exclusion |

Of 5,977 rows in the table, 4,036 are superseded and 1,941 are current. That ratio is
what an audit-and-correct cycle looks like when nothing is overwritten.

---

## 11. Extraction retry, separately

Step 5C.1's `UNIQUE (snapshot_id, extractor_name, extractor_version)` conflated two
things: `extractor_version` meant both *"the extraction logic changed"* and *"the
filesystem was briefly unavailable"*, so a transient failure could only be retried by
lying about the first.

The uniqueness is now **partial** — `uq_extraction_result_per_version ... WHERE status
<> 'FAILED'`. One successful-or-partial result per (snapshot, extractor, version) is
still enforced; a `FAILED` row does not occupy that slot; failures accumulate as
history. `already_extracted` uses the same predicate, because a check that counted
failures while the index did not would skip the retry and leave the slot empty — a page
that could be extracted, permanently reported as done.

No `extraction_attempt` table. The acquisition plane has `fetch_attempt`/`fetch_run`
because a *fetch* has genuine in-flight mutable state: a lease, a heartbeat, a worker
that can die mid-request. Extraction is a pure function over bytes already held,
offline, in-process. A mutable attempt table would model a lifecycle that does not
exist.

---

## 12. Nothing was published

Asserted over the tables, not claimed in prose. After the full pass:

`field_claim` 0 · `field_provenance` 0 · `change_proposal` 0 · `change_proposal_item` 0
· `university` 0 · `campus` 0 · `faculty` 0 · `program` 0 · `program_offering` 0 ·
`intake` 0 · `application_round` 0 · `tuition` 0 · `admission_requirement` 0 ·
`language_requirement` 0 · `application_deadline` 0.

All 319 pilot sources are still `NOT_ELIGIBLE`. No official domain is verified. No
source mapping was promoted.

---

## 13. Security

Offline by construction: the pass reads artifacts from the object store and makes no
network request. `test_the_claim_pass_makes_no_network_request` replaces
`socket.socket` for the duration, so an HTTP client smuggled in by any library would
fail there too. No LLM, no browser, no new crawling — `fetch_attempt` is unchanged by a
claim pass, and a test asserts it.

One unreadable artifact is a reported failure, not a crash: the same per-item
containment lesson as 5B.2.

---

## 14. Commands

```bash
make claims-run           # extract candidate claims from stored documents (offline)
make claims-coverage      # coverage by responsibility, institution and gap kind
make claims-sanity        # checks for claims that cannot be true, + locator resolution
make claims-audit         # deterministic samples for manual precision review
make claims-lineage       # walk one claim back to the bytes, resolving its locator
make claims-untouched     # prove nothing publishable or canonical moved
```

Manual commands rather than CI jobs: the automated suite is fixture-based, and turning
174 universities' markup into a test dependency would make it fail when they redesign
their sites.

---

## 15. What Step 5C.3 has to decide

Promotion into `field_claim` needs the eligibility C27 asks for, which nobody has
earned yet. Before that:

1. **Verification first.** No candidate becomes a `field_claim` until its source's
   official domain is verified and its mapping promoted. That order is forced by the
   trigger, and it should stay forced.
2. **Conflict resolution is a review, not a rule.** Two claims disagreeing is the
   normal case; §26 deliberately left it unresolved here.
3. **The LOW bucket needs a human pass, not a better regex.** 841 `ADMISSION_REQUIREMENT`
   LOW rows is a reviewing workload, and the useful question is which of them a reviewer
   can dismiss in a second — which is what the evidence and locator on each row are for.
4. **`SCOPE_MAPPING_REQUIRED` (1,066 rows) is the largest unresolved reason**, and
   resolving it means deciding what applicant scope a requirement with no country
   wording has. That decision is the client's, not the extractor's.


---

## 12. Extraction hygiene

Step 5C.4. The corpus Step 5C.3 handed over was clean enough to review and not clean
enough to promote: 30 candidates were script payloads read as prose, 146 were link
labels, 178 had reached the admission rule from site chrome, and 210 of 226 programme
groups were quarantined because the row could not say where its link came from. This
step fixed the causes rather than the symptoms, which meant fixing the document model.

### 12.1 The drop pass was removing every other element

The defect was one loop:

```python
for element in tree.iter():
    if element.tag in DROPPED_TAGS:
        _drop(element)          # mutates the tree being iterated
```

`iter()` advances past the next sibling each time the current one is removed, so **22 of
29 scripts and 48 of 49 SVGs survived** and their payload was emitted as visible prose.
C52 had named the fix as "drop `<script>`/`<style>` subtrees in the normaliser". That was
accurate and incomplete: the subtrees were being dropped, just not all of them -- and
removing `<script>` from the *emitted block list* would not have helped, because
`parent.text_content()` still contains a descendant script's text.

Collecting the doomed elements before removing any of them took the fleet's
script-shaped blocks from 168 to 1 and its script-derived candidates from 20 to 0.

Removal had a second effect worth naming: `see <script>x=1</script> below` became
`see below`, welding the words on either side. A space now goes in where an element
comes out. The regression fixture is the one section 4 specifies --
`<p>real text<script>payload</script>more real text</p>` yields `real text more real
text` and never `payload`.

### 12.2 A link now knows where it was

`Link` records `container`, `in_chrome`, `block_index`, `ancestry`,
`is_link_only_block` and `is_link_only_item`; a `Block` records `in_chrome` and a
`LinkProfile`. Ancestry is **tag names only** -- no classes, no ids, no indices, capped
at six -- because section 2 forbids storing a DOM path that changes when a wrapper
`<div>` is added, and because section 5 forbids deciding anything from a CSS class
called `nav`. `text_fraction` is rounded to three places so the artifact hash cannot
depend on float noise.

21,824 of 28,047 links now carry a container. **16,874 of them -- 60% of the fleet --
are inside navigation**, which is the population the programme rule had been reading.

### 12.3 Chrome is sticky; the innermost container is not

Adding `section` to the container list created a way out of a `nav`. `container` records
the *innermost* semantic container, so `<nav><section><p>` began reporting `section`,
and any guard testing `container in {nav, header, footer}` would have waved it through.
A field added to help distinguish body from chrome made one case of it worse.

The two facts are recorded separately. `container` stays the innermost container, which
is what tells a catalogue section from a bare `<div>`. `in_chrome` is sticky: once the
walk is inside a `nav`, `header` or `footer`, everything below it is chrome however many
`<section>`s intervene.

Nothing is deleted either way. D45 still holds -- a fee table in an `<aside>` is still
in the artifact -- and the extractor decides.

### 12.4 The same shape means different things to different fields

A block whose entire text is one anchor's text is navigation on a requirements page and
a programme in a catalogue. Section 3 is explicit that the parser must not resolve this
by deleting link-only content: that would fix the navigation labels and lose the
catalogues.

So the parser records the shape and the field rule decides. The admission rule (v5)
refuses a link-only unit and excludes anything `in_chrome`; the programme rule (v5)
requires body origin -- `main`, `article` or `section`, not `in_chrome` -- for both
heading-derived and link-derived names, and asserts a catalogue responsibility on the
rule rather than relying on the routing table to have done it.

### 12.5 What that removed, and what it deliberately did not

| | before | after |
|---|---|---|
| candidates | 1,936 | 1,399 |
| script payload read as prose | 20 | **0** |
| under a navigation heading path | 72 | 5 |
| programme candidates | 479 | 54 |
| distinct programme names | 253 | 33 |
| bare title-case admission labels | 126 | 85 |

Of the 529 distinct statements lost, **seven are long prose and all seven are script
payloads**. Nothing that reads like a requirement was lost.

The five remaining navigation-path rows sit in a `<div class="breadcrumb">` with no
semantic element at all. They are reported rather than special-cased, because section 5
forbids deciding from a class name. The 85 remaining title-case labels are A-level
subject lists -- "English Language and Literature", "Art and Design: Graphic Design" --
which look exactly like navigation and are exactly what an entry requirement is made of.

One value was simply wrong and was fixed rather than reported: the degree level was read
from the whole heading path, so a page titled "Graduate Admissions" wrote
`degree_level_raw='Graduate'` onto student testimonials. It now comes from the wording
plus the innermost heading.

**What was not done.** 469 LOW rows exist because `_REQUIREMENT_HEADING` matched a
page's own title, so every paragraph beneath it became a candidate. Removing the title
from that test would delete them in one line and would take genuine requirement prose
with it. Section 7 forbids solving the LOW band by aggressive filtering, so the number is
reported and the trade is left to a reviewer. `make claims-low` classifies what remains:
of 730 LOW admission requirements, 346 are fragments with no finite verb, 281 are
ordinary prose under a requirements heading, 45 describe the application process, 36 are
cross-references and 22 are qualification-list items.

### 12.6 Which extractors were versioned, and which were not

Section 8 forbids bumping a rule version merely because the document changed, so the
question was answered by measurement: each rule's current code was run over both
artifacts of all 175 dual-artifact snapshots.

| rule | rows v1 -> v2 | statements added | removed | re-worded | version |
|---|---|---|---|---|---|
| calendar | 242 -> 242 | 0 | 0 | 0 | **unchanged, 4** |
| language | 113 -> 113 | 0 | 0 | 0 | **unchanged, 5** |
| tuition | 40 -> 40 | 0 | 0 | 0 | **unchanged, 4** |
| deadline | 77 -> 77 | 0 | 0 | 1 | **unchanged, 4** |
| admission | rule changed | — | — | — | 4 -> **5** |
| programme | rule changed | — | — | — | 4 -> **5** |

Four rules say exactly the same things about the same pages; only `block_index` moved,
on one document each, where the parser stopped emitting script blocks ahead of them.
Bumping them would have marked 472 correct claims superseded to express a fact about the
parser. That fact belongs on `document_artifact_version` instead -- see D55 and
[CANDIDATE_REVIEW §5](CANDIDATE_REVIEW.md#5-what-current-means-and-why-one-frozen-list-was-not-enough).

### 12.7 The invariants section 9 gates on

Both held on the full corpus, not a sample:

- **1,399 / 1,399 locators resolved** to the quoted wording.
- **0 routing violations**: no candidate carries a responsibility that does not
  authorise its extractor.
- 14 of 14 sanity checks clean, including no two current claims sharing a fingerprint.


---

## 13. Applicant scope

Step 5C.5. The scope rule had been asserting a jurisdiction from a bare country token,
and on the real corpus that was wrong three times out of four.

### 13.1 Twelve scopes, three of them right

| wording | scope asserted | correct? |
|---|---|---|
| "Hong Kong Certificate of Education (HKCEE) at grade C" | HK | yes |
| "Malaysia Sijil Pelejaran (SPM) at grades 1 to 6" | MY | yes |
| "Singapore/Cambridge GCE Ordinary level" | SG | yes |
| "...focuses on **Chinese medicine** and basic Western medicine" | CN | no, a subject |
| "graduate degrees at Schwarzman College in **Beijing, China**" | CN | no, a location |
| "additional locations in Beijing, Delhi, London, Paris, and **Hong Kong**" | HK | no, campus cities |
| "**NTU Singapore** is embarking on an ambitious effort..." | SG | no, an institution's name |
| "Harvard College Admissions86 Brattle StreetCambridge, MA 02138 **USA**" | US | no, a postal address |
| "(such as A-levels or **USA** Aps)" | US | no, a qualification system |
| three runs of navigation text | US | no |

Every one of the nine wrong ones had `unresolved_reason = NULL`, so they read as
**resolved**.

### 13.2 An allowlist, not a blocklist

Excluding "Chinese medicine" and "Beijing, China" fixes two strings and leaves the rule
still asserting a jurisdiction from a bare token, so the next address walks through. The
general form is what was wrong: a country token with no applicant or qualification
semantics asserts nothing.

Five named constructions, and the one that fired is recorded on the row:

| kind | example |
|---|---|
| `DEMONYM` | "Chinese applicants", "US nationals", "Indian students" |
| `APPLICANT_ORIGIN` | "applicants from China", "students educated in China" |
| `QUALIFICATION_ORIGIN` | "qualifications obtained in China", "grades from Singapore" |
| `CONDITIONAL_ORIGIN` | "if you are from China" |
| `NATIONAL_QUALIFICATION` | "Hong Kong Certificate of Education", "Malaysia Sijil ..." |

The name is on the row because section 12's rule for review evidence applies here too:
`NATIONAL_QUALIFICATION` tells a reviewer what to check, and a confidence number does
not.

**Negation is detected, not interpreted.** "Students who are non-US citizens" contains
"US citizens" and is about who the rule does *not* apply to, so the marker is dropped
rather than inverted — "not China" is not a scope, and guessing which jurisdictions it
leaves would be an inference the page never made.

Result: 12 jurisdiction scopes became 6. All three true positives survive; all nine false
positives are gone; three new ones appeared that the old rule had missed because it was
matching tokens rather than phrases.

### 13.3 Two dimensions, never collapsed

"Applicants from China" and "applicants offering A-levels" are both scopes and are not
the same kind of thing. They were distinguishable only by `applicant_country_code` being
null, which is an accident of the data rather than a statement about it.

Every marker now carries `dimension`, and a candidate carries `applicant_jurisdictions`
and `qualification_systems` beside the combined `applicant_scopes` — which stays, because
the grouping key, the quality cascade and the review-time scope proposal all read it.

Unresolved stays unresolved, and nothing becomes `UNIVERSAL`:

| state | count | reason |
|---|---|---|
| no marker at all | 804 | `SCOPE_MAPPING_REQUIRED` |
| qualification hint, no jurisdiction | 63 | `APPLICANT_JURISDICTION_UNRESOLVED` |
| jurisdiction stated | 6 | resolved |

Knowing an applicant holds A-levels does not say where they are from, and one flag
covering both would hide which of the two is missing.

### 13.4 A rule that had never run

Found while fixing the above: `language._COORDINATOR` was written as
`<BS>(?:or|and|either|alternatively)<BS>|[/;,]` and stored with **backspace characters**
where the word boundaries belonged, because a tool wrote the file through a non-raw
Python string. It compiles, it looks right in an editor and in a diff, `ruff` and `mypy`
pass — and it cannot match, because no web page contains a backspace. Since the rule was
written, only punctuation had ever coordinated.

Restoring it added one correct claim and removed none, so the language rule went to
version 6. `tests/test_source_hygiene.py` now fails on any control character in any
source file; it caught a third instance immediately, in the comment being written about
the second.


---

## 14. Publication authority

Step 5C.6. `app.domains.verification.policy.FIELD_AUTHORITY` says which source
responsibilities may **publish** each candidate field kind. It is separate from
`ROUTING`, which says which extractors may **read** each page, and the separation is the
point: one table answering both questions would mean widening extraction for recall
silently widened publication authority.

| field kind | may be published by |
|---|---|
| `TUITION` | `TUITION_FEES` |
| `LANGUAGE_TEST` / `_OVERALL_SCORE` / `_COMPONENT_SCORE` | `LANGUAGE_REQUIREMENTS`, entry/UG/PG/PhD admissions |
| `APPLICATION_DEADLINE` | `APPLICATION_DEADLINES`, UG/PG/PhD admissions |
| `ACADEMIC_CALENDAR_EVENT` | `ACADEMIC_CALENDAR` only |
| `ADMISSION_REQUIREMENT` | entry/UG/PG/PhD admissions |
| `PROGRAM_NAME` / `DEGREE_LEVEL` / `DISCIPLINE_HINT` | `PROGRAM_CATALOG`, `PROGRAM_PAGE` |

An admissions page may publish a language requirement because institutions really do
state them there. It may not publish a fee: the fees page is where the institution states
those, and a page that mentions a number is describing it.

`ACADEMIC_CALENDAR_EVENT` is authorised by the calendar and nothing else, because a
calendar entry is explicitly not a deadline -- converting "Beginning of instruction" into
an admissions deadline would be inventing a fact.

**The invariant.** Publication authority must be a subset of extraction authority. It is
asserted, not assumed, and it caught the first draft granting `APPLICATION_DEADLINE` to
`ENTRY_REQUIREMENTS` -- a page `ROUTING` does not let the deadline extractor read, so no
deadline candidate can originate from one and the grant authorised nothing that could
exist.
