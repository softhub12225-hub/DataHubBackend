Written for: the engineer who will run the pilot import, plus whoever briefs the
client on filling the workbook in.

# Pilot data collection

How the 35 pilot universities get from the spreadsheets the client supplies to
reviewable claims — and what must not happen along the way.

---

## 1. Where things stand

- All **181** institutions from `QS_2027_世界前500_指定地区院校.xlsx` are imported. The
  list spans eight destinations: US 68, GB 48, AU 27, CA 17, NZ 8, HK 7, SG 4, MO 2.
- **The pilot is 35 institutions**, named by the client in
  `university_official_sources.xlsx` and imported in Step 5A. They span six
  destinations — US 13, UK 7, Australia 6, Hong Kong 4, Canada 3, Singapore 2.
- Those 35 carry `pilot_wave = 1`. The other 146 keep `pilot_wave` NULL and remain
  valid future targets; nothing was deleted or demoted (U8, architecture D24/D30).
- The pilot was **not** inferred — not by QS rank, not by destination, not by row
  order. The client's file named it and the importer resolved each name exactly.
- The PRD asked for ≥36 and its framing suggested UK/Hong Kong/Macau. Both turned out
  to be wrong about the final scope, and both are left in the record as written.
- `RANKINGS_ENABLED` is `false`; no `ranking_entry` row is created from any supplied
  workbook (U9).

There are now **two** client files and they do different jobs:

| File | Answers | Creates |
|---|---|---|
| `university_official_sources.xlsx` | which universities, and which pages to fetch | scope + acquisition targets (§12) |
| the pilot collection template | what those pages say | staged facts awaiting reconciliation (§8) |

Built: the **export**, the **validator**, the **staging importer** (§8) and the
**source verification queue** (§10). Deliberately not built: anything that fetches a
page. Nothing in this system has made an outbound request, and importing a workbook
does not start one.

---

## 2. Producing the workbook

```bash
make export-pilot-template o=out/pilot.xlsx dest="GB HK MO" n=35
```

Every candidate in the named destinations is exported with `selected` **blank**.
`n=35` is written into the README as a note to the client and passed to the validator
later; it never picks anything. There is a regression test for exactly that.

`dest` is a convenience filter, not a scope rule. The client's actual pilot spans six
destinations, so exporting with the old `"GB HK MO"` default would omit most of it.

Sheets: `README`, `Pilot_Universities`, `Official_Sources`, `Programs`, `Admissions`,
`Language_Requirements`, `Tuition`, `Deadlines`, plus a hidden `_Lists` sheet holding
the dropdown vocabularies.

`Pilot_Universities` is pre-filled, one row per institution, ordered by QS rank
because that matches how the client reads their own list — presentation only. Every
other sheet is a header row the client adds as many rows to as the facts require, and
no collection sheet ships with example data.

---

## 3. Checking a returned workbook

```bash
make validate-pilot-workbook f=returned.xlsx n=35
```

Reads the file, **writes nothing**, and reports every problem with a sheet and row
number. Exit 0 when there are no errors; warnings alone still exit 0.

