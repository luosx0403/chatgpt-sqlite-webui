export type SortMode = "relevance" | "newest" | "oldest" | "created" | "updated" | "title";
export type PathMode = "current" | "all";
export type SearchScope = "all" | "title" | "message";
export type MatchMode = "contains" | "word";
export type UnknownApiEnum = string & { readonly __unknownApiEnum?: never };

export interface SearchFilters {
  role: "" | "user" | "assistant" | "system" | "developer" | "tool" | "tool/system" | "tool_system";
  scope: SearchScope;
  title: string;
  exact: string;
  exclude: string;
  after: string;
  before: string;
  source: string;
}

export interface ConversationSummary {
  conversation_id: string;
  title: string | null;
  title_truncated?: boolean;
  title_length?: number;
  create_time: number | null;
  update_time: number | null;
  current_node: string | null;
  source_file: string | null;
  source_file_truncated?: boolean;
  source_file_length?: number;
  node_count?: number;
  current_path_nodes?: number;
  current_path_fallback_to_all?: boolean;
  current_node_exists?: boolean;
  current_collection_source?: "current_node" | "raw_flags" | "fallback_all" | UnknownApiEnum;
  effective_path?: PathMode;
  cycle_detected?: boolean;
  missing_parent?: boolean;
  cross_conversation_parent?: boolean;
  partial_chain?: boolean;
  raw_flag_leaf_count?: number;
  selected_chain_cycle_detected?: boolean;
  raw_flag_cycle_detected?: boolean;
  selected_chain_missing_parent?: boolean;
  raw_flag_missing_parent?: boolean;
  selected_chain_cross_conversation_parent?: boolean;
  raw_flag_cross_conversation_parent?: boolean;
  hit_count?: number;
  snippets?: SearchSnippet[];
  reasons?: string[];
  score?: number;
  message_match?: boolean;
  title_match?: boolean;
  has_title_hits?: boolean;
  has_internal_hits?: boolean;
  has_branch_hits?: boolean;
  enrichment_partial?: boolean;
}

export interface SearchSnippet {
  node_id: string;
  role: string | null;
  content_type?: string | null;
  snippet: string;
  is_on_current_path: boolean;
  current_path_fallback_to_all?: boolean;
  effective_visible_in_current_view?: boolean;
  is_internal?: boolean;
}

export interface MessageItem {
  conversation_id?: string;
  node_id: string;
  parent_node_id: string | null;
  message_id: string | null;
  role: string | null;
  role_truncated?: boolean;
  role_length?: number;
  author_name: string | null;
  author_name_truncated?: boolean;
  author_name_length?: number;
  create_time: number | null;
  update_time: number | null;
  content_type: string | null;
  content_type_truncated?: boolean;
  content_type_length?: number;
  display_text: string;
  display_text_truncated?: boolean;
  display_text_total_chars?: number;
  display_text_total_chars_exact?: boolean;
  display_text_resolver_input_truncated?: boolean;
  display_text_returned_chars?: number;
  has_text: boolean;
  has_raw: boolean;
  raw_preview: string;
  raw_preview_truncated?: boolean;
  raw_text?: string;
  content_hash: string | null;
  is_on_current_path: boolean;
  current_path_fallback_to_all?: boolean;
  effective_visible_in_current_view?: boolean;
  is_internal: boolean;
  is_empty_mapping_node?: boolean;
  highlight_ranges: HighlightRange[];
  highlight_ranges_truncated?: boolean;
  highlight_truncated?: boolean;
  highlight_scanned_chars?: number;
  highlight_range_limit_reached?: boolean;
  snippet?: string;
  title?: string | null;
  reasons?: string[];
  score?: number;
}

export interface SearchMessageHit {
  conversation_id: string;
  node_id: string;
  role: string | null;
  create_time: number | null;
  update_time: number | null;
  content_type: string | null;
  display_text: string;
  display_preview?: string;
  display_preview_truncated?: boolean;
  display_preview_returned_chars?: number;
  display_text_total_chars?: number;
  display_text_total_chars_exact?: boolean;
  snippet: string;
  match_char_offset?: number | null;
  match_length?: number | null;
  matched_term?: string | null;
  display_anchor_revision?: string | null;
  display_anchor_cursor?: string | null;
  is_on_current_path: boolean;
  current_path_fallback_to_all?: boolean;
  effective_visible_in_current_view?: boolean;
  is_internal: boolean;
  title: string | null;
  conversation_create_time?: number | null;
  conversation_update_time?: number | null;
  current_node?: string | null;
  source_file?: string | null;
  reasons?: string[];
  score?: number;
}

