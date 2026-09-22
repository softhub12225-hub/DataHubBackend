/**
 * Shapes the review API returns.
 *
 * Hand-written, unlike the generated `@datahub/api-types`: several console endpoints
 * return `dict[str, Any]` because they assemble dataclasses from the verification domain,
 * and the generated document therefore describes them only as objects. Declaring them
 * here keeps the console type-checked; the server remains the authority on their content.
 *
 * Nothing in this file encodes a *rule*. There is no "is this decision valid" type,
 * because the console never computes that -- it renders `PreviewResult.valid`, which the
 * server decided.
 */

export interface SessionInfo {
  user_id: string;
  email: string;
  display_name: string;
  roles: string[];
  permissions: string[];
  may_verify: boolean;
  expires_at: string;
}

export interface ManifestStatus {
  name: "DOMAINS" | "SOURCES" | "RESPONSIBILITIES";
  rows: number;
  present: boolean;
  matches: boolean;
  approved_sha256: string;
  actual_sha256: string | null;
}

export interface DashboardData {
  reviewer: { display_name: string; roles: string[] };
  pilot: {
    institutions: number;
    reviewed_domain_rows: number;
    physical_sources: number;
    responsibility_rows: number;
  };
  trust: {
    verified_domains: number;
    pilot_decisions: number;
    source_mappings: number;
    responsibility_decisions: number;
    promoted_mappings: number;
    eligible_sources: number;
  };
  safety: {
    field_claim: number;
    change_proposal: number;
    change_event: number;
    canonical_rows: number;
    all_zero: boolean;
  };
  manifests: ManifestStatus[];
  audit: { rows: number; chain_ok: boolean };
  blockers: string[];
}

export interface InstitutionSummary {
  institution_id: string;
  name: string;
  verified_domains: number;
  total_domains: number;
  pilot_rows: number;
  pilot_decided: number;
  registered_mappings: number;
  responsibility_decided: number;
  promoted: number;
  unresolved_sources: number;
}

export interface DomainRow {
  domain_id: string;
  host: string;
  verification_status: string;
  is_active: boolean;
  verified_by_name: string | null;
  verified_at: string | null;
}

export interface PilotSourceRow {
  pilot_source_id: string;
  source_ref: string;
  responsibility: string;
  degree_scope: string | null;
  url: string;
  host: string;
  verification_state: string;
  verification_reason: string | null;
  mapping_id: string | null;
  is_duplicate: boolean;
  duplicate_of: string | null;
  body_available: boolean;
  effective_url: string | null;
  redirected: boolean;
  http_status: number | null;
}

export interface ResponsibilityCard {
  mapping_id: string;
  source_ref: string;
  responsibility: string;
  requested_url: string;
  effective_url: string | null;
  requested_host: string;
  requested_host_authority: string;
  effective_host: string;
  effective_host_authority: string;
  redirected: boolean;
  body_available: boolean;
  verification_status: string;
  publication_eligibility: string;
  verified_by_name: string | null;
  promoted_source_id: string | null;
  manifest_bound: boolean;
  manifest_blocker: string | null;
}

export interface InstitutionDetail {
  institution_id: string;
  name: string;
  summary: Omit<InstitutionSummary, "institution_id" | "name">;
  domains: DomainRow[];
  sources: PilotSourceRow[];
  responsibilities: ResponsibilityCard[];
  manifest_sha256: string;
  decisions_offered: string[];
}

export interface MappingState {
  verification_status: string;
  publication_eligibility: string;
  verified_by: string | null;
  verified_by_name: string | null;
  is_active: boolean;
  promoted_source_id: string | null;
}

export interface Blocker {
  code: string;
  message: string;
}

