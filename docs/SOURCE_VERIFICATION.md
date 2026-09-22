# Source verification

How a submitted URL becomes something a published fact may cite. Step 5C.5.

---

## 1. MODE B, and why

Step 5C.5 offered two modes: apply the decisions, or produce a manifest and stop. It ran
in **MODE B**.

Not because the database could not hold a decision. The machinery has existed since
revision `b0c1d2e3f4a5`: `verify_domain`, `authorize_external_domain`, `reject_domain`,
`replace_domain`, `mark_domain_legacy`, `verify_source_mapping`, `verify_candidate`,
`classify_candidate` — each demanding an `Actor` and a reason, each appending to the
hash-chained audit log, and none of them with a bulk variant. The module docstring puts
it plainly: *there is no `verify_all_matching_hosts` function, and its absence is the
feature.*

The reason is that there is exactly one `app_user` row. It was created during Step 5C.3
to attribute a single review decision. It has no `password_hash` and no
`external_subject`, so nobody can authenticate as it. Writing 129 domain decisions and
385 responsibility decisions under it would record that a person had inspected 514
things they have not seen. The identity would be real and the judgement would not, and
that is exactly the disguise section 10 prohibits.

So the step persists three manifests — every row `NEEDS_REVIEW` — and stops:

```
apps/api/.reports/step-5c5/domain_decisions_proposed.{json,csv}          129 hosts
apps/api/.reports/step-5c5/source_decisions_proposed.{json,csv}          319 pages
apps/api/.reports/step-5c5/responsibility_decisions_proposed.{json,csv}  385 claims
```

Regenerate with `make verify-manifest`. Promotion readiness is consequently **0**, which
is the correct answer and not a shortfall.

---

## 2. Three units, three decisions

They are separate because they answer different questions, and an answer to one is never
an answer to another.

| Unit | Table | Question | Rows today |
|---|---|---|---|
| **Domain / host** | `official_domain` | Is this host controlled by, or officially authorised for, this institution? | 0 of 129 |
| **Page / URL** | `source_mapping` | Is this URL an appropriate official source, for one category? | 0 of 319 |
| **Responsibility claim** | `pilot_collected_source.verification_state` | Is this page authoritative *for this particular thing*? | 385 `PENDING` |

The 385 and the 319 are deliberately not the same number. 66 workbook rows carry
`duplicate_of_source_ref`, and they are how **one URL carries several
responsibilities**. Collapsing them into 319 page decisions would lose precisely the
distinction this step exists to make: an official admissions page is not thereby a
tuition source.

A verified domain does not verify the pages on it. `source_mapping.publication_eligibility`
is a `GENERATED ALWAYS ... STORED` column reading only that mapping's own
`verification_status` and `source_category`, so a second responsibility on a verified
host is `NOT_ELIGIBLE` until somebody verifies *it* — computed by the database, not
asserted by a rule.

---

## 3. What must be true for a source to become eligible

End to end, with the mechanism that enforces each arrow:

```
official_domain.verification_status ∈ {VERIFIED_OFFICIAL, AUTHORIZED_EXTERNAL}
   │   ck_official_domain_verified_domain_records_its_basis
   │       — requires verification_method, verified_at AND verified_by (FK → app_user)
   ▼
source_mapping  (url + source_category, official_domain_id)
   │   source_mapping_requires_trusted_host   — refuses a mapping on an untrusted host
   ▼
source_mapping.publication_eligibility        — GENERATED; no role can write it
   │   ck_source_mapping_only_a_trusted_mapping_may_be_promoted
   ▼
source_mapping.promoted_source_id → source
   │   source_eligibility_is_earned
   │       — refuses any class unless a promoted mapping vouches, its derived
   │         eligibility matches, its url_sha256 matches, and it and its domain are active
   ▼
source.publication_eligibility ∈ {OFFICIAL_VERIFIED, AUTHORIZED_EXTERNAL, AUTHORIZED_RANKING}
   │   ck_source_eligibility_records_actor_and_time
   ▼
field_claim_requires_eligible_evidence  (C27)   — the only column it reads is the one above
```