export interface HighlightRange {
  start: number;
  end: number;
}

export interface BasePage<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  next_offset: number | null;
  total_exact?: boolean;
  order_exact?: boolean;
  scan_complete?: boolean;
  provisional_order?: boolean;
  visible_total?: number;
  empty_hidden_count?: number;
  internal_hidden_count?: number;
  /** @deprecated Exact alias of internal_hidden_count for compatibility. */
  technical_hidden_count?: number;
  raw_size?: number;
  truncated?: boolean;
  diagnostics?: SearchDiagnostics;
  db_ready?: boolean;
  effective_path?: PathMode;
  current_path_fallback_to_all?: boolean;
  current_node_exists?: boolean;
  current_collection_source?: "current_node" | "raw_flags" | "fallback_all" | UnknownApiEnum;
  cycle_detected?: boolean;
  missing_parent?: boolean;
  cross_conversation_parent?: boolean;
  partial_chain?: boolean;
  raw_flag_leaf_count?: number;
  selected_chain_cycle_detected?: boolean;
  raw_flag_cycle_detected?: boolean;
  selected_chain_missing_parent?: boolean;
  raw_flag_missing_parent?: boolean;
  selected_chain_cross_conversation_parent?: boolean;
  raw_flag_cross_conversation_parent?: boolean;
  around_target_found?: boolean;
  around_target_visible?: boolean;
  around_target_in_effective_collection?: boolean;
  around_target_in_requested_collection?: boolean;
  around_target_applied?: boolean;
}

export interface ConversationPage extends BasePage<ConversationSummary> {
  selected_in_results?: boolean;
  selected_item?: ConversationSummary;
}

export interface MessagePage extends BasePage<MessageItem> {
  effective_path?: PathMode;
  current_path_fallback_to_all?: boolean;
  page_text_budget_exhausted?: boolean;
  page_preview_budget_exhausted?: boolean;
  page_highlight_budget_exhausted?: boolean;
  response_budget_estimated?: number;
  response_budget_limit?: number;
  response_budget_estimate_exhausted?: boolean;
}

export interface DisplayTextChunk {
  conversation_id: string;
  node_id: string;
  display_text: string;
  offset: number;
  returned_chars: number;
  total_chars: number;
  total_chars_exact: boolean;
  has_more: boolean;
  next_offset: number | null;
  next_cursor: string | null;
  content_revision: string | null;
  max_chunk_chars: number;
  resolver_input_truncated: boolean;
  source: "canonical" | "raw_fallback" | "canonical_placeholder" | UnknownApiEnum;
  anchor_char_offset?: number | null;
  anchor_offset_in_chunk?: number | null;
}

export interface SearchMessagePage extends BasePage<SearchMessageHit> {
  total_exact: boolean;
  effective_path?: PathMode;
  current_path_fallback_to_all?: boolean;
}

export interface SearchDiagnostics {
  candidate_backend?:
    | "normalized_trigram"
    | "normalized_title_trigram"
    | "normalized_scan"
    | "normalized_title_scan"
    | "full_scan"
    | UnknownApiEnum;
  web_index_missing?: boolean;
  normalized_trigram_available?: boolean;
  legacy_trigram_index?: boolean;
  legacy_fts_present?: boolean;
  short_query?: boolean;
  diagnostics_accuracy?: "best_effort" | UnknownApiEnum;
  actual_fallback_note?: string;
  estimated_backend_note?: string;
  resource_contract?: string;
  configured_candidate_scan_char_limit?: number;
  configured_verified_char_limit_per_candidate?: number;
  configured_verified_byte_limit_per_candidate?: number;
  max_observed_verified_chars_per_candidate?: number;
  max_observed_verified_bytes_per_candidate?: number;
  candidates_seen?: number;
  candidates_verified?: number;
  candidate_sql_rows?: number;
  blob_read_bytes?: number;
  temp_page_delta?: number;
  /** @deprecated Configured ceiling; use configured_candidate_scan_char_limit. */
  candidate_scan_chars_per_row?: number;
  hit_preview_chars?: number;
  snippet_scan_chars?: number;
  response_estimated_bytes?: number;
  response_estimated_bytes_limit?: number;
  partial_due_to_oversized_input?: boolean;
  partial?: boolean;
  partial_reason?: string | null;
  order_exact?: boolean;
  scan_complete?: boolean;
  provisional_order?: boolean;
  /** @deprecated Configured ceiling; use configured_verified_char_limit_per_candidate. */
  verified_chars_per_candidate?: number;
  /** @deprecated Configured ceiling; use configured_verified_byte_limit_per_candidate. */
  verified_bytes_per_candidate?: number;
  request_verified_bytes?: number;
  request_verified_chars?: number;
  request_verify_bytes_limit?: number;
  request_verify_chars_limit?: number;
  raw_fallback_bytes_per_row?: number;
  raw_fallback_chars_per_row?: number;
  verify_chunk_bytes?: number;
  oversized_candidates_seen?: number;
  oversized_candidates_verified?: number;
  oversized_candidates_pending?: number;
  candidate_count?: number;
  candidate_limit?: number;
  resolver_calls?: number;
  blob_reads?: number;
  candidate_blob_bytes?: number;
  raw_blob_bytes?: number;
  decoded_chars?: number;
  normalization_units?: number;
  sqlite_vm_steps?: number;
  wall_seconds?: number;
  continuation_available?: boolean;
  continuation_token?: string | null;
  completion_state?: "complete" | "partial" | UnknownApiEnum;
}

