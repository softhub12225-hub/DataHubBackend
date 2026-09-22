Written for: the engineer who will run the first acquisition cycle, and whoever
reviews the sources afterwards.

# Safe acquisition and evidence capture

How the pilot's 319 pages get fetched, what is preserved, and what fetching them
deliberately does *not* earn.

---

## 1. The correction this step made

Step 5A coupled acquisition to trust. A `source_mapping` requires a verified
`official_domain`, registration went through a mapping, and **zero domains were
verified** — so nothing could be fetched until the whole review pass finished.

That is backwards. Fetching a page is how you find out what it is. Requiring the answer
first means a reviewer judges a URL they cannot see through our own record, so they
open it in a browser instead and the system learns nothing.

The two questions are now separate:

| | Question | Who answers | Column |
|---|---|---|---|
| **Fetchable** | may a worker send a request? | a machine: syntax, scheme, SSRF, active, not blocked | `source.fetch_eligibility` |
| **Publication eligible** | may a fact from this source be published? | a person: domain verified, mapping promoted, responsibility verified | `source.publication_eligibility` (C27) |

Every source in the pilot is **`FETCHABLE` + `NOT_ELIGIBLE`**. We may look; nothing we
see may be published.

**C27 is unchanged.** `publication_eligibility` still defaults closed,
`source_eligibility_is_earned` still refuses a class no promoted mapping vouches for,
and the triggers on `field_claim` and `field_provenance` still refuse evidence whose
class does not permit the fact. No schema change was needed to allow a `NOT_ELIGIBLE`
source to exist — C27 always permitted that. What changed is the code path that creates
sources, plus a lineage link that does not run through `source_mapping`.

> **A source existing is not a trust signal.** It means "this URL is a known
> acquisition target", and `registered_by` says who said so.

---

## 2. The acquisition model

```
pilot_collected_source (385 claims)
        │  acquisition_source_id
        ▼
    source (319)  ──▶  fetch_attempt  ──▶  fetch_run  ──▶  snapshot  ──▶  content_blob
   NOT_ELIGIBLE        mutable, leased     immutable      immutable      content-addressed
```

`acquisition_target` is the view the scheduler reads: one row per **physical page**,
carrying `submission_id`, `target_institution_id`, `match_key`, the raw workbook URL,
the normalized URL, the physical `source_ref`, and every claim riding on it.

Lineage runs through `pilot_collected_source.acquisition_source_id` rather than
`source_mapping`, because a mapping needs a verified host and nothing is verified yet.
When domains are verified, mappings are created then — and *they* are what confers
eligibility.

```bash
make acquisition-register dry=1     # inspect
make acquisition-register by=<app_user uuid>
```

For the client's file: **319 sources created, 385 claims linked, 120 hosts, 1 DOCUMENT
strategy (the Caltech PDF), 3 unclassified, 0 refused, all NOT_ELIGIBLE.**

---

## 3. One page, fetched once

385 claims cover 319 URLs. A page claimed for postgraduate admissions, entry
requirements *and* deadlines is **one fetch**, and all three claims read the same
observation. Scheduling is over `acquisition_target`, which is one row per URL, so
duplicate claims cannot cause duplicate requests.

```bash
make acquisition-enqueue pilot=1 dry=1
make acquisition-enqueue university=<target_id>
make acquisition-enqueue source=<source_id>
```

The dry run reports: physical pages selected, hosts, duplicate responsibilities that
needed no second fetch, already queued, blocked or disabled, new attempts.

`cycle_key` (`2027-01-15T06`) groups retries of one logical check and makes enqueueing
idempotent — a second `enqueue` for the same cycle is a no-op, which matters because
that is exactly the command an operator runs twice.

---

## 4. Lease fencing

The failure this exists to prevent is silent. Worker A stalls mid-fetch past its lease;
the sweeper marks the attempt `ABANDONED`; worker B claims a new one; A wakes up and
finalises — writing an authoritative `fetch_run` and a `snapshot` for work it no longer
owns. Timestamps cannot prevent it, because the clock disagreement *is* the problem.

