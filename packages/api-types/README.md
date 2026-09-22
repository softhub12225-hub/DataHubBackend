# @datahub/api-types

**Everything in this package is generated. Do not edit by hand.**

The FastAPI application is the single source of truth for the API contract
(ARCHITECTURE.md D10). This package holds the two artifacts derived from it:

| File | Produced by |
|---|---|
| `openapi.json` | `apps/api/scripts/export_openapi.py` |
| `src/schema.d.ts` | `openapi-typescript` reading `openapi.json` |

## Regenerating

```bash
make generate-api-types     # or: pnpm generate:api-types
```

Both artifacts are committed so that CI can detect drift without booting Python:

```bash
make check-api-types
```

That command regenerates the document in memory and fails if the committed copy
differs. If it fails, run the generator and commit the result — do not edit the
files to make the check pass.