export interface PreviewResult {
  mapping_id: string;
  decision: string;
  reason: string;
  /** The server's verdict. The console renders this; it never recomputes it. */
  valid: boolean;
  blockers: Blocker[];
  reviewer: { display_name: string; email: string };
  before: MappingState;
  after: MappingState | null;
  binding: {
    source_ref: string;
    institution: string;
    responsibility: string;
    url: string;
    manifest_sha256: string;
    access_class: string;
    page_evidence_available: boolean;
    same_page_as: string | null;
    matches: boolean;
  } | null;
  authority: {
    verdict: string;
    redirected: boolean;
    requested_host: string;
    requested_authority: string;
    effective_host: string;
    effective_authority: string;
    effective_url: string | null;
    redirect_chain: unknown[];
    note: string | null;
  } | null;
  would_append: string | null;
  creates_field_claim: boolean;
  modifies_canonical: boolean;
  promotes: boolean;
  /** Opaque and server-signed. The console stores it and echoes it; it cannot read it. */
  preview_token: string | null;
  issued_at: string | null;
}

export interface ApplyResult {
  operation: string;
  status: string;
  actor: string;
  source_ref: string;
  responsibility: string;
  mapping_id: string;
  previous_state: MappingState;
  current_state: MappingState;
  audit_seq: number;
  audit_chain_ok: boolean;
  publication_eligibility: string;
  promoted: boolean;
  field_claim_created: boolean;
  field_claim_total: number;
  canonical_unchanged: boolean;
}

export interface PromotionPreviewResult {
  mapping_id: string;
  source_ref: string;
  responsibility: string;
  valid: boolean;
  blockers: Blocker[];
  manifest_bound: boolean;
  manifest_note: string | null;
  authority: {
    verdict: string;
    redirected: boolean;
    requested_host: string;
    requested_authority: string;
    effective_host: string;
    effective_authority: string;
    note: string | null;
  };
  field_bindings_that_would_be_created: { entity_type: string; field_path: string }[];
  source_id: string | null;
  source_eligibility_before: string | null;
  source_eligibility_after: string | null;
  promoted_source_id_before: string | null;
  promoted_source_id_after: string | null;
  field_claim_remains_zero: boolean;
  canonical_unchanged: boolean;
  /** Server-signed and opaque. Present only on a valid preview; null when blocked. */
  preview_token: string | null;
  issued_at: string | null;
  note: string;
}

export interface PromotionApplyResult {
  operation: string;
  status: string;
  actor: string;
  mapping_id: string;
  source_id: string;
  source_ref: string;
  responsibility: string;
  promoted: boolean;
  promoted_source_id: string | null;
  publication_eligibility: string;
  mapping_eligibility: string;
  field_bindings_written: number;
  already_promoted: boolean;
  audit_seq: number;
  audit_chain_ok: boolean;
  field_claim: number;
  change_proposal: number;
  change_event: number;
  canonical_rows: number;
  canonical_unchanged: boolean;
}

export interface EvidenceResult {
  pilot_source_id: string;
  source_ref: string;
  institution: string;
  responsibility: string;
  degree_scope: string | null;
  duplicate_of: string | null;
  requested_url: string;
  effective_url: string | null;
  requested_host: string;
  host_status: string | null;
  verification_state: string;
  snapshot_id: string | null;
  extraction_id: string | null;
  document_artifact_version: string | null;
  extraction_status: string | null;
  media_type: string | null;
  http_status: number | null;
  fetch_status: string | null;
  error_class: string | null;
  access_class: string;
  warnings: string[];
  available: boolean;
  body_status: string;
  body: string | null;
  blocks?: number;
  tables?: number;
  note?: string;
  same_page_note?: string;
  chrome_filter_note?: string;
}

export interface AuditEntry {
  seq: number;
  occurred_at: string;
  action: string;
  group: string;
  actor_name: string | null;
  actor_type: string;
  object_type: string;
  object_id: string | null;
  source_ref: string | null;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  reason: string | null;
  is_historical_registration: boolean;
}

export interface OperationEntry {
  seq: number;
  occurred_at: string;
  operation: string;
  group: string;
  actor_name: string | null;
  source_ref: string | null;
  object_type: string;
  object_id: string | null;
  applied: boolean;
  audit_seq: number;
  is_historical_registration: boolean;
}


// ---------------------------------------------------------------------------
// Step 5C.9: candidate scope and conflict resolution
// ---------------------------------------------------------------------------

export interface ScopeCriterion {
  dimension_code: string;
  operator: string;
  value?: string | null;
  value_ref?: string | null;
}

