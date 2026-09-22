# Overseas University DataHub -- development commands.
#
# Every target here is also reachable as a pnpm script or a direct uv/pnpm command
# (see README.md), because `make` is not usually present on Windows. CI uses these
# targets so that local and CI runs execute identical commands.

# Host-run targets need the same secrets the containers get. `--env-file` below only
# reaches docker compose; a command run directly on the host reads its process
# environment, and the nested settings classes (DatabaseSettings and friends) declare
# no env_file precisely because in a container compose has already put the values
# there. Including and exporting .env closes that gap for local use without changing
# how deployed processes are configured.
#
# Guarded by wildcard so a checkout with no .env behaves exactly as before.
ifneq (,$(wildcard .env))
include .env
export
endif

# The database DB-backed tests are allowed to write to. Stated here and passed
# explicitly by every test target, because Step 5C.7E's incident was a test-shaped
# write that reached the real pilot database through an inherited POSTGRES_DB. The
# guard in app/db/safety.py asks the server which database it is actually attached
# to and refuses `datahub`; this makes the intended answer visible in the command
# rather than dependent on developer shell state.
TEST_DB      := datahub_test
TEST_DB_ENV  := POSTGRES_DB=$(TEST_DB)

COMPOSE := docker compose -f infra/compose/docker-compose.yml --env-file .env
UV      := uv
PNPM    := pnpm

.DEFAULT_GOAL := help
.PHONY: help bootstrap up up-deps down down-volumes logs ps \
        migrate migrate-down migration migrate-sql import-target-list \
        export-pilot-template validate-pilot-workbook \
        lint lint-py lint-web format typecheck typecheck-py typecheck-web \
        test test-py test-py-unit test-web build-web \
        generate-api-types check-api-types \
        api worker beat shell-api shell-db check \
        claims-run claims-coverage claims-sanity claims-audit \
        claims-lineage claims-untouched \
        claims-integrity claims-groups claims-programs claims-language-groups \
        claims-deadline-groups claims-calendar claims-admission-quality \
        claims-admission-sample claims-scope claims-tuition-review claims-queue \
        claims-readiness claims-sources claims-review-list claims-review-show \
        claims-review-accept claims-review-reject claims-review-context \
        claims-review-scope claims-review-untouched \
        privilege-gate runtime-roles-reset runtime-roles-verify claims-low \
        verify-packet verify-domains verify-redirects verify-responsibilities \
        verify-authority verify-scopes verify-untouched verify-manifest \
        review-status review-reviewer-create review-matrix review-readiness \
        review-packet review-forms review-enrol-password review-change-password \
        review-issue-enrollment review-revoke-enrollment \
        db-isolation-gate \
        review-pilot-source review-register-source

# ---------------------------------------------------------------------------
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
bootstrap: ## Install all dependencies and create .env if absent
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example -- review the passwords")
	$(UV) sync
	$(PNPM) install

# ---------------------------------------------------------------------------
# Local stack
# ---------------------------------------------------------------------------
up: ## Start the full stack (postgres, redis, minio, api, worker, beat, web)
	$(COMPOSE) up --build -d
	@echo "api  -> http://localhost:8000/health/ready"
	@echo "web  -> http://localhost:3000/system-status"

up-deps: ## Start dependencies only (postgres, redis, minio) for running apps on the host
	$(COMPOSE) up -d postgres redis minio minio-init

down: ## Stop the stack, keeping volumes
	$(COMPOSE) down

down-volumes: ## Stop the stack and DESTROY local Postgres and MinIO data
	$(COMPOSE) down --volumes

logs: ## Follow logs for all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
migrate: ## Apply all migrations
	cd apps/api && $(UV) run alembic upgrade head

migrate-down: ## Revert the most recent migration
	cd apps/api && $(UV) run alembic downgrade -1

migrate-sql: ## Print the DDL for review without touching the database
	# PYTHONIOENCODING: migration comments contain Chinese, and a Windows console
	# defaults to a codec that cannot encode it.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run alembic upgrade head --sql