`fetch_attempt.lease_token` is the fence: a fresh UUID per claim, and every heartbeat
and finalisation is conditional on it.

```sql
UPDATE fetch_attempt SET ... WHERE id = :id AND state = 'RUNNING' AND lease_token = :token
```

Zero rows updated → `LeaseLostError`. The fenced update is the **first write of the
finalisation transaction**, so a lost lease means no run, no snapshot and no blob row
are written at all.

Exactly one terminal run per attempt has two independent guarantees: the fenced
transition (whoever loses never reaches the insert) and `uq_fetch_run_attempt_id`.

Retries are new attempts with the next `attempt_no`, never a state reset — so three
tries appear as three runs rather than one that eventually worked. Three per cycle,
then it becomes a worklist item.

---

## 5. SSRF, and the limit we are not hiding

`onboarding/urls.py` validates a URL when it is **stored**: scheme, authority shape,
port, no IP literal, no credentials. It performs no I/O, because a DNS answer at
registration time says nothing about the answer at fetch time.

`acquisition/netsafety.py` is the **real defence**, at fetch time:

1. Shape check — http/https only, no credentials, no IP literal, normal port. Numeric
   spellings are resolved before classification, so `127.1`, `0177.0.0.1`,
   `2130706433` and `0x7f.1` are all refused (the Step 4 fixes, preserved).
2. Resolve, and refuse unless **every** A/AAAA answer is public. Validating only the
   first is the bug.
3. `is_global` is the primary predicate, not `is_private` — `100.64.0.0/10` (carrier
   NAT) is not "private" to Python's `ipaddress` and is not routable either.
4. IPv4-mapped, 6to4 and Teredo IPv6 addresses are unwrapped and the inner address
   classified, so `::ffff:127.0.0.1` is refused as loopback.
5. Cloud metadata named explicitly — `169.254.169.254`, `169.254.170.2`,
   `100.100.100.200` (Alibaba), `192.0.0.192` (Oracle), `fd00:ec2::254` — plus
   `metadata.google.internal` and friends by name.
6. **Pin the connection** to a validated address. `PinnedTransport` rewrites the
   connection target and restores `Host` and TLS SNI, so the socket goes where we
   checked and certificate verification still runs against the real name.
7. **Re-run all of it on every redirect hop.** Redirects are followed by hand;
   `follow_redirects=True` would have httpx resolve and connect itself, skipping the
   check that matters.

### The DNS rebinding limit, stated

Pinning closes the gap between *our* resolution and *our* connection. It does **not**
defend against a name whose legitimate answer is itself hostile, nor against an
attacker who returns a public address they control and which proxies inward. Those are
"the host is malicious", which no client-side check detects.

Second limit: if the platform resolver is bypassed — a `hosts` entry, a hostile local
resolver — `getaddrinfo` returns what it is told and we validate what we are given.
Controlling the resolver is part of deploying this system.

Nothing is relaxed because a URL came from the client's workbook. A spreadsheet is not
a trust boundary.

---

## 6. Politeness

120 hosts, 319 pages, and one host serves eleven of them. `HostGate` allows **one**
in-flight request per host with a configurable floor between them; global concurrency
is bounded separately, so throughput comes from working many hosts rather than any host
harder.

`429` and `Retry-After` are obeyed as stated — the server's number always wins over
ours. A source that refuses becomes `BLOCKED`, stops being scheduled, and records what
the site actually did.

**There is no proxy rotation, no user-agent cycling and no CAPTCHA handling anywhere in
this package.** Those exist to defeat a decision the site has made, and a system whose
claim is "we only use what the university published" cannot also be one that works
around the university saying no.

```bash
make acquisition-worker pages=5            # contacts real sites; prints every URL
make acquisition-worker pages=50 confirm=1 # more than 20 needs confirm
```

### Which identity each command runs as

The fetch plane — `fetch_attempt`, `fetch_run`, `snapshot`, `content_blob` — is
writable **only by `app_worker`**; `app_api` may read it and never write it. The CLI
picks the identity the command needs rather than running everything as one role, and
`--role` overrides it:

