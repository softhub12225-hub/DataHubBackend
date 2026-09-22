# Collecting with a model instead of a crawler

`apps/api/scripts/ai_collect.py` reads the registered official-source URLs with the
OpenAI API and stages what the model found. It replaces the crawler for this purpose:
no cycle, no queue, no lease, no cron, no snapshot — one HTTP GET per page, inside the
command, and the bytes live only long enough for the model to read them and for the
quote check to run.

```
uv run python apps/api/scripts/ai_collect.py preflight
uv run python apps/api/scripts/ai_collect.py run --limit 5
uv run python apps/api/scripts/ai_collect.py run --i-understand-cost
uv run python apps/api/scripts/ai_collect.py report
```

`OPENAI_API_KEY` must be set; `OPENAI_MODEL` defaults to `gpt-4o-mini`. `preflight`
makes no API call and writes nothing.

## Where the data lands, and why not where you would expect

Not in `field_claim_candidate`. The database will not take it, and not as a policy
check: a candidate needs an `extraction_id`, an `extraction` needs a `snapshot_id`, a
`snapshot` needs a `fetch_run`. Nothing can be stored there that did not come from a
page the acquisition pipeline fetched and hashed.

It lands in `pilot_collected_fact`, the manual-collection staging plane — the table a
returned collection workbook lands in. That is not a workaround. The plane exists for
facts a collector gathered by reading pages, with no snapshot behind them, which a
reviewer then has to verify. A model reading pages is the same shape of artifact, so
each run is its own `COLLECTION_WORKBOOK` submission with `defines_pilot_scope` false:
a machine-produced workbook sitting beside the human ones, comparable with them and
subject to the same review.

Every row is written `NEEDS_REVIEW`. No `field_claim`, no `field_provenance`, no
`change_proposal`; no `pilot_*` table is reachable from `field_provenance` by any
foreign key, and `app_publisher` holds no privilege on this plane at all. A wrong
reading cannot become a published fact by any route.

## Each run copies the pages it read

`pilot_collected_fact` requires an asserted row to name a `source_ref`, and the ref
resolves *within its own submission* (`asserted_row_cites_a_source_ref`, plus a
composite foreign key). So the run copies the source rows it is about to read into its
own submission, keeping their refs, and every fact cites the page it came from.

The constraint is right and the copy is not ceremony: a collection artifact that
asserts facts about pages it never recorded reading is not reviewable. Both are
asserted adversarially in `tests/integration/test_ai_collection_plane.py`.

Only the physical pages are copied — the rows with no `duplicate_of_source_ref`. Of
the 385 registered responsibility claims, 319 are distinct URLs; fetching a page once
per heading would be 66 needless requests to universities.

## The model is never believed

It is shown the page's text and nothing else: no URL, no institution name, no
category. Every value it returns must be quoted verbatim, and the quote is checked
against the retrieved bytes in `app/domains/claims/llm.py`. A claim that cannot be
found in the page is discarded, not down-weighted — `run` reports how many, and a
rising share is the signal that the extractor is drifting toward invention.

Nothing is converted. A quoted `AUD 45,600 per annum` is stored as that string with
`amount_min` NULL, because reading it as a number is a decision a reconciler makes
deliberately.

Non-HTML responses are skipped and counted rather than parsed: a PDF fed to the HTML
normaliser yields plausible-looking rubbish, and the quote check would happily verify a
claim against it.

## "Not published" is bounded by the page's remit

`OFFICIALLY_NOT_PUBLISHED` means the institution does not publish this — a strong claim
one page cannot establish. "The fees page states no fee" is that finding; "the
admissions page did not mention fees" is a page about something else.

So a model abstention is recorded only from the page registered as the authority for
that category: `LANGUAGE_REQUIREMENTS`, `TUITION_FEES`, `APPLICATION_DEADLINES`,
`ENTRY_REQUIREMENTS`. Of the 319 pages, 125 qualify. Abstentions outside a page's remit
are counted in the run summary and never stored. Quoted values are stored whatever the
page's category, because a fee quoted on an admissions page is still a fee that page
states.

## Cost

One HTTP request and one paid API call per page, spent whether or not anything is
found. `run` refuses more than 20 pages without `--i-understand-cost`, mirroring the
acquisition worker's `--i-understand`. Sample with `--limit` first and read the
unquoted share before paying for all 319.

Three consecutive model failures end the run, with a non-zero exit code and a count of
the pages never attempted. An exhausted balance, an expired key or an outage fails
every call identically; without the guard the run keeps fetching university pages it
can do nothing with, at their expense. The first sample run hit exactly this —
`insufficient_quota` — and fetched five pages for nothing before the guard existed.

## What still has to happen afterwards

Everything that decides truth. A reviewer reads the staged rows against the pages,
and the existing route — source verification, real acquisition, extraction, claims,
proposals — is unchanged. This command shortens the gathering, not the deciding.