import-target-list: ## Import a client target list: make import-target-list f=path/to/list.xlsx
	@test -n "$(f)" || (echo 'usage: make import-target-list f=path/to/list.xlsx [dry=1]' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/import_target_list.py \
	  "$(abspath $(f))" $(if $(dry),--dry-run,)

export-pilot-template: ## Export the pilot collection workbook: make export-pilot-template o=out/pilot.xlsx
	@test -n "$(o)" || (echo 'usage: make export-pilot-template o=out/pilot.xlsx [dest="GB HK MO"] [n=35]' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/export_pilot_template.py \
	  "$(abspath $(o))" $(foreach d,$(dest),--destination $(d)) $(if $(n),--pilot-target $(n),)

validate-pilot-workbook: ## Check a returned workbook: make validate-pilot-workbook f=returned.xlsx n=35
	@test -n "$(f)" || (echo 'usage: make validate-pilot-workbook f=returned.xlsx [n=35]' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/validate_pilot_workbook.py \
	  "$(abspath $(f))" $(if $(n),--expect-selected $(n),)

import-pilot-workbook: ## Import a completed collection workbook into staging: make import-pilot-workbook f=returned.xlsx
	@test -n "$(f)" || (echo 'usage: make import-pilot-workbook f=returned.xlsx [n=35] [dry=1]' && exit 1)
	# Staging only: writes pilot_* rows and nothing else. No source is fetched,
	# and no canonical or published fact is created (U12).
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/import_pilot_workbook.py \
	  "$(abspath $(f))" $(if $(n),--expect-selected $(n),) $(if $(dry),--dry-run,)

import-official-sources: ## Import the final official-source list: make import-official-sources f=university_official_sources.xlsx [dry=1]
	@test -n "$(f)" || (echo 'usage: make import-official-sources f=university_official_sources.xlsx [n=35] [dry=1] [by=<app_user uuid>]' && exit 1)
	# Establishes acquisition targets only: staging rows and PENDING verification
	# candidates. Creates no source, no mapping, no fact, and fetches nothing.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/import_official_sources.py \
	  "$(abspath $(f))" $(if $(n),--expect-institutions $(n),) $(if $(by),--imported-by $(by),) $(if $(dry),--dry-run,)

source-verification-report: ## The U15 source verification worklist: make source-verification-report [list=1]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_verification_report.py \
	  $(if $(by),--by $(by),) $(if $(list),--list,) $(if $(pages),--pages,) $(if $(all),--all-institutions,)

acquisition-register: ## Register the pilot's distinct URLs as acquisition targets [dry=1]
	# Creates one source per distinct URL. NOT a trust signal: every source is
	# NOT_ELIGIBLE for publication, and nothing is fetched.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py register $(if $(dry),--dry-run,) $(if $(by),--by $(by),)

acquisition-enqueue: ## Queue a cycle: make acquisition-enqueue pilot=1 | university=<id> | source=<id> [dry=1]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py enqueue $(if $(pilot),--pilot,) $(if $(university),--university $(university),) \
	  $(if $(source),--source $(source),) $(if $(cycle),--cycle $(cycle),) $(if $(limit),--limit $(limit),) $(if $(dry),--dry-run,)

acquisition-worker: ## Fetch queued pages. CONTACTS REAL WEBSITES. [pages=5]
	# Politeness is not optional: one request in flight per host, a floor between
	# them, Retry-After obeyed. More than 20 pages needs confirm=1.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py worker --max-pages $(if $(pages),$(pages),5) \
	  $(if $(cycle),--cycle $(cycle),) $(if $(confirm),--i-understand,)

acquisition-report: ## Acquisition state and source health [by=institution]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py report $(if $(by),--by-institution,)

claims-integrity: ## Locator resolution over ALL current candidates, plus routing
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py integrity

claims-groups: ## Agreement, conflict and unconfirmed-disagreement groups [limit=N]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py groups $(if $(limit),--limit $(limit),)

claims-programs: ## Program context groups and the section 9 quarantine [limit=N]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py programs $(if $(limit),--limit $(limit),)

claims-language-groups: ## Language requirement groups, one per test
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py language

claims-deadline-groups: ## Deadline groups and what context the pages actually gave
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py deadlines

claims-calendar: ## Academic calendar separation: nothing becomes a deadline
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py calendar

claims-admission-quality: ## The 985 admission candidates, classified structurally
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py admission

claims-admission-sample: ## Stratified manual-review sample [limit=100]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py admission-sample $(if $(limit),--limit $(limit),)

claims-scope: ## Applicant scope reconciliation. UNIVERSAL is never inferred
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py scope

claims-tuition-review: ## All current tuition candidates, in full, with financial context
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py tuition

claims-queue: ## Review queues and explainable priority [limit=N]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py queue $(if $(limit),--limit $(limit),)

claims-readiness: ## Promotion readiness and every blocker
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py readiness

claims-sources: ## Source verification assistance for the later verification pass
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py sources

claims-review-list: ## The review queue [kind= queue= state= limit=]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py list $(if $(kind),--kind $(kind),) $(if $(queue),--queue $(queue),) $(if $(state),--state $(state),) $(if $(limit),--limit $(limit),)

claims-review-show: ## One candidate in full: make claims-review-show candidate=<id>
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py show --candidate $(candidate)

claims-review-accept: ## Record ACCEPTED [candidate= actor= code= reason=]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py accept --candidate $(candidate) --actor $(actor) --reason-code $(code) $(if $(reason),--reason "$(reason)",)

claims-review-reject: ## Record REJECTED [candidate= actor= code= reason=]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py reject --candidate $(candidate) --actor $(actor) --reason-code $(code) $(if $(reason),--reason "$(reason)",)

claims-review-context: ## Record NEEDS_CONTEXT [candidate= actor= code= reason=]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py context --candidate $(candidate) --actor $(actor) --reason-code $(code) $(if $(reason),--reason "$(reason)",)

claims-review-scope: ## Record NEEDS_SCOPE_MAPPING [candidate= actor= code= reason=]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py scope-mapping --candidate $(candidate) --actor $(actor) --reason-code $(code) $(if $(reason),--reason "$(reason)",)

claims-review-untouched: ## Prove nothing publishable or canonical moved
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_claims.py untouched

claims-run: ## Extract candidate field claims from stored documents (offline)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_claims.py run

claims-openai: ## OpenAI candidate extraction for pilot fleet (needs OPENAI_API_KEY)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_claims_openai.py run

pilot-all-universities: ## Full pilot pipeline: make pilot-all-universities cmd=all i-understand=1
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/run_all_universities.py $(cmd) \
	  $(if $(i-understand),--i-understand,) $(if $(max-pages),--max-pages $(max-pages),) \
	  $(if $(limit),--limit $(limit),) $(if $(dry),--dry-run,)

claims-coverage: ## Claim coverage by responsibility, institution and gap kind
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_claims.py coverage

claims-sanity: ## Automated checks for claims that cannot be true
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_claims.py sanity

claims-audit: ## Deterministic samples for manual precision review
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_claims.py audit

claims-lineage: ## Walk one claim back to the bytes it came from
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_claims.py lineage

claims-untouched: ## Prove nothing publishable or canonical moved
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_claims.py untouched

claims-low: ## What is left in the LOW confidence band, by shape [samples=N]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_claims.py low $(if $(samples),--samples $(samples),)

review-issue-enrollment: ## OPERATOR: issue the one-time challenge for an account [email=...]
	# Run by the OPERATOR, not by the person claiming the account, and on the owning
	# connection -- app_api holds no INSERT on credential_enrollment, so it cannot mint
	# one. That asymmetry is what stops whoever knows an email address claiming the
	# identity it names before its owner does.
	#
	# Prints the token ONCE. Deliver it out of band -- not by email, which is the
	# channel the token exists to be independent of. Only its SHA-256 is stored.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py reviewer-issue-enrollment --email "$(email)" $(if $(minutes),--minutes $(minutes),) $(if $(reissue),--reissue,)

review-revoke-enrollment: ## OPERATOR: revoke an outstanding challenge [email=..., reason=...]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py reviewer-revoke-enrollment --email "$(email)" --reason "$(reason)"

review-enrol-password: ## REVIEWER: claim your account with the challenge issued for it
	# Run by the REVIEWER. Prompts for the enrollment token, then for the password
	# twice -- neither is ever an argument, because a command line is in the shell
	# history and in `ps` output. Only the Argon2id hash is stored.
	#
	# Takes no --email: the token names the account, so this cannot be aimed at
	# somebody else's. Needs only the application credential, not the owner's --
	# claiming a challenge and issuing one are deliberately different privileges.
	#
	# Initial enrolment ONLY: it refuses an account that already has a credential, so
	# it cannot take one over. Changing a password is review-change-password.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py 		reviewer-enrol-password

review-change-password: ## Change YOUR OWN password; asks for the current one [email=...]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py 		reviewer-change-password --email "$(email)"

review-pilot-source: ## Record a decision about ONE workbook row [pilot=..., decision=...]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py 		pilot-source --pilot-source "$(pilot)" --decision "$(decision)" 		--reviewer-email "$(email)" --reason "$(reason)" $(if $(dry),--dry-run,)

review-register-source: ## Create the source_mapping for a VERIFIED workbook row
	# Registration is not authority: the mapping is created CANDIDATE and NOT_ELIGIBLE.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py 		register-pilot-source --pilot-source "$(pilot)" 		--reviewer-email "$(email)" --reason "$(reason)" $(if $(dry),--dry-run,)

review-forms: ## Write the reviewer decision forms (.md + 2 CSVs) with blank decisions
	# Section 16. Stamped with the manifest SHAs and the reviewer's identity; every
	# decision field is blank, and apply-manifest refuses a blank rather than defaulting.
	#   make review-forms reviewer_id=... name="..." email="..."
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_forms.py 		--reviewer-id "$(reviewer_id)" --reviewer-name "$(name)" --reviewer-email "$(email)"

review-packet: ## Institution-grouped review packet from the hashed manifests [institution=X]
	# Section 4. A view of the three approved packages, SHAs printed at both ends, so a
	# reviewer can see exactly which immutable package they would be approving. Decides
	# nothing and reads nothing but the manifests.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/review_packet.py 		$(if $(institution),--institution "$(institution)",) $(if $(limit),--limit $(limit),)

review-status: ## Who may record a verification decision, and what they hold
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py reviewer-status

review-reviewer-create: ## Provision a REAL reviewer. Operator supplies the identity
	# Section 11. Nothing is invented and this is never run automatically:
	#   make review-reviewer-create email=... name="..."
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py 		reviewer-create --email "$(email)" --display-name "$(name)" $(if $(dry),--dry-run,)

review-matrix: ## The source/responsibility readiness matrix, computed from the policy
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py matrix

review-readiness: ## Promotion readiness with per-candidate blocker reasons [sample=N]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/source_review.py readiness $(if $(sample),--sample $(sample),)

verify-manifest: ## MODE B: persist the 129-host / 319-source / 385-responsibility review manifest
	# Section 19. Writes proposals only, all NEEDS_REVIEW, and modifies no verification
	# state. Lands in apps/api/.reports/step-5c5/ as .json and .csv.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/verification_manifest.py

verify-packet: ## Per-page source verification packet. READ ONLY; verifies nothing [limit=N]
	# Section 15. A successful fetch means a server answered. It is not evidence that
	# the institution publishes there, and this script cannot record a decision.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/verify_sources.py packet $(if $(limit),--limit $(limit),)

verify-domains: ## The 120 hosts, grouped as a domain decision would be made [limit=N]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/verify_sources.py domains $(if $(limit),--limit $(limit),)

verify-redirects: ## Hosts reached without being asked for; trust does not follow
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/verify_sources.py redirects

verify-responsibilities: ## What each page is claimed to be, for confirmation or denial
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/verify_sources.py responsibilities

verify-authority: ## Where a verification decision gets recorded, and that none has been
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/verify_sources.py authority

verify-scopes: ## The applicant-scope vocabulary the pages actually use [limit=N]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/verify_sources.py scopes $(if $(limit),--limit $(limit),)

verify-untouched: ## Prove this step published nothing
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/verify_sources.py untouched

db-isolation-gate: ## Prove automated tests cannot mutate the real pilot database
	# Step 5C.7F. The guard asks the server SELECT current_database() rather than
	# reading POSTGRES_DB, because in the 5C.7E incident the environment was
	# correct -- datahub is the right value for operating the real system -- and
	# the intent was not. Reading the variable again would have caught nothing.
	cd apps/api && $(TEST_DB_ENV) PYTHONIOENCODING=utf-8 $(UV) run pytest tests/integration/test_database_isolation.py -q -rs

privilege-gate: ## Run the 15 runtime-role privilege tests. 0 skips required before promotion
	# Section 13. These exercise the actual app_api / app_worker / app_publisher roles
	# over a real authenticated connection. When their passwords are not in the
	# environment the suite generates one per role for the run and restores the
	# original verifier afterwards -- see apps/api/tests/runtime_credentials.py.
	cd apps/api && $(TEST_DB_ENV) PYTHONIOENCODING=utf-8 $(UV) run pytest 		tests/integration/test_privileges.py 		tests/integration/test_runtime_credentials.py -q -rs

runtime-roles-reset: ## Re-set the runtime role passwords from APP_*_PASSWORD in the environment
	# Needed after a test run is hard-killed mid-provisioning: the generated password
	# survives and the original verifier exists nowhere else.
	#
	# The Python path rather than infra/postgres/init/10-runtime-roles.sh, which needs
	# psql and the container entrypoint. Same statements, same three environment
	# variables; it refuses to run rather than invent a password, and prints role names
	# and whether each authenticates, never a credential.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/provision_runtime_roles.py

runtime-roles-verify: ## Prove each runtime role authenticates. Changes nothing
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/provision_runtime_roles.py --verify-only

extract-documents: ## Normalise stored evidence into documents. OFFLINE; no field_claim [limit=N]
	# Reads bytes already in the evidence store. No network request, no JavaScript, no
	# LLM, and no business fact of any kind.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_documents.py run $(if $(limit),--limit $(limit),)

extract-report: ## Extraction totals, structural coverage, charsets and the PDF
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_documents.py report

extract-jsonld: ## The JSON-LD @type inventory the real pages contain
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_documents.py jsonld

extract-thin: ## Low-text pages: is browser rendering genuinely needed? [threshold=N]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/extract_documents.py thin $(if $(threshold),--threshold $(threshold),)

pilot-full-run: ## CONTACTS 120 REAL SITES for 319 pages: make pilot-full-run cycle=pilot-full-001
	@test -n "$(cycle)" || (echo 'usage: make pilot-full-run cycle=pilot-full-001' && exit 1)
	# Refuses without --i-understand. Effective concurrency 1, per-host 1, 5s same-host
	# floor, TLS verified, addresses pinned, every redirect hop revalidated.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/pilot_full_run.py --cycle $(cycle) --i-understand

pilot-full-report: ## Every Step 5B.3 number, queried from the cycle: make pilot-full-report cycle=<key>
	@test -n "$(cycle)" || (echo 'usage: make pilot-full-report cycle=pilot-full-001 [section=hosts]' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/pilot_full_report.py --cycle $(cycle) $(if $(section),--section $(section),)

pilot-page-analysis: ## Technical page shapes for parser design; no extraction [all=1]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/pilot_page_analysis.py $(if $(all),--list-all,)

acquisition-plan: ## Full-fleet dry run; sends no HTTP request: make acquisition-plan [cycle=<key>] [hosts=1]
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py plan $(if $(cycle),--cycle $(cycle),) $(if $(hosts),--hosts,)

acquisition-source-status: ## One source's operational state: make acquisition-source-status source=<id>
	@test -n "$(source)" || (echo 'usage: make acquisition-source-status source=<uuid>' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py source-status --source $(source)

# The four recovery actions (Step 5B.2 section 17). Each needs an actor and a reason,
# and each appends to the audit chain: they are a person overriding what the system
# concluded. Re-enabling means "a worker may request this URL again" and never "this
# source is trusted" -- publication eligibility is untouched, and the next fetch still
# resolves DNS, validates every address and revalidates every redirect.
acquisition-source-reenable: ## Allow fetching again: make acquisition-source-reenable source=<id> actor=<id> reason="..."
	@test -n "$(source)" -a -n "$(actor)" -a -n "$(reason)" || (echo 'usage: make acquisition-source-reenable source=<uuid> actor=<uuid> reason="..."' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py source-reenable --source $(source) --actor $(actor) --reason "$(reason)"

acquisition-source-disable: ## Stop fetching: make acquisition-source-disable source=<id> actor=<id> reason="..."
	@test -n "$(source)" -a -n "$(actor)" -a -n "$(reason)" || (echo 'usage: make acquisition-source-disable source=<uuid> actor=<uuid> reason="..."' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py source-disable --source $(source) --actor $(actor) --reason "$(reason)"

acquisition-source-review: ## Park for a human: make acquisition-source-review source=<id> actor=<id> reason="..."
	@test -n "$(source)" -a -n "$(actor)" -a -n "$(reason)" || (echo 'usage: make acquisition-source-review source=<uuid> actor=<uuid> reason="..."' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py source-review --source $(source) --actor $(actor) --reason "$(reason)"

acquisition-clear-cooldown: ## Cut a cooldown short: make acquisition-clear-cooldown source=<id> actor=<id> reason="..." [host=1]
	@test -n "$(source)" -a -n "$(actor)" -a -n "$(reason)" || (echo 'usage: make acquisition-clear-cooldown source=<uuid> actor=<uuid> reason="..." [host=1]' && exit 1)
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py clear-cooldown --source $(source) --actor $(actor) --reason "$(reason)" $(if $(host),--include-host,)

acquisition-assist: ## What acquisition learned, for a source reviewer [moved=1]
	# Assistance only. Nothing here verifies, classifies or promotes a source.
	cd apps/api && PYTHONIOENCODING=utf-8 $(UV) run python scripts/acquisition.py assist $(if $(moved),--moved,) $(if $(university),--university $(university),)

migration: ## Create a revision: make migration m="add university table"
	@test -n "$(m)" || (echo 'usage: make migration m="description"' && exit 1)
	cd apps/api && $(UV) run alembic revision --autogenerate -m "$(m)"

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------
lint: lint-py lint-web ## Lint everything

lint-py: ## Lint and format-check Python
	$(UV) run ruff check .
	$(UV) run ruff format --check .

lint-web: ## Lint the frontend
	$(PNPM) run lint

format: ## Auto-fix Python lint and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: typecheck-py typecheck-web ## Type-check everything

typecheck-py: ## Type-check Python (mypy, strict)
	$(UV) run mypy

typecheck-web: ## Type-check the frontend
	$(PNPM) run typecheck

test: test-py test-web ## Run all tests

test-py: ## Run backend tests (integration tests skip without live services)
	$(TEST_DB_ENV) $(UV) run pytest

test-py-unit: ## Run backend tests excluding integration
	$(TEST_DB_ENV) $(UV) run pytest -m "not integration"

test-web: ## Run frontend tests
	$(PNPM) --filter @datahub/web run test

build-web: ## Production build of the frontend
	$(PNPM) --filter @datahub/web run build

check: lint typecheck test check-api-types ## Everything CI runs

verify-docker: ## Assert the whole Compose stack behaves (see ops/runbooks)
	bash scripts/verify-docker-stack.sh

# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------
generate-api-types: ## FastAPI -> openapi.json -> TypeScript types
	$(PNPM) run generate:api-types

check-api-types: ## Fail if the committed generated types are stale
	$(PNPM) run check:api-types

# ---------------------------------------------------------------------------
# Running on the host (against `make up-deps`)
# ---------------------------------------------------------------------------
api: ## Run the API on the host with reload
	cd apps/api && $(UV) run uvicorn app.main:app --reload --port 8000

worker: ## Run a Celery worker on the host
	cd apps/api && $(UV) run celery -A app.workers.celery_app worker --loglevel=info --concurrency=2

beat: ## Run Celery beat on the host
	cd apps/api && $(UV) run celery -A app.workers.celery_app beat --loglevel=info

shell-api: ## Shell into the running api container
	$(COMPOSE) exec api /bin/bash

shell-db: ## psql into the running database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-datahub} -d $${POSTGRES_DB:-datahub}