| Command | Role | Because |
|---|---|---|
| `register` | `app_api` | writes `source`, which is onboarding-side work |
| `enqueue` | `app_worker` | writes `fetch_attempt` |
| `worker` | `app_worker` | writes runs, snapshots and blobs |
| `report`, `assist` | `app_api` | read-only |

---

## 7. What is preserved

| Table | Holds | Dedup |
|---|---|---|
| `fetch_run` | one per completed attempt, abandonments included | never |
| `snapshot` | one per observation that carried bytes | never |
| `content_blob` | one per distinct sha256 | **here and only here** |

Object storage first, then one transaction. *Object written, transaction failed* leaves
an orphan: wasted bytes, reconcilable, harmless. *Transaction committed, object missing*
leaves a published fact whose evidence cannot be produced — which is the outcome the
whole system exists to prevent. So the recoverable failure is the one we allow.

`find_orphans` takes a 24-hour age floor, because an object is written seconds before
its row. Deletion is an operator's decision; there is no automatic GC, since an
over-eager sweeper deletes evidence nobody notices is gone until someone asks for it.
`find_missing_objects` audits the direction that matters.

### 304 is not a 200 that matched

| | `fetch_run` | `snapshot` |
|---|---|---|
| **200, identical bytes** | `OK`, http 200, bytes counted | **yes**, sharing the existing blob |
| **304 Not Modified** | `UNCHANGED`, http 304, `unchanged_content_hash` | **no** |

A snapshot means "we saw these bytes". A 304 saw none, so it gets none — and a CHECK
stops anything but a 304 claiming unchanged content.

---

## 8. PDFs and HTML

A PDF is streamed, size-limited, hashed and stored. **No OCR, no table parsing, no
claims** — one of the 319 is a direct PDF and it is preserved as bytes.

For HTML the raw response is stored, plus two operational values: `<title>` and the
declared charset, bounded to the first 64 KiB. They exist so a reviewer looking at 319
URLs has something beyond the address. **No programme name, fee, deadline, requirement
or test score is read from any page.**

---

## 9. Source health

`source_health` is a **view** over `fetch_run` and `snapshot`, so it cannot drift from
the history it summarises and there is no second write to forget.

`HEALTHY` · `DEGRADED` (1–2 consecutive failures) · `FAILING` (3+) · `BLOCKED` ·
`DISABLED` · `STALE` (last success over 30 days ago) · `NEVER_FETCHED`.

```bash
make acquisition-report
make acquisition-report by=institution
```

---

## 10. Verification assistance

After a fetch, a reviewer can see the HTTP status, content type, page title, redirect
chain, and whether the final host differs from the one we asked for.

**None of that decides anything.** `reporting.py` has no write path: nothing verifies,
rejects, classifies or promotes. A university page redirecting to an external
application portal is recorded and surfaced; that host is not made official by being
observed.

The temptation refused here is specific: with a title and a status it would be easy to
write *"if the title contains 'Tuition' then classify it as `TUITION_FEES`"*. That is
guessing from a string, which is how one university's fees end up published under
another's name.

```bash
make acquisition-assist            # everything fetched
make acquisition-assist moved=1    # only pages that redirected off-host
```

---

## 11. What acquisition does not do

No `field_claim`, no `extraction`, no `change_proposal`, no `field_provenance`, no
canonical row of any kind — asserted by test across nine tables after a full cycle.

We can say: *we safely fetched and preserved official-source candidate pages.*
We cannot say: *we extracted or published university facts.*

---

## 12. Open gates

- **Docker.** MinIO and the Compose stack have never been executed; there is no Docker
  on the development machine. `S3EvidenceStore` is written to the same contract as
  `FilesystemEvidenceStore` and exercised through that contract, which is not the same
  as having run it. `make verify-docker` remains the release gate, and the worker
  defaults to the filesystem store so it can run locally at all. The real-web run in §13
  used the **filesystem** store; nothing about S3 was validated by it.