`TARGET_SCOPE_ONLY` is exempt from the earning rule and refused by C27 anyway: it labels
the client's target list, which is not an official source at all and exists to be
refused.

**Nothing in this chain is reachable from HTTP.** A 200, a title that names the
institution, and a `.edu` hostname are each tested to confer nothing — see
`tests/integration/test_source_verification.py`.

---

## 4. Decision classes

`official_verification_status` already carries what section 6 asked for:

| Label | Meaning | Constraint |
|---|---|---|
| `VERIFIED_OFFICIAL` | the institution's own host | needs method, actor, time |
| `AUTHORIZED_EXTERNAL` | a third party the institution authorised | additionally needs `authorization_reference` |
| `REJECTED` | not the institution's | needs `rejected_reason`, forced inactive |
| `CANDIDATE` | proposed, nobody has decided — this is `NEEDS_REVIEW` | the default |
| `LEGACY` | retired but historically real | forced inactive |

`LEGACY` has defined semantics (`ck_official_domain_legacy_domain_is_inactive`), so it is
usable; it is not used by any proposal here because none of the 129 hosts is a retired
one.

---

## 5. Revocation

Revoking a domain used to revoke nothing. `source.publication_eligibility` is a **stored
copy** of a decision two tables away, checked by `source_eligibility_is_earned` at the
moment it is set and never again — so a host could be declared not the institution's
while every source under it kept reading `OFFICIAL_VERIFIED`, and C27 reads exactly that
column.

Revision `b3c4d5e6f7a8` adds two `AFTER UPDATE` triggers. When a domain or a mapping
stops being trusted or active, dependent sources go back to `NOT_ELIGIBLE` with the
reason recorded.

What withdrawal does **not** touch:

- `snapshot`, `extraction`, `fetch_run`, `content_blob` — the evidence, append-only;
- `field_claim`, `field_provenance`, `change_event` — history, which `app_forbid_mutation`
  refuses to change at all;
- the `official_domain` row itself, which keeps its `rejected_reason`.

Facts already published stay, visibly sourced from a host whose verification was later
withdrawn. That is what an auditor needs to see. Rewriting them would destroy the record
of what was believed and when.

---

## 6. Replacing a dead URL

61 of the 319 pages answer 404, 7 fail TLS verification and 2 do not resolve. An official
URL can be dead.

When a reviewer supplies a replacement, the old source is **not** edited to point at the
new URL. Its snapshots were fetched from the old one, and repointing it would make the
stored evidence say it came from somewhere it did not. `source.superseded_by_source_id`
records the relationship instead; the old row keeps its URL, its `url_hash` and every
fetch it made, and the new row is an ordinary new source with its own history. A source
may be superseded only once inactive, because a replaced source still being fetched is
two sources for one thing.

No replacement URL is discovered at this stage. That is a reviewer's input, not a crawl.

---

## 7. Access classes, and what each does and does not mean

Kept apart because they have different consequences (sections 8 and 9):

| Class | Pages | What it means |
|---|---|---|
| `BODY_EVIDENCE` | 175 | a stored body a reviewer can read |
| `BLOCKED` | 72 | the site refused automation. **Not** evidence the domain is unofficial |
| `DEAD_NOT_FOUND` | 61 | HTTP 404. Needs a replacement URL |
| `TLS_FAILURE` | 7 | certificate verification failed. A transport problem, not identity |
| `DEAD_HOST` | 2 | does not resolve |
| `OTHER_HTTP` | 2 | HTTP 202 |

A blocked official source may remain a **verified official domain** and still be unusable
for candidate extraction: `DOMAIN_VERIFIED` and `PAGE_EVIDENCE_AVAILABLE` are different
facts. What must not happen is page-content verification being faked for a body that was
never captured, so the manifest marks those rows and says so.

---

## 8. What blocks promotion

1. **No writer for `promoted_source_id`.** The chain in §3 is enforced end to end, but
   nothing in `src/` or `scripts/` ever writes `source_mapping.promoted_source_id` —
   only tests and the migration. `register_verified_candidate` creates a mapping and
   stops, deliberately, because promotion is a separate decision. So no pilot source can
   be made eligible today even with every human decision in place. This is a missing
   function, not a missing rule.