| Code | Severity | Meaning |
|---|---|---|
| `SELECTED_COUNT_MISMATCH` | error | not exactly `n` institutions marked — in either direction |
| `SELECTED_VALUE_UNRECOGNISED` | error | `selected` is not YES/NO/UNDECIDED (`Y`, `是`, `x`) |
| `SELECTED_WITHOUT_DETAILS` | error | marked YES but no official name or homepage |
| `TARGET_UNRESOLVED` | error | column A is blank, malformed, or names an institution we do not have |
| `PROGRAM_REF_DUPLICATE` / `SOURCE_REF_DUPLICATE` | error | a code defined twice |
| `PROGRAM_REF_UNKNOWN` / `SOURCE_REF_UNKNOWN` | error | a code referenced but never defined |
| `PROGRAM_REF_INSTITUTION_MISMATCH` / `SOURCE_REF_INSTITUTION_MISMATCH` | error | the code exists, but for a different institution |
| `*_REF_MALFORMED` | error | not `P0001` / `S0001` shape |
| `SOURCE_REF_MISSING` | error | a high-risk row asserts something and cites no page |
| `PUBLISHED_WITHOUT_VALUE` / `VALUE_WITH_UNPUBLISHED_STATUS` | error | status and value contradict each other |
| `TUITION_AMOUNT_KIND_UNKNOWN` | error | not one of the five shapes — usually `OFFICIALLY_NOT_PUBLISHED`, which is a *status* |
| `TUITION_AMOUNT_SHAPE_INCONSISTENT` | error | `EXACT` with two different figures, a `RANGE` with one end, a `VARIABLE` with a number |
| `TUITION_AMOUNT_NOT_A_NUMBER` | error | `GBP 28,000 per year` typed into an amount cell |
| `ACADEMIC_YEAR_MALFORMED` | error | not `2027` or `2027/28` |
| `STUDENT_CATEGORY_NOT_IN_DESTINATION` | error | `HOME` on a Hong Kong institution |
| `DEADLINE_PARTS_INCONSISTENT` | error | a time without a day, a timezone without a time, `month_part` with a day |
| `DATA_FOR_UNSELECTED_INSTITUTION` | warning | kept, but not publishable |
| `SCOPE_MAPPING_REQUIRED` | warning | a scope a human must map |

The last group of errors exist because those are database CHECKs the client cannot
see. Without them the workbook validates clean and then throws constraint violations
partway through a batch insert, long after the pages have been closed.

---

## 4. The three identifiers

**`target_institution_id`** (column A, every sheet) is the UUID of our
`target_institution` row. Frozen, grey-filled, "do not edit".

**`program_ref`** (`P0001`) is defined on `Programs` and referenced by the four fact
sheets. **`source_ref`** (`S0001`) is defined on `Official_Sources` and referenced the
same way. Both are **workbook-local collection identifiers** — they live in one file,
mean nothing outside it, and are never `program.id` or `source.id`.

Nothing joins on programme-name text, and that is the whole point. Two programmes at
one university are routinely called the same thing (full-time and part-time), a name
gets retyped with a different dash on the next sheet, and Excel's autocomplete edits
it for you.

`source_url` remains on the fact sheets for readability only. The link is `source_ref`;
a pasted address is never matched, because matching addresses is the guess this design
refuses.

### What happens when an identifier is missing

`domains/onboarding/pilot_matching.py` is the contract. Only `RESOLVED` imports
without a human:

| Outcome | When |
|---|---|
| `RESOLVED` | a valid id naming a row we have |
| `MISSING_ID` | the cell is blank; a name-derived *suggestion* may be attached |
| `MALFORMED_ID` | something is there but is not a UUID |
| `UNKNOWN_ID` | a valid UUID we do not have — usually a workbook from another environment |
| `AMBIGUOUS_NAME` | no id, and the name could be several institutions |

**There is no fuzzy fallback, and it is a deliberate trade.** A similarity threshold
that joins `Essex, University of` to `University of Essex` also joins `University of
Canterbury` to `Canterbury Christ Church University`, and the result is one
university's official fee published under another's name. That failure is silent and
permanent; an unresolved row is a worklist item. The supported way to make future
imports match more often is `entity_alias` — once a human records an alias it is an
exact match against a stored fact.

---

## 5. Applicant scope: never silently universal

`admission_requirement.applicant_scope_id` is NOT NULL and `UNIVERSAL` is the only
seeded scope, so the path of least resistance is to map everything to "all
applicants". Applied to *"IELTS 7.0, or 6.5 for holders of a Chinese bachelor degree
from a 985/211 institution"*, that publishes a requirement on everybody the university
never stated.

So the rule is asymmetric: **only the literal `UNIVERSAL` resolves.** Every other
value — including a blank — parks as `SCOPE_MAPPING_REQUIRED`. A blank does not mean
"everyone"; it means the collector did not say.

Each fact sheet collects `applicant_scope_hint`, `applicant_country_code` and
`qualification_hint` alongside the mandatory `official_text`. They are **hints**, not
taxonomy mappings: a human maps them against `applicant_scope`,
`applicant_scope_criterion` and `qualification_group` afterwards.