- **Browser fetching.** `BrowserFetcher` is an interface and nothing more. The evidence
  gathered in §13 does not justify building it: every page that returned a body returned
  substantial static text. Introducing Playwright before that changes would add 400 MB
  and a second failure mode for nothing.
- **The PRD schedule.** 06:00 / 12:00 / 18:00 is not switched on. `beat_schedule` is
  empty and cycles are enqueued by hand, because putting 319 pages in front of 120
  universities three times a day before anyone has reviewed a single result is not a
  schedule, it is a load test someone else pays for.

---

## 13. What the first real-web run found (Step 5B.1)

Everything above had been tested against a fixture server. A fixture agrees with
whatever the test assumed, so twelve real pages were fetched under a checked-in,
reproducible smoke set (`apps/api/smoke/acquisition_smoke_set.toml`, D38) — one page per
institution, all six destinations, every structurally unusual case. Three cycles. It is
not business configuration: nothing reads it at runtime.

```bash
uv run python apps/api/scripts/acquisition_smoke.py list        # what is in the set
uv run python apps/api/scripts/acquisition_smoke.py preflight   # DNS only; sends nothing
uv run python apps/api/scripts/acquisition_smoke.py run --i-understand
uv run python apps/api/scripts/acquisition_smoke.py evidence    # what it produced
uv run python apps/api/scripts/acquisition_smoke.py audit        # every count, per grain
```

**Every number below is `audit` output, not prose.** The first version of this section
was written from terminal scrollback after the database had been purged, and stated two
counts wrongly (see C35). `audit` prints them from SQL, each grain labelled, so the
reconciliation is a command rather than a memory.

`run` refuses to start without `--i-understand`, holds one request in flight per host
with a five-second floor, and has no flag that makes it faster, relaxes TLS or softens
the SSRF guard. A page that cannot be fetched safely is recorded as a failure, because
discovering that is the point.

### What the sites did

| | |
|---|---|
| 12 pages, 30 attempts, 30 runs | 20 OK · 4 UNCHANGED (304) · 3 HTTP_ERROR · 3 BLOCKED · 0 TIMEOUT · 12 redirected · 6 conditional |
| object-store integrity | **0 missing objects, 0 hash or size mismatches** |
| trust state after the run | unchanged: 319 `NOT_ELIGIBLE`, 385 `PENDING` |
| extraction | `field_claim`, `extraction`, `change_proposal`, `field_provenance` and every canonical table still 0 |

**Health over the twelve: `HEALTHY 8 · BLOCKED 3 · FAILING 1`** (the other 307
registered pages are `NEVER_FETCHED`). Oxford and Monash refuse us with 403; NUS answers
200 with a WAF challenge and is blocked by C32; PolyU's tuition URL is a genuine 404, a
finding for source review rather than a retry loop (C34). All of that is **expected site
behaviour or a bad URL** — the defects the run found were our own.

### Four grains, never summed

A page that returns a challenge is *observed* and *not stored*, and a page re-fetched
unchanged adds an observation without adding a body. Merge those and the totals stop
meaning anything, which is precisely how C35 happened. So:

| Grain | Count | What one row is |
|---|---|---|
| **Sources holding evidence** | **8** — 7 `text/html` + 1 `application/pdf` | a physical page, the thing the client's workbook names |
| **Snapshots** | **20** — 19 `text/html` + 1 `application/pdf` | one occasion a body was seen; a re-fetch adds one |
| **Distinct content blobs** | **8** — 7 `text/html` + 1 `application/pdf`, 2,889,707 bytes | one distinct body; an unchanged re-fetch adds none |
| **Observed and discarded** | **6** — 1 challenge (200, 212 wire bytes) + 2 × 403 + 3 × 404 | a response that reached us and was deliberately not stored |

Two traps in that table. A blob records the bare media type (`text/html`) while a
snapshot keeps the response header verbatim (`text/html; charset=UTF-8`), so **the
snapshot and blob grains do not join on `content_type`**. And 20 snapshots over 8 blobs
is not an anomaly — it is deduplication working: every page served byte-identical
content across all three cycles, so cycles 2 and 3 added 12 observations and no bodies.