2. **No reviewer identity.** See §1.
3. **0 verified domains**, therefore 0 eligible sources, therefore 0 promotable
   candidates — which C27 enforces rather than merely expecting.


---

## 9. Authority is scoped to a field

Step 5C.6. C27 was asymmetric, and the asymmetry was permissive for the class that
matters most:

| eligibility | scope before 5C.6 |
|---|---|
| `AUTHORIZED_RANKING` | ranking entity types, plus a live licence |
| `AUTHORIZED_EXTERNAL` | an exact `(entity_type, field_path)` binding |
| `OFFICIAL_VERIFIED` | **source level only** |

The pilot's shape makes that concrete. 385 responsibility claims over 319 URLs means one
page is submitted as both `LANGUAGE_REQUIREMENTS` and `TUITION_FEES`, and a reviewer
verifies one and rejects the other. A source-level gate honours only the first: the
tuition claim publishes on the strength of the language verification, from the same
source, the same snapshot and the same extraction.

The fix is symmetry, not a new mechanism. `source_field_binding` already scoped
`AUTHORIZED_EXTERNAL`; the check simply stops being conditional on one class. An eligible
source now publishes nothing until its bindings exist.

### 9.1 Which responsibility may publish which field

`app.domains.verification.policy.FIELD_AUTHORITY` is its own table, **not** `ROUTING`.
"May this rule read this page?" and "may this page publish this fact?" are different
questions, and one table answering both is the D39 mistake -- widening extraction for
recall would silently widen publication.

They are tied by an invariant instead: publication authority must be a subset of
extraction authority, because a page nobody was allowed to read cannot be one something
is published from. It earned its keep immediately, catching `APPLICATION_DEADLINE`
granted to `ENTRY_REQUIREMENTS` -- which `ROUTING` does not let the deadline extractor
read at all, so the grant authorised something that could not exist.

---

## 10. Promotion

`verification.promotion.promote` is the writer Step 5C.5 reported missing. Ten checks in
one transaction: host verified and active, responsibility verified and active, derived
eligibility promotable, source present, not superseded, not inactive, URL hashes equal,
not already promoted elsewhere, actor present, reason present.

It writes `promoted_source_id`, then `source.publication_eligibility`, then the
`source_field_binding` rows the mapping's responsibility implies -- and appends to the
audit chain last, as every writer here does.

It **consumes** trust and never creates it. A missing decision raises
`NotYetTrustedError`, whose remedy is a person rather than a retry. It is explicit rather
than automatic: verification says *this page is what it claims to be*, promotion says
*and we are now going to rely on it*, and keeping them apart makes the second observable
and separately revocable. Repeating it is idempotent and writes no audit row, because a
repeated request is not a second decision.

---

## 11. Who may decide

`role.reviewer` already existed with `is_reviewer_role = true`, so there is no second
role. What is new is one permission, `source:verify`, deliberately not `source:manage`:
registering a URL is data entry and declaring it official is the act the C27 boundary
rests on, so a `data_editor` who can add a source cannot make it publishable.

`provision_reviewer` refuses to invent anything -- no default email, no default display
name. A reserved domain (`.test`, `.invalid`, `.example`) is refused unless the identity
is explicitly `test_only`, and a `test_only` identity must use one, so the flag cannot
disguise a fixture as a real reviewer or the reverse. Test identities carry
`[TEST ONLY]` in the display name, visible in the audit log without a join.

No password is set: authentication belongs to a front end that does not exist yet, and an
identity that cannot sign in is the correct state until it does.

---

## 12. Batch application

`source_review.py apply-manifest` takes a reviewer-approved manifest and refuses
anything else:

- each manifest carries `content_sha256` over its rows; a file edited after generation is
  refused;
- `--expect-sha256` names the version that was reviewed, so the sequence *reviewer reads
  A, generator produces B, apply applies B* cannot happen silently;
- a row with no `decision` is malformed and aborts the file. It is never read as
  `VERIFIED`;