export interface Stats {
  db_ready?: boolean;
  conversations: number;
  nodes: number;
  current_path_nodes: number;
  warnings: number;
  earliest_create_time: number | null;
  latest_create_time: number | null;
  earliest_update_time: number | null;
  latest_update_time: number | null;
}

export interface Health {
  ok: boolean;
  db_ready?: boolean;
  readiness?:
    | "database_missing_or_uninitialized"
    | "migration_required"
    | "schema_newer"
    | "schema_incompatible"
    | "data_incompatible"
    | "foreign_key_violation"
    | "resource_contract_exceeded"
    | "database_malformed"
    | "database_locked"
    | "database_readonly_or_io"
    | "ready_empty"
    | "ready_with_data"
    | UnknownApiEnum;
  database_error_code?: string | null;
  database: { name: string; exists: boolean };
  schema_version: number;
  api_schema_version?: number;
  current_database_schema_version?: number | null;
  required_database_schema_version?: number;
  migration_required?: boolean;
  schema_compatible?: boolean;
  missing_tables?: string[];
  missing_columns?: Record<string, string[]>;
  invalid_tables?: Record<string, unknown>;
  object_type_mismatches?: Record<string, { expected: string; actual: string }>;
  missing_indexes?: string[];
  invalid_indexes?: Record<string, unknown>;
  missing_triggers?: string[];
  invalid_triggers?: Record<string, unknown>;
  missing_generation_rows?: string[];
  invalid_generation_rows?: Record<string, unknown>;
  missing_foreign_keys?: Record<string, Array<{ column: string; parent_table: string; parent_column: string; on_delete: string; on_update: string }>>;
  fts5_available?: boolean;
  message_fts_available?: boolean;
  message_fts_rebuildable?: boolean;
  message_fts_error?: string | null;
  optional_message_fts_error?: boolean;
  optional_message_fts_recovery_hint?: string;
  trigram_available?: boolean;
  web_trigram_indexed?: boolean;
  web_normalized_indexed?: boolean;
  web_normalized_trigram_indexed?: boolean;
  web_legacy_trigram_indexed?: boolean;
  web_index_metadata?: boolean;
  access_profile?: "loopback_local" | "remote_opt_in";
  remote_access?: boolean;
  allowed_hosts?: string[];
  trusted_proxies?: string[];
  write_origin_required?: boolean;
  foreign_key_violations?: number;
  foreign_key_violations_exact?: boolean;
  foreign_key_check_complete?: boolean;
  foreign_key_check_last_completed_at?: string | null;
  foreign_key_check_connection_data_version?: number | null;
  result_stale?: boolean;
  foreign_key_violation_sample_limit?: number;
  foreign_key_violations_by_table?: Array<{ table: string; count: number }>;
  foreign_key_violation_samples?: Array<{ table: string; rowid: number | null; parent_table: string; constraint_index: number }>;
  reader_resource_contract_checked?: boolean;
  reader_resource_contract_exact?: boolean;
  reader_resource_contract_violations?: number;
  reader_resource_contract_limit_nodes_per_conversation?: number;
}

export interface CleanupWarning {
  code: string;
  error_type: string;
  path_kind: string;
}

export interface ImportWarningCount {
  warning_type: string;
  count: number;
}

export interface ImportBatchResourceProfile {
  max_conversations: number;
  max_nodes: number;
  max_input_bytes: number;
  max_decoded_chars: number;
  max_raw_bytes: number;
  max_metadata_bytes: number;
  max_estimated_heap_bytes: number;
  max_sqlite_bind_bytes: number;
}

