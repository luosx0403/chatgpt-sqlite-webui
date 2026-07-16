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
  create_time: number | null;
  update_time: number | null;
  current_node: string | null;
  source_file: string | null;
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
  author_name: string | null;
  create_time: number | null;
  update_time: number | null;
  content_type: string | null;
  display_text: string;
  display_text_truncated?: boolean;
  display_text_total_chars?: number;
  display_text_total_chars_exact?: boolean;
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
  snippet: string;
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
  max_chunk_chars: number;
  resolver_input_truncated: boolean;
  source: "canonical" | "raw_fallback" | "canonical_placeholder" | UnknownApiEnum;
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
    | "foreign_key_violation"
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
  foreign_key_violation_sample_limit?: number;
  foreign_key_violations_by_table?: Array<{ table: string; count: number }>;
  foreign_key_violation_samples?: Array<{ table: string; rowid: number | null; parent_table: string; constraint_index: number }>;
}

export interface CleanupWarning {
  code: string;
  error_type: string;
  path_kind: string;
}

export interface ImportJob {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "postcheck_failed";
  stage: string;
  outcome: "queued" | "import_running" | "import_job_start_failed" | "input_preflight_failed" | "source_scan_failed" | "source_read_failed" | "json_decode_failed" | "top_level_contract_failed" | "import_transaction_failed" | "canonical_commit_succeeded" | "verify_failed" | "stats_failed" | "web_index_failed" | "succeeded";
  canonical_commit_succeeded: boolean;
  filename: string;
  size: number;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  elapsed_seconds: number;
  summary: Record<string, unknown> | null;
  verify: Record<string, unknown> | null;
  stats: Stats | null;
  web_index: Record<string, unknown> | null;
  error: string | null;
  error_code: string | null;
  error_type: string | null;
  cleanup_warning: string | null;
  cleanup_warnings: CleanupWarning[];
  log_tail: string[];
}

export interface RawMessageResponse {
  conversation_id: string;
  node_id: string;
  raw_message: unknown;
  raw_text?: string;
  raw_size: number;
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