- a row with a decision and no reason aborts too;
- the whole manifest applies or none of it does, because a half-applied trust state is
  worse than an unapplied one;
- `--dry-run` reports every change and mutates nothing.

---

## 13. Revocation and partial revocation

Revoking a **promoted** mapping was impossible: the constraint that stops an untrusted
mapping being promoted also refused the single `UPDATE` that rejects one, so the
withdrawal trigger could never fire. Losing trust now clears the promotion in the same
statement.

Partial revocation is the property the 385-over-319 model depends on. Two
responsibilities on one URL, both verified and promoted; revoking one must leave the
other alone. It does, because authority hangs off the mapping rather than the page.

Revoking the **host** withdraws everything under it, which is correct: the host is the
root of the authority. Neither touches evidence, candidates, review history or published
claims.

---

## 14. What still blocks real verification

1. **No authenticated reviewer.** The machinery is complete and `user_role` is empty. An
   operator must run `make review-reviewer-create` with a real identity, and the client
   must decide who that is.
2. Everything else is ready: 129 domain proposals, 319 source proposals and 385
   responsibility proposals are persisted and hash-stamped, the decision services apply
   them one row at a time with an actor and a reason, and promotion follows.


---

## 14. The operator path

Seven acts, each explicit, each separately auditable and revocable:

| # | command | what it establishes |
|---|---|---|
| A | authenticate (`--reviewer-email` + TTY password) | who this process is |
| B | `review-packet` / `review-forms` | what is being reviewed, by manifest SHA |
| C | `source_review.py domain` | this host is the institution's |
| D | `source_review.py pilot-source` | this URL is the right page |
| E | `source_review.py register-pilot-source` | a `source_mapping` exists — `CANDIDATE`, `NOT_ELIGIBLE` |
| F | `source_review.py responsibility` | this page is authoritative **for this thing** |
| G | `source_review.py promote` | and we now rely on it |

D and E had no operator command until Step 5C.7B (C67). Every write takes
`--reviewer-email`, `--reason` and `--dry-run`, and **none takes `--actor`**.

`register-pilot-source` grants nothing: the mapping is created `CANDIDATE`, so its
generated eligibility is `NOT_ELIGIBLE`, nothing is promoted, and two further explicit
acts stand between registration and a publishable source.

---

## 15. Authentication

A credential, not an argument. `AppUser.password_hash` is Argon2id — the algorithm the
column comment has named since the identity module was written — and the password is
read from a TTY or `DATAHUB_REVIEWER_PASSWORD`, never from the command line.

Four refusals, in order: unknown account or no local credential, wrong password,
deactivated account, and an account whose roles lack `source:verify`. The first two
report identically, because a caller that can tell them apart is an account-enumeration
oracle.

A `[TEST ONLY]` identity **may** authenticate — the end-to-end workflow tests depend on
it — and `refuse_test_identity_on_real_data` stops it reaching the operator commands.
Both halves matter: one that could not authenticate would leave the workflow untested,
one that could decide real sources would put a fixture's judgement in the audit trail.

This is the documented *fallback*. OIDC is the preferred path and `external_subject` is
where it lands; no provider is configured, so this is explicitly operator-only.

---

## 16. Review is not publication

`proposal:publish` has been removed from the `reviewer` role and assigned to **no role**.

The architecture's own separation was subtler — "you may not publish your own work",
re-validated per proposal (C2/D7) — which means the permission *is* the publication
authorisation with a same-actor guard. Step 5C.7B takes the stronger form. There is no
distinct application publisher role to hold it, so it is held by nobody and the
permission row survives; assigning it later is one insert, and whoever does it is making
a visible decision rather than inheriting an invisible one.

`proposal:create` stays with the reviewer. A proposal is a request for governed
publication, not publication: `app_api` may insert one, and `change_event`,
`field_claim`, `field_provenance` and every canonical table remain insertable only by
`app_publisher`. Application permissions and database roles are different axes, and no
permission widens a grant.

---

## 17. Provisioning and the bootstrap row

Creating an identity or granting it a role now requires an authenticated administrator
holding `admin:roles`. The exception is a genuine bootstrap — no active administrator
exists at all — and it closes the moment one does, with no flag to re-open it.