export interface CandidateScope {
  machine_resolution: string;
  machine_raw_text: string | null;
  /** Dimension A: where the applicant is from, or their fee status. */
  jurisdiction_hints: string[];
  /** Dimension B: what the applicant holds. Never merged with A. */
  qualification_hints: string[];
  category_hints: string[];
  hint_source: string | null;
  suggested_criteria: ScopeCriterion[];
  human_state: string;
  human_criteria: ScopeCriterion[];
  human_reason: string | null;
  human_actor: string | null;
  resolved: boolean;
  scopeable: boolean;
}

export interface CandidateConflict {
  verdict: string;
  context_fingerprint: string | null;
  member_count: number;
  action: string | null;
  actor: string | null;
  resolved: boolean;
}

export interface CandidateRow {
  candidate_id: string;
  field_kind: string;
  responsibility: string;
  institution: string;
  source_ref: string;
  pilot_source_id: string;
  mapping_id: string | null;
  mapping_status: string | null;
  source_authority: string;
  requested_url: string;
  effective_url: string | null;
  requested_host: string;
  extracted_value: string;
  normalized_value: Record<string, unknown> | null;
  evidence_excerpt: string;
  locator: Record<string, unknown>;
  heading_path: string[];
  confidence: string;
  confidence_reason: string | null;
  unresolved_reason: string | null;
  rule_version: string;
  artifact_version: string;
  review_decision: string;
  scope: CandidateScope;
  conflict: CandidateConflict;
  blockers: string[];
  promotion_ready: boolean;
}

export interface CandidateQueue {
  institution_id: string;
  institution: string;
  total_current: number;
  shown: number;
  scope_states: string[];
  conflict_actions: string[];
  dimensions: string[];
  jurisdiction_dimensions: string[];
  qualification_dimensions: string[];
  candidates: CandidateRow[];
}

export interface CandidateGroup {
  context_fingerprint: string;
  institution: string;
  field_kind: string;
  verdict: string;
  needs_resolution: boolean;
  resolved: boolean;
  action: string | null;
  actor: string | null;
  members: {
    candidate_id: string;
    source_ref: string;
    value_raw_text: string;
    value_normalized: Record<string, unknown> | null;
    confidence_band: string;
    requested_url: string;
    review_decision: string;
    human_scope_state: string;
  }[];
}

export interface ScopePreviewResult {
  candidate_id: string;
  state: string;
  criteria: ScopeCriterion[];
  reason: string;
  valid: boolean;
  blockers: Blocker[];
  reviewer: { display_name: string };
  before: { scope_state: string; criteria: ScopeCriterion[]; promotion_blockers: string[] };
  after: { scope_state: string; criteria: ScopeCriterion[]; promotion_blockers: string[] };
  would_append: string | null;
  creates_field_claim: boolean;
  modifies_canonical: boolean;
  preview_token: string | null;
  issued_at: string | null;
}

export interface ConflictPreviewResult {
  context_fingerprint: string;
  field_kind: string;
  verdict: string;
  action: string;
  selected_candidate_id: string | null;
  member_candidate_ids: string[];
  reason: string;
  valid: boolean;
  blockers: Blocker[];
  reviewer: { display_name: string };
  before_action: string | null;
  would_append: string | null;
  creates_field_claim: boolean;
  modifies_canonical: boolean;
  preview_token: string | null;
  issued_at: string | null;
}

export interface ResolutionResult {
  operation: string;
  status: string;
  actor: string;
  candidate_id: string | null;
  context_fingerprint: string | null;
  action: string;
  previous_state: string;
  current_state: string;
  promotion_blockers_now: string[];
  audit_seq: number;
  audit_chain_ok: boolean;
  field_claim: number;
  canonical_rows: number;
  canonical_unchanged: boolean;
}

export interface ReadinessMatrix {
  institution: string;
  current_candidates: number;
  ready: number;
  scope_unresolved: number;
  conflict_unresolved: number;
  blocker_counts: Record<string, number>;
  by_field_kind: Record<string, { current: number; ready: number }>;
}