### The three defects

They are recorded in full as C32, C33 and C34. In short: a 200 can carry a WAF
interstitial and we stored one as evidence; a fetch that redirected and then failed lost
its whole redirect chain; and a permanent 404 was retried like a timeout.

None of the fixes weakens anything. Challenge detection is bounded by size so a page that
merely mentions Cloudflare is not mistaken for a challenge, and detecting a challenge
means **stopping**, not working around it (D6).

### What this says about Step 5C

Reported as technical shape only — no extraction was performed, and no page was read for
its content.

| Observation | Consequence for the parser |
|---|---|
| all 7 stored HTML pages carry 4.0k–15.2k characters of static visible text | a **static HTML parser** is the right first tool |
| **JSON-LD: absent on every page** | there is nothing to gain from a structured-data parser yet |
| tables are rare (1 `<table>` across all 7); lists are everywhere (9–21 per page) | list- and heading-oriented extraction, not table scraping |
| the HKUST flipbook — the page most likely to be a JS shell — returned 15.2k characters of text from 4 script tags | **no browser fetcher is justified** |
| the Caltech PDF is a real PDF (`%PDF-1.6`, 115 KB) | a **PDF text parser** is needed, and it is the one genuinely new capability |
| Caltech serves `application/pdf` for a column the workbook labels `ACADEMIC_CALENDAR` | content type is a property of the response, not of the human's column heading |

There is no eighth HTML body: NUS is blocked before anything is stored, so the set of
stored pages and the set of usable pages are now the same set. That is the property C32
bought — previously they differed by one, and nothing downstream could tell which.

**Three pages land on a host other than the one requested**, all recorded and none
trusted: `admissions.hku.hk` → `portal.hku.hk`, `www.anu.edu.au` → `study.anu.edu.au`,
and `www.calendar.ubc.ca` → `vancouver.calendar.ubc.ca` over four hops. A fourth page
(Toronto) redirects **within** its own host, which is not the same thing and no longer
counted as though it were (C36).

HKU's is the one a reviewer should look at first — the page title comes back as "Home",
which suggests the workbook's taught-postgraduate URL now lands on a portal front page
rather than on the admissions content it is claimed for. That is a source-review
finding; acquisition records it and decides nothing (`make acquisition-assist moved=1`).

### What is still not proven

Twelve pages are not 319, and the sample is deliberately one-per-institution, so it says
nothing about how a host behaves under eleven requests. The full cycle is safe to run —
politeness, fencing, SSRF and storage all held — but it should be run **once, watched,
with the 429/`Retry-After` path observed on a real server**, which no page in this sample
exercised.

---

## 14. Recovery and cycle isolation (Step 5B.2)

Four operational blockers stood between the smoke test and a 319-page run. Three shared
a shape: **a temporary condition was recorded as a permanent one, and nothing could undo
it.**

### The two questions that were one column

| | Question | Who answers | Where it lives |
|---|---|---|---|
| **Eligibility** | may we fetch this **at all**? | a machine, on evidence of refusal; otherwise an audited human | `source.fetch_eligibility` |
| **Cooldown** | may we fetch it **now**? | the clock | `source.cooldown_until` |

A cooldown is deliberately **not** a new `fetch_eligibility` member (D39). Making it one
would force every reader of that column to know that one of its values expires, and
leave the scheduler as the only thing able to say whether a source was really blocked.
Timing state belongs in a timestamp.

`source_health` now answers both separately: `health` describes the record
(`HEALTHY` / `DEGRADED` / `FAILING` / `BLOCKED` / `DISABLED` / `NEEDS_MANUAL_REVIEW` /
`STALE` / `NEVER_FETCHED`) and `schedule_state` is what a scheduler reads
(`FETCHABLE_NOW` / `COOLDOWN` / `BLOCKED` / `DISABLED` / `NEEDS_MANUAL_REVIEW`).

### 429 is a throttle