export interface ImportJsonResourceProfile {
  max_element_utf8_bytes: number;
  max_element_decoded_chars: number;
  max_string_primitive_tokens: number;
  max_mapping_entries: number;
  max_array_items: number;
  max_nesting_depth: number;
  max_integer_digits: number;
  max_estimated_decoded_heap_bytes: number;
  max_nodes_per_conversation: number;
  import_batch_materialized_bytes: number;
  import_batch: ImportBatchResourceProfile;
}

export interface ImportSummary {
  attempted_valid_conversations?: number;
  committed_conversations?: number;
  committed_nodes?: number;
  valid_conversations?: number;
  nodes?: number;
  skipped_invalid_elements?: number;
  warnings?: number;
  warnings_by_type?: ImportWarningCount[];
  unchanged_conversations?: number;
  inserted_conversations?: number;
  updated_conversations?: number;
  dirty_domains?: string[];
  json_resource_profile?: ImportJsonResourceProfile;
  import_batch_count?: number;
  import_batch_total_input_bytes?: number;
  import_batch_peak_input_bytes?: number;
  import_batch_peak_decoded_chars?: number;
  import_batch_peak_nodes?: number;
  import_batch_peak_raw_bytes?: number;
  import_batch_peak_metadata_bytes?: number;
  import_batch_peak_estimated_heap_bytes?: number;
  import_batch_peak_sqlite_bind_bytes?: number;
  [key: string]: unknown;
}

export interface ImportJob {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "postcheck_failed";
  stage: string;
  outcome: "queued" | "import_running" | "import_job_start_failed" | "input_preflight_failed" | "source_scan_failed" | "source_read_failed" | "json_decode_failed" | "top_level_contract_failed" | "import_transaction_failed" | "canonical_commit_succeeded" | "verify_failed" | "stats_failed" | "web_index_failed" | "web_index_cancelled" | "succeeded";
  completion_outcome: "queued" | "running" | "success" | "success_with_warnings" | "partial_success" | "failed_before_commit" | "failed_after_canonical_commit" | "cleanup_warning" | "cancelled";
  canonical_import_outcome: "queued" | "running" | "success" | "success_with_warnings" | "partial_success" | "failed_before_commit";
  canonical_commit_succeeded: boolean;
  filename: string;
  size: number;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  elapsed_seconds: number;
  summary: ImportSummary | null;
  verify: Record<string, unknown> | null;
  stats: Stats | null;
  web_index: Record<string, unknown> | null;
  web_index_cancel_requested: boolean;
  web_index_cancelled: boolean;
  error: string | null;
  error_code: string | null;
  error_type: string | null;
  cleanup_warning: string | null;
  cleanup_warnings: CleanupWarning[];
  stage_timings?: Record<string, number>;
  log_tail: string[];
}

export interface RawMessageResponse {
  conversation_id: string;
  node_id: string;
  raw_message: unknown;
  raw_text?: string;
  raw_size: number;
  raw_size_unit: "bytes" | UnknownApiEnum;
  raw_size_exact: boolean;
  raw_size_chars?: number | null;
  raw_size_chars_exact?: boolean;
  raw_size_bytes: number;
  raw_size_bytes_exact?: boolean;
  parsed?: boolean;
  incomplete?: boolean;
  error_code?: string;
  limit?: number;
  truncated: boolean;
}

export interface ApiSchemaResponse {
  version: number;
  pagination: {
    conversation_page: string[];
    message_page: string[];
    message_search_page: string[];
    total_exact: string;
  };
  conversations: {
    endpoint: string;
    filters: string[];
    response: string[];
    [key: string]: unknown;
  };
  messages: {
    endpoint: string;
    path_metadata: string[];
    around_node_id: { description: string; response: string[] };
    [key: string]: unknown;
  };
  raw: Record<string, unknown>;
  export: Record<string, unknown>;
  search: Record<string, unknown>;
  suggest: Record<string, unknown>;
  upload: { effective_policy: Record<string, unknown>; [key: string]: unknown };
  import_contract: Record<string, unknown>;
  jobs: {
    endpoints: string[];
    job_id: string;
    fields: string[];
    failure_codes: string[];
    cleanup_warnings: { item_fields: string[]; codes: string[] };
    preflight_cleanup_error: string[];
    [key: string]: unknown;
  };
  stable_error_codes: string[];
  provenance: Record<string, string>;
}
