# 3. FastAPI is the source of truth for the API contract

Date: 2026-09-17

## Status

Accepted (architecture decision D10; supersedes the contract-first approach in
architecture revision 1)

## Context

The Next.js console and, later, a CRM integration consume the API. Revision 1 of
the architecture proposed a hand-maintained OpenAPI specification in
`packages/contracts` as the single source of truth, with FastAPI validated against
it.

## Decision

Pydantic models in FastAPI define the contract. The pipeline is generated, one way:

    FastAPI/Pydantic  ->  packages/api-types/openapi.json  ->  src/schema.d.ts

Both artifacts are committed. CI regenerates and fails on drift
(`make check-api-types`). No hand-written specification.

## Rationale

A hand-maintained spec is a second source of truth that must be kept in sync by
discipline. In practice it drifts, and the drift is discovered by the consumer at
runtime. Generating from the implementation makes drift impossible by construction.

The cost is real but smaller: the contract can change without anyone reviewing a
spec diff. Committing the generated artifacts mitigates this, since a contract
change shows up as a reviewable diff in `openapi.json` on the pull request that
caused it.

## Consequences

- `packages/api-types` is entirely generated. Editing it by hand is always wrong.
- The API must be importable without a database for generation to work, which
  constrains `create_app()` to stay free of I/O at import time. This is a good
  constraint independently.
- If a future integration genuinely needs contract-first development (a partner
  building against an API before it exists) that specific surface can be specified
  first and the implementation validated against it, without changing this default.