| What arrives | Status | Consequence |
|---|---|---|
| `429` with `Retry-After: 600` | `RATE_LIMITED` | cooldown of 600s — the site's number wins outright |
| `429` with an HTTP-date `Retry-After` | `RATE_LIMITED` | parsed, and not scheduled before that instant |
| `429` with `Retry-After: soon` or none | `RATE_LIMITED` | 900s, deliberately generous |
| the 4th consecutive `429` | `RATE_LIMITED` | `NEEDS_MANUAL_REVIEW` — a question about our cadence |
| any success | — | cooldown cleared, strike count reset to 0 |

Still never `BLOCKED`, and the retry is always a **new `fetch_attempt`** scheduled no
earlier than the cooldown — never a retry inside the worker loop, which would be asking
again immediately. Strikes count a *streak*, not a lifetime: counting forever would
eventually escalate every source in the pilot, which is a slow way of switching a
working system off.

**A `429` quiets the whole host** (D40). One pilot host serves eleven pages, and
honouring the throttle only on the page that received it is not honouring it.
`host_cooldown` is one row per hostname, consulted by both the enqueue and the claim, and
persisted because `HostGate` is empty in the next process.

### A resolver that did not answer is not a hostile target

`UnsafeTargetError` used to cover all of it. Now:

| Condition | Error | Status | Consequence |
|---|---|---|---|
| non-global address, IP literal, metadata endpoint | `UnsafeTargetError` | `BLOCKED` | permanent, **never** retried automatically |
| resolver timeout, `SERVFAIL`, `EAI_AGAIN` | `DnsTemporaryError` | `DNS_TEMPORARY` | bounded cooldown, retried later |
| `NXDOMAIN`, a name with no addresses | `NameNotResolvedError` | `NAME_NOT_RESOLVED` | `NEEDS_MANUAL_REVIEW`; no retry invents a hostname |

An unknown resolver code is treated as **temporary**, deliberately: wrong in that
direction costs one retry, and wrong the other way parks a working page in a review
queue waiting for a human who has no reason to look. Both platforms' code families are
pinned numerically, because Windows has no `socket.EAI_AGAIN` and glibc has no
`WSATRY_AGAIN` — so one test suite can assert one mapping.

A failed resolution **never** falls through to a connection, asserted against the
fixture server's own request log.

### The way back

There was none. Registration is `ON CONFLICT DO NOTHING` by design, so re-importing the
workbook could not clear a state either, and recovery meant hand-written SQL.

```bash
make acquisition-source-status source=<id>
make acquisition-source-reenable source=<id> actor=<id> reason="..."
make acquisition-source-disable source=<id> actor=<id> reason="..."
make acquisition-source-review  source=<id> actor=<id> reason="..."
make acquisition-clear-cooldown source=<id> actor=<id> reason="..." [host=1]
```

Each needs an actor and a reason of at least eight characters, and each appends to the
audit chain — they are a person overriding what the system concluded. A cooldown
expiring is **not** audited: it is the clock, not a decision, and recording thousands of
them would bury the handful that are.

> **Re-enabling means "a worker may request this URL again". It does not mean the source
> is trusted**, and it cannot be made to: `publication_eligibility` is never written
> here, so C27 is untouched and a re-enabled source is exactly as publication-ineligible
> as it was a moment earlier. The `source-status` output prints that line every time,
> because "re-enabled" invites the other reading.

Re-enabling bypasses nothing. The next fetch resolves DNS, classifies every answer, pins
the connection and revalidates every redirect; a URL that still resolves to loopback is
refused again within seconds. There is no command that fetches without checking.

**A worker cannot override a human.** Every machine write to `fetch_eligibility` carries
`WHERE fetch_eligibility = 'FETCHABLE'`, so a source someone disabled, flagged, or that
is already blocked keeps its state *and its original reason* — asserted for all three
states, using a successful 200 as the test case, because a success is the strongest
thing that might wrongly reopen a source.

### A cycle key is a filter

`run_cycle(cycle_key=K)` used to pass `K` to the report and nothing else. `cycle_key` is
now a required keyword argument on `claim_next` and a predicate in its SQL (D41) —
required so that no future call site can quietly drain the wrong cycle. The `UPDATE`
never writes `cycle_key`, so claiming cannot re-label work, and a retry stays in the
cycle that spawned it.