**Dejan's own provisioning is that bootstrap case.** It was performed with no
authenticated granting administrator because none existed, `user_role.granted_by` is
NULL, and it is **not** in the audit chain. That is deliberate: inventing a historical
admin, or filing the grant under a machine actor, would put a fabricated approval in a
chain whose whole value is that it contains none. It is recorded here instead, as
BOOTSTRAP / PRE-AUTH PROVISIONING, and every grant after it is audited.


---

## 18. Credential enrolment

Four operations. A first claim, a change and a reset need different authority, and the
single command that once did all three could take over any account:

| operation | who runs it | requires | may it overwrite? |
|---|---|---|---|
| `reviewer-issue-enrollment` | the **operator** | the owning connection, plus an administrator holding `admin:roles` — or, while none exists, the bootstrap authority | no; sets no password |
| `reviewer-enrol-password` | the **reviewer** | a live, unexpired, unused challenge for an account that has **no** credential | no — `WHERE password_hash IS NULL` |
| `reviewer-change-password` | the reviewer | the **current** password | yes, its own |
| `reviewer-reset-password` | an administrator | an authenticated identity holding `admin:roles`; audited | yes, another's |

### 18.1 Why an email address is not a credential

An email address is a routing label. It is on business cards, in `git log`, and in this
document. Two corrections came from treating it as though it proved something:

* **C70** — the command overwrote `password_hash` for whoever's address you typed.
* **C71** — with the overwrite closed, the *first* claim was still open. An account with
  no password went to whoever ran the command first.

Provisioning says *this account exists*. The challenge says *and you are the person it
was provisioned for*. Those are two claims and they need two pieces of evidence.

### 18.2 The challenge

`secrets.token_urlsafe(32)` — 256 bits, not a UUID, not derived from the email, the
timestamp or the account id, because each of those is guessable or derivable. The
database stores its SHA-256 and nothing else; the plaintext is printed once, to the
issuing operator, and never written to a log, an audit row, a `repr` or a test artifact.

SHA-256 rather than Argon2id, deliberately. `password_hash` is Argon2id because a
human-chosen password has little entropy and must be expensive to guess. A 256-bit random
token is not guessable at any cost, so a slow KDF buys nothing — and its per-row random
salt would make the token impossible to look up by hash. Different threat, different
primitive. `user_session.token_hash` made the same call for the same reason.

The default lifetime is 30 minutes, configurable with `minutes=`. It is a bearer
credential handed over out of band; a multi-day one is a password with a shorter name.
An expired challenge reports `ENROLLMENT_TOKEN_EXPIRED`, distinctly, because the remedy
is a reissue rather than a correction.

One live challenge per account, and that is a database invariant rather than a
convention: `ux_credential_enrollment_live` is UNIQUE on `user_id` WHERE the row is
neither used nor revoked. A reissue therefore *has* to revoke first, which is what makes
"reissuing invalidates the previous token" true rather than merely intended.

### 18.3 Issuing and claiming are different privileges

This is the part that actually closes C71, and it is in the grants rather than in Python,
because the attacker in this threat model is an operator running our own code. A check
they could skip by calling a different function is not a check.

| | `credential_enrollment` | `app_user` | `app_claim_enrollment` |
|---|---|---|---|
| `datahub` (owner) | all | all | — |
| `app_api` | `SELECT` | `SELECT` | `EXECUTE` |
| `app_worker`, `app_publisher` | `SELECT` | `SELECT` | — |

So the application role can *spend* a challenge and can never *mint* one. Issuance needs
the owning connection; claiming does not, and `reviewer-enrol-password` connects as
`app_api` on purpose. If it needed the owner's credential too, the separation would be
decorative — whoever could run one command could run the other. The command refuses
rather than falling back if `POSTGRES_API_PASSWORD` is unset.

`GRANT UPDATE (password_hash) ON app_user TO app_api` would have separated the two
commands just as well and was the wrong way to do it: it would let a compromised API
process set anybody's password, a larger hole than the one being closed.
`app_claim_enrollment` is `SECURITY DEFINER` with a pinned `search_path`, takes the
token's SHA-256 and an already-Argon2id-hashed password, and so never sees either
plaintext.

