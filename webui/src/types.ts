export type SortMode = "relevance" | "newest" | "oldest" | "created" | "updated" | "title";
export type PathMode = "current" | "all";
export type SearchScope = "all" | "title" | "message";
export type MatchMode = "contains" | "word";

export interface SearchFilters {
  role: "" | "user" | "assistant" | "system" | "developer" | "tool" | "tool/system";
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
  content_text: string;
  display_text: string;
  render_text: string;
  has_text: boolean;
  has_raw: boolean;
  raw_preview: string;
  content_hash: string | null;
  is_on_current_path: boolean;
  is_internal: boolean;
  is_empty_mapping_node?: boolean;
  highlight_ranges: HighlightRange[];
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
  content_text: string;
  snippet: string;
  is_on_current_path: boolean;
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

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  next_offset: number | null;
  visible_total?: number;
  empty_hidden_count?: number;
  internal_hidden_count?: number;
  technical_hidden_count?: number;
  selected_in_results?: boolean;
  selected_item?: T;
  db_ready?: boolean;
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
  database: { name: string; exists: boolean };
  schema_version: number;
  schema_compatible?: boolean;
  missing_tables?: string[];
  missing_columns?: Record<string, string[]>;
  fts5_available?: boolean;
  message_fts_available?: boolean;
  trigram_available?: boolean;
  web_trigram_indexed?: boolean;
  web_normalized_indexed?: boolean;
  web_normalized_trigram_indexed?: boolean;
  web_legacy_trigram_indexed?: boolean;
  web_index_metadata?: boolean;
}

export interface ImportJob {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "postcheck_failed" | "cancelled";
  stage: string;
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
  log_tail: string[];
}