### One broken page does not end a cycle

Containment moved to the per-attempt boundary, catching `Exception` and **not**
`BaseException`, so Ctrl-C still stops a 319-page run (D42). A contained failure writes a
terminal `INTERNAL_ERROR` run with the exception's *type* as `error_class` and no
traceback in any database field — and **no snapshot and no blob**, because an internal
failure observed nothing and a snapshot would assert bytes we cannot produce.

If the lease is gone or the database is what broke, nothing is manufactured: the attempt
stays `RUNNING` and the sweeper closes it as `ABANDONED`. `INTERNAL_ERROR` earns no
retry — the cost of a defect in this repository should not be paid in requests to
someone else's server.

### Retry policy, after the changes

| Condition | Retry |
|---|---|
| `404`, `410` | never — a worklist item for whoever owns the source list |
| `401`, `403`, WAF challenge | never — `BLOCKED`, and a person decides |
| `429` | after the cooldown, as a new attempt; escalates to review at 4 in a row |
| `5xx`, timeout, temporary DNS | bounded exponential backoff, capped |
| unsafe SSRF target | **never** |
| `NXDOMAIN` | never — `NEEDS_MANUAL_REVIEW` |
| internal error (ours) | never automatically |

### What the fleet looks like before the run

```bash
make acquisition-plan cycle=pilot-full-001 hosts=1   # sends no HTTP request
```

35 institutions · 385 responsibility claims · **319 physical pages** · 120 hosts ·
**319 `FETCHABLE_NOW`**, 0 in every other state. 66 claims need no second fetch.
Busiest host 11 pages (`www.imperial.ac.uk`), median 1, and 62 hosts serve a single page.

Politeness as configured: per-host concurrency 1, a 5s floor between requests to one
host, `Retry-After` obeyed as stated. **Effective global concurrency is 1**, whatever the
setting says: `run_cycle` awaits each page before claiming the next, so the semaphore
never binds. That is safe rather than fast, and it sets the floor for a full cycle at
roughly 27 minutes plus fetch time — worth knowing before watching one.

---

## 15. The first full pilot cycle (Step 5B.3)

One watched cycle, `pilot-full-001`, over all 319 pages. **30.3 minutes**, 343 requests
(319 initial plus 24 retries), 120 hosts. Every number here is
`make pilot-full-report cycle=pilot-full-001`, not a recollection (C35).

| | |
|---|---|
| OK | **175** |
| BLOCKED | 72 (64 outright refusals, 8 challenges) |
| HTTP_ERROR | 88 (61 × 404, 25 TLS/transport, 5 × HTTP 202, others) |
| TIMEOUT | 6 |
| NAME_NOT_RESOLVED | 2 |
| RATE_LIMITED · DNS_TEMPORARY · INTERNAL_ERROR · ABANDONED · lease lost | **0** |

175 snapshots over **174 distinct bodies** (two pages served byte-identical content),
30.1 MB stored, and the object store came back with **0 missing objects, 0 hash
mismatches, 0 size mismatches and 0 orphans**. Trust state untouched: 0
publication-eligible, 0 verified domains, 0 promoted mappings, and `field_claim`,
`extraction`, `change_proposal`, `field_provenance` and every canonical table still 0.

**55% of the fleet yielded evidence.** That is the real number and it is the interesting
one: 2 institutions of 35 gave us every page, and 4 gave us none at all.

### What stopped the other 45%

| Cause | Pages | What it means |
|---|---|---|
| 403 / WAF refusal | 64 | the site declined; recorded and not worked around (D6) |
| **404** | **61** | the client's workbook URL is dead — a source-review worklist, not a bug |
| challenge interstitial | 8 | all NUS; 212–960 wire bytes, counted and **discarded** |
| TLS verification failure | 7 | CUHK (6) and Caltech (1) — see below |
| timeout | 6 | three Australian hosts are slow from here |
| HTTP 202 | 5 | a CDN answering "accepted"; we treat only 200 as a body |
| NXDOMAIN | 2 | `catalog.my.harvard.edu`, `www.gsas.upenn.edu` |