Parked rows are a warning, not an error — the file is importable, the row is held.

---

## 6. The one thing to tell the client

> Leave it blank if you have not checked. If you *did* check and the university
> publishes nothing, that is different and more useful — set the row's status column
> to `OFFICIALLY_NOT_PUBLISHED`, leave the value blank, and still give the
> `source_ref` for the page you checked.

This is the platform's central distinction (D2), and the workbook is where it is
either captured or lost forever. A blank cell means `NOT_CHECKED`, so a collector who
runs out of time misrepresents nothing.

Ask them not to write `0`, `N/A`, `TBC` or `-` to mean unknown — a `0` fee is a
published fact and the difference is unrecoverable afterwards.

Two Excel warnings are in the README because they cost real data:

- **Do not drag the fill handle.** Excel silently counts `P0003` up to `P0004`,
  `P0005`. The duplicate and mismatch checks catch some of it, not all.
- **Write dates as text.** A cell that reformats itself to `45678` has lost the
  wording.

### Dates

Verbatim wording first, then only the parts the page gives:

```
'15 January 2027'        → year 2027, month 1, day 15
'mid March 2027'         → year 2027, month 3, month_part MID, day BLANK
'January 2027'           → year 2027, month 1, day BLANK
'15 Jan 2027, 23:59 GMT' → also time_of_day 23:59, timezone GMT
```

Never add a time or timezone the page does not state (D17, C9, C13). The validator
enforces this: a time without a day, or a timezone without a time, is an error.
`NO_FIXED_DEADLINE` (the page says there is no deadline) is a *published* fact and is
not the same as `OFFICIALLY_NOT_PUBLISHED` (the page says nothing about deadlines).

---

## 7. What the adversarial review found, and where it landed

Seven problems surfaced that were **not** workbook problems. Five are now closed;
two remain, and neither blocks collection.

| # | Problem | Resolution |
|---|---|---|
| 1 | **Nowhere for the collected facts to land.** `field_claim` needs an extraction of a snapshot of a fetched page, which a typed spreadsheet cannot produce. | **Closed (U12).** Its own staging plane — §8. The rejected alternative was registering each pasted wording as a `MANUAL` snapshot, which would have made a person's retyping load-bearing evidence. |
| 2 | **`university.name_en` is governed (C27)** and the sheet collects a name with no source and no status. | **Closed.** The import writes **no** `university` row. The official name stays in `pilot_selected_university` until identity resolution, which `target_institution.matched_university_id` / `matched_at` / `matched_by` already require to be a recorded human act. |
| 3 | **A second workbook re-creates every programme**, because `program_ref` is workbook-local and `program` has no natural key. | **Half closed.** `pilot_collected_program.reconciled_program_id` is where a reconciler records the link, and it is never set by the importer. Pre-filling a grey `program_id` column on re-export is still to do, and is only needed once a real `program` row exists. |
| 4 | **A fee range could not be represented.** | **Closed (U14).** §9. |
| 5 | **~200 URLs need human verification** and nothing scheduled that pass. | **Closed (U15).** §10. |
| 6 | **Two entry routes for one programme collide** on `uq_admission_requirement_scope_kind`. | **Open.** The README still says to use two rows. Either put alternatives in one row's `official_text`, or add a `route_label` to the sheet and to the key. Decide before the first reconciliation, not before collection. |
| 7 | **`campus_name` and `faculty_or_school` are free text**, so an importer could only join them by name — the failure `program_ref` exists to prevent, one level down. | **Open, and deliberately so for the pilot.** Both are staged verbatim on `pilot_collected_program` and neither is joined on. Give them their own refs if the pilot shows they matter. |

---

## 8. The staging plane (U12)

```bash
make import-pilot-workbook f=returned.xlsx n=35        # add dry=1 to roll back
```

The workbook lands in five tables and stops there:

