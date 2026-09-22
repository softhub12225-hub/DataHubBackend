# Architecture decision records

The architecture decisions themselves (D1–D16, schema decisions B1–B7, corrections
C1–C8) are recorded in [../ARCHITECTURE.md](../ARCHITECTURE.md) §1, which is the
authoritative register.

This directory holds ADRs for decisions that needed their own reasoning trail —
typically because the alternative was reasonable and the choice will be questioned
later.

| ADR | Decision |
|---|---|
| [0001](./0001-record-architecture-decisions.md) | Where architecture decisions live |
| [0002](./0002-single-python-codebase-multiple-runtime-roles.md) | One Python codebase, several container roles |
| [0003](./0003-code-first-api-contract.md) | FastAPI is the contract source of truth |
| [0004](./0004-append-only-history-with-mutable-projections.md) | Append-only history, mutable projections |