Four institutions returned nothing: **Monash, NUS, Melbourne, Oxford** — each blocked
fleet-wide rather than page-by-page. Melbourne refused 9 of 10 with 403 and timed out on
the tenth; NUS answered every request with an Imperva challenge.

### Three findings that need a decision, not a patch

**TLS verification failed on 7 pages** (`admission.cuhk.edu.hk`, `www.res.cuhk.edu.hk`,
`finaid.caltech.edu`) with *"unable to get local issuer certificate"*. That signature is
characteristic of a server not sending its intermediate certificate — browsers often
succeed anyway because they cache intermediates, which is exactly how the
misconfiguration survives unnoticed. **We could not confirm it from here**, and a gap in
our own trust store would look identical. TLS verification was not disabled and will not
be; a reviewer should open one of these URLs and, if the chain is the problem, raise it
with the institution.

**HTTP 202 is currently an error.** `catalog.upenn.edu` and `courses.cornell.edu` answer
`202 Accepted`, and the fetcher treats only `200` as carrying a body. Whether a 202 body
is evidence is a question about what counts as a successful observation, so it is left
as it is and reported rather than quietly widened.

**The 61 dead URLs are still `FETCHABLE_NOW`.** A 404 stops retries *within* a cycle, by
design, and confirms nothing about the next one — so a scheduled run would ask all 61
again. Correct per the current rules and impolite in aggregate. Parking a
confirmed-dead URL in `NEEDS_MANUAL_REVIEW` after N cycles is the obvious answer and is
not something to decide as a side effect of a counting change.

### What Step 5C actually needs

`make pilot-page-analysis` classifies the fleet technically — not a publication category,
and nothing was read for what it says:

| Classification | Pages |
|---|---|
| **STATIC_HTML** | **169** |
| PDF_DOCUMENT | 1 |
| STRUCTURED_JSON | 0 |
| POSSIBLE_BROWSER_REQUIRED | 5 |
| BLOCKED | 72 |
| SOURCE_ERROR (404) | 61 |
| UNAVAILABLE | 11 |

Of the 169 static pages: visible text from 494 to 67,809 characters (median 7,304);
**50 carry JSON-LD** and 51 carry embedded JSON; 25 have a `<table>` (139 in total); 166
have lists, median 22 per page; 112 embed an iframe.

This changes one Step 5B.1 conclusion. The 12-page smoke set found **no** JSON-LD
anywhere and suggested a structured-data parser was pointless; across 169 pages **50
have it**. A structured-data path is now worth building — as an accelerator, not a
replacement, since two thirds of pages still need HTML parsing.

**Five pages look browser-required** and no browser is being built (§20). Four are
Chicago's admissions site and McGill's homepage, each ~30–45 characters of visible text
with embedded JSON in the initial response — which suggests reading that JSON would be
cheaper and more stable than rendering. CUHK's homepage is the fifth.

**PDFs:** exactly 1 registered source is a PDF (Caltech's calendar). 29 HTML pages link
to **174** PDFs between them — Penn's calendar page alone links 48. None were downloaded:
a linked PDF is not a registered acquisition target, and following links is crawling
rather than fetching a named source.

### Conditional polling

Of the 175 pages with evidence: 80 supplied both `ETag` and `Last-Modified`, 8 an ETag
only, 17 a `Last-Modified` only, and **70 neither**. So **60% can be polled
conditionally** and 40% will re-transfer their whole body every cycle. Zero conditional
requests were sent this run, correctly — a first run has nothing to compare against.

### Politeness, measured

49% of the elapsed time was spent **waiting** on the same-host floor rather than in
requests (890s of 1,818s). Mean request 2.7s, slowest 31.7s. No host was asked for
anything while owed a pause, and no 429 was provoked from any of the 120 hosts — which is
the outcome the 5-second floor was chosen for.

Evidence store: **filesystem**. Docker is still unavailable, so S3 and MinIO remain
unexercised.