### 18.4 The race is in the write predicate

Both writes live inside `app_claim_enrollment`:

```sql
UPDATE credential_enrollment SET used_at = now()
 WHERE token_hash = $1 AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now()
 RETURNING id, user_id INTO v_claim, v_user;
...
UPDATE app_user SET password_hash = $2, updated_at = now()
 WHERE id = v_user AND password_hash IS NULL AND is_active;
```

Two concurrent enrolments serialise on the challenge row: the second re-evaluates after
the first commits, matches nothing, and loses. A `SELECT` followed by an `UPDATE` would
let both through, which is the entire reason the checks are in the write.

The token is spent if and only if the password was set. A failure at either step rolls
both back, so a refused enrolment costs the reviewer nothing and the same token still
works. The account conditions are re-evaluated at claim time, so an account disabled
between issuance and use cannot be claimed — revoking access has to mean revoking access.

### 18.5 Nothing on a command line

Neither the token nor the password is ever an argument. A command line appears in shell
history, in `ps` output, and in anything that echoes the command. Both are read from the
terminal with no echo, the password twice. `reviewer-enrol-password` takes no `--email`
either: the token names the account, so the command cannot be aimed at somebody else's.

### 18.6 What the audit trail says, and what it must never claim

`CREDENTIAL_ENROLLMENT_ISSUED` when a challenge is issued, `CREDENTIAL_ENROLLED` when it
is spent, `CREDENTIAL_RESET` for an administrative reset. None of them carries the token
or its hash — the hash is the verifier, and an audit log is read by more people than a
credential store is.

A bootstrap issuance happens while no administrator exists; that is its entry condition.
Filing it under a person would claim somebody approved an identity when nobody did, and
leaving it out would hide the one issuance that had no approver. It is recorded with
`actor_type = SYSTEM`, `actor_id` NULL and `bootstrap_mode = true`: *this happened, and no
human authorised it*, which is the fact a later auditor needs. The check constraint
`ck_credential_enrollment_issuer_matches_mode` keeps `bootstrap_mode` and `issued_by` in agreement, so the two
cases stay distinguishable afterwards.

`append_system` exists for that one case and refuses every other action by allow-list. A
general SYSTEM appender would make "a machine verified it" a one-word change; adding an
action to `SYSTEM_ACTIONS` is a reviewable edit to `audit.py`, whereas passing a different
string at a call site is not.

The bootstrap exception closes itself. It reuses `authorize_grant`, so unauthenticated
issuance is permitted only while the database holds no active identity carrying
`admin:roles` — only while there is nobody who *could* have authorised it — and there is
no flag to reopen it.

### 18.7 What this does not protect against

Stated plainly: anyone holding the **owning** database credential can `UPDATE app_user`
directly, and no application check prevents it. That is precisely why issuance is the
owner-only half and claiming is not — an operator holding only the application credential
can spend a challenge and cannot mint one. Direct owner access remains a trusted
position, as it is for every table in this system.

### 18.8 CLI authentication is not a browser session

The operator CLI authenticates **per command** and issues nothing. It does not touch
`user_session`, whose rows belong to the Next.js BFF, and it is not a token model. A
second, weaker way in would not be an improvement, and the CLI's fallback status is why
`AppUser.password_hash` is documented as the fallback to OIDC in the first place.

---

## 19. Local services: where they live and how to bring them back

Recorded because losing them cost an hour of rediscovery. There is no Docker, Docker
Desktop, Podman or WSL distribution on this machine; the dependencies are portable
builds under `C:\dhtmp`, started by hand.

| | path | port |
|---|---|---|
| PostgreSQL 16.9 | `C:\dhtmp\pgsql` (binaries), `C:\dhtmp\pgdata` (cluster) | 55432 |
| Redis 5.0.14.1 | `C:\dhtmp\redis` | 56379 |
| server log | `C:\dhtmp\pg.log` | |