| Table | Grain | Mutability |
|---|---|---|
| `pilot_submission` | one returned file | status only |
| `pilot_selected_university` | one institution row on `Pilot_Universities` | append-only |
| `pilot_collected_program` | one `program_ref` | append-only |
| `pilot_collected_source` | one `source_ref` | verification decision only |
| `pilot_collected_fact` | one row on a fact sheet | append-only |

**What the import does not create:** no `field_claim`, no `field_provenance`, no
`university`, no `program`, no `source`, no `source_mapping`, no published fact. A
test asserts all thirteen of those tables are still empty afterwards.

**Why the separation is real and not just a naming convention.** Three independent
things hold it:

1. No foreign key path leads from `field_provenance` into any `pilot_*` table. A test
   walks the whole FK graph to prove it, so a future migration that adds a convenient
   link fails the build rather than the review.
2. A `field_claim` still requires an extraction of a snapshot of a fetched source.
   There is no staging column that could substitute for one.
3. `app_publisher` — the only role that writes canonical data — holds no privilege
   here at all, not even `SELECT`. That is a mitigation rather than the enforcement:
   C27 is precisely the lesson that grants cannot express "a spreadsheet value must
   not become a published fact".

**Re-import.** `file_sha256` is unique. The same file twice writes nothing. A
corrected file is a **new** submission; the previous one becomes `SUPERSEDED` and
every one of its rows stays exactly as it was, because the reason to keep both is to
compare them and a comparison against an edited row compares nothing.

**Validation first.** An importable workbook is one with **no errors**. Warnings do
not block: a parked applicant scope and a row for an unselected institution are both
things worth keeping. A partial import would be worse than a refusal — it leaves a
submission that looks complete, and the missing rows are exactly the ones nobody
knows are missing.

**Still no fetching.** Every URL is validated by `validate_source_url` (http/https
only, no IP literals, no credentials — D23) and stored. None is dereferenced. A URL
that fails validation is reported and its row skipped; the other 199 still import.

---

## 9. Fees that are not a single number (U14)

`tuition.amount` was one `numeric` column tied to its status by a biconditional. That
shape cannot hold what universities actually publish:

```
"GBP 28,000-32,000 depending on pathway"      a published fee, no scalar
"from GBP 24,500"                             a published floor
"fees vary by module selection"               published, and not a number at all
```

Each way of forcing those into one column lies: pick an endpoint and an invented
figure is published as exact; mark `OFFICIALLY_NOT_PUBLISHED` and we deny a page that
plainly publishes; leave `NOT_CHECKED` and we discard work that was done.

So the shape is recorded:

| `amount_kind` | `amount_min` | `amount_max` | the page said |
|---|---|---|---|
| `EXACT` | required | required, equal | one figure |
| `RANGE` | required | required, ≥ min | two figures |
| `FROM` | required | blank | a floor |
| `UP_TO` | blank | required | a ceiling |
| `VARIABLE` | blank | blank | fees vary; `official_text` mandatory |

**No midpoint is ever derived.** A fee of 28,000–32,000 is not 30,000, and a
consultant quoting 30,000 to a family would be quoting a figure no university
published. If a product surface wants a single number it must show its working;
nothing stores one.

**`amount_kind` is not a status.** `OFFICIALLY_NOT_PUBLISHED` means the page says
nothing about fees. `VARIABLE` means the opposite — it does address fees, just not
numerically. Writing the status into the kind column is the likeliest mistake, so the
validator names the right column in its message.

The `Tuition` sheet collects `amount_kind` / `amount_min` / `amount_max` /
`currency_code` / `billing_unit_code` / `official_text` / `amount_status`. For
`EXACT`, the same figure goes in both amount columns.

---

## 10. The source verification queue (U15)

```bash
make source-verification-report              # counts, three groupings
make source-verification-report list=1       # the individual rows still open
```

The client's source list carries 385 claimed responsibilities over 319 distinct
pages for 35 institutions. Every one starts `PENDING`, and **no URL is ever
auto-classified as official.** The
promotion path is four separate decisions:

```
collected candidate → official_domain verified → source_mapping promoted
                    → source earns its eligibility class
```

