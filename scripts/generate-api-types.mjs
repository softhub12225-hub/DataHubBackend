#!/usr/bin/env node
/**
 * Regenerate the API contract artifacts, end to end.
 *
 *   FastAPI/Pydantic  ->  packages/api-types/openapi.json  ->  src/schema.d.ts
 *
 * This is the single definition of that pipeline, including the generator flags.
 * The Makefile, the pnpm scripts and CI all call it, so local and CI runs cannot
 * diverge, and Windows developers (who typically have no `make`) run the same thing.
 *
 * Usage:
 *   node scripts/generate-api-types.mjs            regenerate and write
 *   node scripts/generate-api-types.mjs --check    fail if the committed files are stale
 */

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const check = process.argv.includes("--check");

const apiTypesDir = join(repoRoot, "packages", "api-types");
const openapiPath = join(apiTypesDir, "openapi.json");
const schemaPath = join(apiTypesDir, "src", "schema.d.ts");

/**
 * Spawn without a shell.
 *
 * Node >= 20.12 refuses to spawn `.cmd`/`.bat` shims (the CVE-2024-27980 fix) and
 * fails with EINVAL, so `pnpm` cannot be launched directly on Windows. Passing
 * `shell: true` works around that but concatenates arguments instead of escaping
 * them (DEP0190). We avoid the dilemma entirely by never spawning a shim: `uv` is a
 * real executable, and the TypeScript generator is run as a JS file under
 * `process.execPath`.
 */
function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: repoRoot,
    stdio: "inherit",
    shell: false,
    ...options,
  });
  if (result.error) {
    throw new Error(`failed to launch ${command}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

/** Absolute path to the openapi-typescript CLI entry point, however pnpm laid it out. */
function resolveGeneratorCli() {
  const requireFromApiTypes = createRequire(join(apiTypesDir, "package.json"));
  const manifestPath = requireFromApiTypes.resolve("openapi-typescript/package.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const binField = manifest.bin;
  const relative =
    typeof binField === "string" ? binField : binField?.["openapi-typescript"];
  if (!relative) {
    throw new Error("openapi-typescript does not declare a usable bin entry");
  }
  return resolve(dirname(manifestPath), relative);
}

function generateTypes() {
  run(
    process.execPath,
    [
      resolveGeneratorCli(),
      "./openapi.json",
      "--output",
      "./src/schema.d.ts",
      // Emit named root types alongside the nested `components` tree, so callers can
      // import a response type by name instead of indexing into the schema.
      "--root-types",
    ],
    { cwd: apiTypesDir },
  );
}

// Step 1: export the OpenAPI document from the FastAPI app. In --check mode this
// fails if the committed document is stale.
run("uv", [
  "run",
  "python",
  "apps/api/scripts/export_openapi.py",
  ...(check ? ["--check"] : []),
]);

// Step 2: generate TypeScript types from that document.
if (check) {
  const before = readFileSync(schemaPath, "utf8");
  generateTypes();
  const after = readFileSync(schemaPath, "utf8");
  if (before !== after) {
    console.error(
      "\nFAIL: packages/api-types/src/schema.d.ts is stale.\n" +
        "Run `pnpm generate:api-types` (or `make generate-api-types`) and commit the result.",
    );
    process.exit(1);
  }
  console.log(`OK: ${schemaPath} is up to date`);
} else {
  generateTypes();
  console.log(`\ngenerated:\n  ${openapiPath}\n  ${schemaPath}`);
}