The cluster holds `datahub` (the real pilot), `datahub_test` (the suite's database, whose
DSN is `DATAHUB_TEST_POSTGRES_DSN`) and `datahub_gates`, an orphan left at revision
`f1a2b3c4d5e6` by Step 5C.3 that nothing in the repository references.

### 19.1 Starting them

The port and bind address are **command-line options, not settings in
`postgresql.conf`** — `postmaster.opts` records what the running server was given, and
starting it any other way silently moves it:

```
C:\dhtmp\pgsql\bin\pg_ctl.exe start -D C:\dhtmp\pgdata -l C:\dhtmp\pg.log \
    -o "-p 55432 -c listen_addresses=127.0.0.1" -w
C:\dhtmp\redis\redis-server.exe --port 56379 --bind 127.0.0.1 --appendonly no
```

Redis holds no persisted state — there is no RDB or AOF file — so it always starts
empty. That is fine: it carries Celery queues and cache entries, both rebuildable, and
nothing in the evidence, governance or canonical planes lives there.

### 19.2 Why they disappeared, once

Both were children of an interactive console session. When it was torn down the log
recorded `autovacuum launcher process ... terminated by exception 0x40010004`
(`DBG_CONTROL_C`, delivered to the whole process group) followed by
`startup process ... terminated by exception 0xC000026B`
(`STATUS_DLL_INIT_FAILED_LOGOFF`). The cluster was killed, not corrupted: the next start
replayed WAL from the last checkpoint, reported `redo done`, and every count matched.
`pg_controldata` showing `in production` with no `postmaster.pid` is the signature of
exactly that, and it is a reason to start the server and let it recover — never a reason
to `initdb`.

### 19.3 Running an operator command on Windows

**`make` is not installed on this machine**, and the Makefile is also the only thing that
loads `.env`, so the bare `uv run python scripts/source_review.py ...` fails with *"the
migration role is not configured in this process"*. Neither half of that is obvious from
a `make` recipe, and a handoff command that cannot be run is not a handoff.

`review.cmd` at the repository root closes it: it loads `.env`, changes into `apps/api`,
and forwards its arguments to `source_review.py`. Call it by full path from any directory,
from `cmd.exe` or PowerShell:

```
"D:\My Project\Chinese\overseas-uni-datahub\review.cmd" reviewer-status
"D:\My Project\Chinese\overseas-uni-datahub\review.cmd" reviewer-issue-enrollment --email someone@example.org
"D:\My Project\Chinese\overseas-uni-datahub\review.cmd" reviewer-enrol-password
```

With no arguments it prints the command list. It refuses up front if `.env` is missing,
naming what that file has to contain, rather than failing later inside the settings layer.

`*.cmd` is pinned to CRLF in `.gitattributes`, against the repository-wide `eol=lf`:
`cmd.exe` is the one interpreter here that can misread an LF-only script, and this file
exists specifically to be the dependable way in.

### 19.4 Host-run commands need the secrets

`.env` is read by `docker compose --env-file`, which puts the values in the container's
environment. The nested settings classes (`DatabaseSettings` and its siblings) declare no
`env_file` for that reason: in a container the values are already there. A command run
directly on the host has no such help, so the Makefile now includes and exports `.env`
when one exists.

**Anything ad-hoc must name its own database.** `.env` sets `POSTGRES_DB=datahub`, so a
throwaway script that builds a DSN from the project settings talks to the **real pilot
database**. Point ad-hoc verification at `DATAHUB_TEST_POSTGRES_DSN`.

---

## 20. The 5C.7E incident: a test execution against the real database

**TEST EXECUTION AGAINST REAL DB — HISTORICAL INCIDENT.**

During Step 5C.7E a live verification script built its connection from the project
settings. `.env` sets `POSTGRES_DB=datahub`, so the script ran against the **real pilot
database** rather than `datahub_test`. It created eight `[TEST ONLY]` identities, issued
each a one-time enrolment challenge, exercised the claim function, and deleted the
identities. Deleting them cascaded away their `credential_enrollment` rows.