`pilot/verification.py` has one function per decision — `verify_candidate`,
`reject_candidate`, `flag_candidate_for_review`, `register_verified_candidate` — and
each requires an actor and a reason, records them on the row, and appends to the
audit hash chain last (C18 lock order). A rejected candidate is kept with its reason,
for the same purpose a rejected `official_domain` is kept: the rejection is what stops
the same wrong page being proposed again.

`VERIFIED` here means *worth registering*, not *official*. Registration creates a
`CANDIDATE` `source_mapping` and no `source` at all, so nothing has become
publication-eligible; C27's `source_eligibility_is_earned` still has to be satisfied
later, deliberately.

**The hostname column is evidence.** The queue shows whether a candidate's host falls
under a domain already verified *for that institution* — the most useful single fact
to put in front of a reviewer, and the one it would be most tempting to act on
automatically. It is not acted on automatically. A university's own domain also hosts
news articles, student societies and personal staff pages; a hostname says where a
page lives, not what it is. There is no bulk-verify function, and its absence is the
feature.

No domain belonging to another institution is ever consulted, and no host is matched
because it resembles an institution's name — the same refusal `pilot_matching.py`
makes for names, one level down.

There is no frontend. The report groups by institution (most work left first), source
type and degree level, with totals for pending / verified / rejected / needs-review.

---

## 11. The final source list (Step 5A)

```bash
make import-official-sources f=university_official_sources.xlsx dry=1   # inspect
make import-official-sources f=university_official_sources.xlsx by=<app_user uuid>
make source-verification-report list=1
make source-verification-report pages=1     # the distinct pages, for acquisition
```

### What the file is

35 institutions, one row each, eleven URL columns and a note. It answers *which pages
should we eventually fetch for each pilot university* and is authoritative for that
and nothing else. It carries no programme, no fee, no deadline, no test score, and the
importer creates none.

| Column | Becomes |
|---|---|
| Official Homepage | `UNIVERSITY_HOME` |
| Undergraduate Admissions URL | `UNDERGRADUATE_ADMISSIONS` (scope `UNDERGRADUATE`) |
| Postgraduate Admissions URL | `POSTGRADUATE_ADMISSIONS` (scope `TAUGHT_POSTGRADUATE`) |
| PhD Admissions URL | `PHD_ADMISSIONS` (scope `RESEARCH_POSTGRADUATE`) |
| Program/Course Catalogue URL | `PROGRAM_CATALOG` |
| Entry Requirements URL | `ENTRY_REQUIREMENTS` |
| English Language Requirements URL | `LANGUAGE_REQUIREMENTS` |
| Tuition/Fees URL | `TUITION_FEES` |
| Application Deadlines URL | `APPLICATION_DEADLINES` |
| Academic Calendar URL | `ACADEMIC_CALENDAR` |
| Important Additional Source URL | **`UNCLASSIFIED`** — optional, and nothing guesses |

### What the real file contained

| | |
|---|---|
| institutions | 35 (resolved 35, ambiguous 0, unknown 0) |
| core URL cells | 350 (10 columns × 35, all populated) |
| additional URL cells | 35 (optional) |
| **claimed responsibilities** | **385** |
| **distinct pages** | **319** |
| repeats of a listed page | 66 |
| additional URLs that were genuinely new | 3 |
| URLs rejected by URL safety | 0 |
| URLs shared between universities | 0 |

The README's "455 populated / 455 expected" counted columns A:M, which includes the
name, the region and the notes. The correct figure is 385. It is documentation about a
spreadsheet, so the import records the right number and moves past it — and 385
populated cells is explicitly *not* a database invariant, because the additional
column is optional.

### Matching: exact, or nothing imports

Each name is folded by `normalize_institution_name` (case, Unicode form, whitespace,
decorative punctuation — nothing else) and must hit exactly one `target_institution`.
The country column is then cross-checked against the destination already on record,
and a disagreement is an error rather than a tie-break.

