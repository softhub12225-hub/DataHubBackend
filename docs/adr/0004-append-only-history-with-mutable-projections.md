# 4. Append-only history with mutable projections

Date: 2026-09-17

## Status

Accepted (architecture correction C1, decision D9)

## Context

The PRD requires that every published field be traceable to its source, snapshot,
reviewer and version, and that corrections happen by adding a new version rather
than by modifying history.

Architecture revision 2 declared `field_provenance` append-only while also having
the publication transaction set `superseded_at` on prior rows, a direct
contradiction. Revision 3 removed it. Two further mutable columns on nominally
immutable tables (`entity_version.is_current`, `field_claim.resolution_status`) had
the same problem and were removed with it.

Full event sourcing was considered and rejected.

## Decision

Split the canonical plane in two:

- **History** — `field_provenance`, `entity_version`, `audit_log`, `change_event`,
  `entity_relationship`, `snapshot`, `extraction`, `field_claim`,
  `claim_resolution`, `review_decision`, `conflict_resolution`. Insert only.
  `UPDATE` and `DELETE` are revoked from every application role, with a
  `BEFORE UPDATE OR DELETE` trigger as defense-in-depth.
- **Projections** — the canonical tables plus `entity_head` and `field_current`.
  Mutable by design, rebuildable from history.

Field-level supersession is expressed by version chronology: provenance is keyed
`(entity_type, entity_id, field_path, root_version_no)` and the current row is the
one with the greatest version. Nothing is marked superseded; being older *is* being
superseded. Entity-level supersession is an append-only `entity_relationship` row.

## Rationale

Full event sourcing would satisfy immutability but pay for it on every read: a
filtered program search would replay events, or depend on a separately maintained
read model with its own consistency problem. The PRD needs *auditability* of
published facts, not a replayable system log.

The hybrid gives both. `entity_version.state_snapshot` answers "what did this
program look like on 1 September" with one indexed lookup, while the projection
serves search and API reads directly.

## Consequences

- One extra table (`field_current`) and the discipline of never reaching for an
  `UPDATE` on a history table.
- No code path and no privilege can alter a published provenance record after the
  fact. That is the property the PRD is actually buying.
- Rebuilding a damaged projection row from history is a supported repair operation,
  not the read path. A full-system replay is neither designed nor tested.
- Delivery infrastructure (`outbox_message`) is explicitly *not* history and is
  mutable, which is why correction C7 separated it from `change_event`.