**Nine `CREDENTIAL_ENROLLMENT_ISSUED` rows in `datahub.audit_log` remain**, at sequence
numbers 2–10. They are `actor_type = SYSTEM`, `actor_id` NULL, `bootstrap_mode = true`,
and every one names an `object_id` that no longer exists in `app_user`. None names Dejan.

They remain because `audit_log` is append-only and enforced as such — `DELETE` is refused
by trigger with *"DELETE on audit_log is forbidden: this table is append-only history"*.
That is correct, and they have deliberately **not** been removed, rewritten or relabelled:

* erasing them would be erasing history to make a count match, which is the one thing an
  audit chain must never permit;
* rewriting them would require inventing an actor, and no human authorised them;
* the rows themselves are truthful — a challenge *was* issued, by no human, for an
  identity that existed at the time.

So the label lives here rather than in the rows. `audit_log` in `datahub` is expected to
contain exactly **10** entries: one `TARGET_LIST_IMPORTED` from the pilot import, and
these nine. A future auditor reading them should read this section.

What the incident cost was nothing but these rows. What it proved is in §21.

## 21. Keeping tests out of the real database

### 21.1 Why the obvious guard would not have worked

The tempting check is to read `POSTGRES_DB` and compare it, or to require
`DATAHUB_ENV=test` before connecting. That is precisely the check that failed. The
environment said `datahub` and the environment was **correct** — it is the right value for
operating the real system. The intent was wrong, not the configuration, and no amount of
reading the same variable more carefully distinguishes the two.

So the guard asks the server:

```sql
SELECT current_database()
```

That answer cannot be stale, inherited, overridden by a `PGDATABASE` nobody remembered,
or wrong about a pooled connection. Environment configuration may be checked as well;
database identity is the authority. `app.db.safety` holds it.

### 21.2 One chokepoint, so it cannot be forgotten

The guard runs inside the `postgres_dsn` fixture. Every DB-backed test reaches the
database through it — `owner_engine`, `conn`, `role_engines` and `runtime_role_passwords`
all descend from that one fixture — so there is no `assert_test_database()` for a test
author to forget. Unit tests never request it and are unaffected.

Three layers, because one is a single point of failure:

| layer | refuses |
|---|---|
| `postgres_dsn` fixture | any DB-backed test against a database not on the allow-list |
| `provision_reviewer(test_only=True)` | creating a fixture identity in the real database |
| `issue(allow_test_identity=True)` | issuing a fixture challenge in the real database |

The second and third are in production code and scoped to the fixture-only arguments, so
a *real* reviewer can still be provisioned in any database — which is how Dejan exists.
Those two are the layers that would have caught the original script.

### 21.3 Fail closed, and no escape hatch

An unrecognised database is refused exactly like the real one. A guard that allowed what
it did not recognise would pass on the very case it exists for: the database created
later, by someone who never read this file.

There is no environment variable that relaxes it, and `app.db.safety` is asserted to
contain no `os.environ` lookup at all. A flag such as `ALLOW_REAL_DB_TESTS=1` is one
inherited shell export away from being permanently on — the same class of accident as the
original. Widening the allow-list is a code change, and a code change is reviewable.

An extraordinary maintenance operation that genuinely must write to `datahub` belongs in a
separately named administrative command, not in the test runner.

### 21.4 The Makefile states the target database

`include .env` / `export` (added in 5C.7E so host-run commands see the secrets) also
exports `POSTGRES_DB=datahub` to everything `make` runs, `pytest` included. The test
session already strips `POSTGRES_*` from the environment in `_isolate_environment`, so
settings-derived connections were never affected — but relying on that is relying on a
second mechanism to undo the first. Every DB-mutating test target now passes
`POSTGRES_DB=$(TEST_DB)` explicitly, so the intended database is visible in the command
instead of inherited from developer shell state.

`make db-isolation-gate` runs the proof.

### 21.5 `datahub_gates`

Six migrations behind, referenced by nothing in the repository, and deliberately **not** in
`TEST_DATABASES`. A database that no code selects must not become a silent default just
because it exists. It was neither migrated nor dropped; a workflow that genuinely wants it
has to name it explicitly, and naming it is the documentation.