There is no similarity threshold, and a test tokenises the importer to keep it that
way. One loose enough to join `Imperial College` to `Imperial College London` also
joins `University of Canterbury` to `Canterbury Christ Church University` — and that
mistake publishes one university's fees under another's name, silently.

A single unresolved row aborts the whole import. Importing the 34 that matched would
leave a pilot that looks complete and is quietly missing a university.

### One page, several responsibilities

This is the part worth understanding before reading any count.

A `pilot_collected_source` row is **one claimed responsibility** — this URL, for this
category. Imperial's `/study/apply/` is both the application-deadlines page and the
postgraduate-admissions page, so it is two rows. `duplicate_of_source_ref` names the
row that first registered the URL, so the rows with NULL there are the **distinct
pages**, and a partial unique index allows exactly one per URL per institution.

```
385 claimed responsibilities   <- what a reviewer works through
319 distinct pages             <- what acquisition will fetch, once each
 66 repeats                    <- kept, not discarded
```

The old `UNIQUE (submission_id, url_sha256)` said one URL = one source. The real file
disproves that 66 times, so it was replaced (architecture C29). Deduplicating by URL
would have discarded the claim that the deadlines page is also the admissions page —
which is exactly the fact the review layer needs.

### The additional-source column

Optional, and it carries no category. 32 of the 35 repeat a core URL: those rows are
kept for lineage, marked as repeats, and are not a second thing to fetch. Three are
genuinely new, and they stay `UNCLASSIFIED` until a person says what they are.

`UNCLASSIFIED` is not a `SourceCategory`, so nothing canonical can ever carry it. A
CHECK lets such a page be **rejected** — "not a page we want" needs no category — but
never **verified**, because verification asserts a page is the authority for
something. `classify_candidate` supplies the category, with an actor and a reason, and
lands in the audit chain like every other decision.

One of the three is a Stanford fee-rates page. Labelling it `OFFICIAL_PDF` because it
arrived in the extra column would have been wrong twice over.

### What the import does not do

No `university`, `program`, `source`, `source_mapping`, `field_claim`,
`field_provenance`, `snapshot` or published fact — asserted by a test across sixteen
tables. **Nothing is fetched**, and running the import starts nothing that fetches.

It writes no `official_name_en` either. The workbook's University Name column holds
"exact original QS target-list labels" (its own README says so), and `university.name_en`
is governed by C27 — putting a QS string in a column meaning "what the institution calls
itself" is the first step of exactly the laundering C27 exists to stop.

### URL handling

Every URL passes `validate_source_url`: http/https only, no IP literals, no
credentials, no local names (D23). Normalisation is conservative — scheme and host
lowercased, default port dropped, fragment removed — and it changed **nothing** in the
supplied file. Both the raw and the normalized form are stored, because debugging a
fetch that went somewhere unexpected needs both.

A rejected URL is reported and its row skipped; the other 384 still import. One bad
address should not cost the rest, and the rejection is visible either way.

**This is a storage floor, not the SSRF defence.** The acquisition layer must still
resolve the name, refuse non-public addresses, pin the resolved address and re-check
every redirect. A URL arriving in a human-maintained spreadsheet earns no exemption.

---

## 12. Reference data

Seeded and ready:

- **Disciplines** (revision 0018, top level only): `BUSINESS`, `COMPUTER_AND_DATA`,
  `ENGINEERING`. Computer Science and Data are one code on purpose — universities
  disagree where the boundary sits, and splitting it would force a judgement the
  sources do not support. `discipline_hint` preserves the real subject verbatim
  ("Artificial Intelligence", "Business Analytics", "Mechanical Engineering") for a
  later mapping pass informed by actual data.
- **Degree levels**: `BACHELOR`, `MASTER`, `DOCTORATE`.
- **Student categories**: `HOME`/`INTERNATIONAL` (GB), `LOCAL`/`NON_LOCAL` (HK, MO) —
  validated against the institution's destination.
- **Round types**: eleven codes including `INSTITUTION_DEFINED`.
- Currencies, billing units, test types, intake seasons: as seeded in revision 0014.

Not seeded: `applicant_scope` holds only `UNIVERSAL`, which is why §5 exists.
