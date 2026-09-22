# 1. Where architecture decisions live

Date: 2026-09-17

## Status

Accepted

## Context

The architecture phase produced 16 numbered decisions (D1–D16), 7 schema decisions
(B1–B7) and 8 corrections (C1–C8), arrived at over several review rounds. Splitting
them across 31 separate ADR files would scatter a set of decisions that are only
comprehensible together: D14 makes no sense without D2, and C1 rewrites part of D9.

## Decision

`docs/ARCHITECTURE.md` §1 is the authoritative decision register. It carries every
D/B/C decision with its rationale and the section it affects.

`docs/adr/` holds ADRs only for decisions that need a standalone reasoning trail:
where a reasonable alternative was rejected and someone will later ask why.

## Consequences

- One place to read the design; no hunting across numbered files.
- ARCHITECTURE.md is long, and must be revised rather than appended to.
- A decision that gets corrected is amended in place with the correction recorded
  (as C1–C3 and C4–C8 were), so the register shows current truth plus its history.
