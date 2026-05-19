export type SortMode = "relevance" | "newest" | "oldest" | "title";
export type PathMode = "current" | "all";
export type SearchScope = "all" | "title" | "message";

export interface SearchFilters {
  role: "" | "user" | "assistant" | "developer" | "tool/system";
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
}

export interface SearchSnippet {
  node_id: string;
  role: string | null;
  snippet: string;
  is_on_current_path: boolean;
}

export interface MessageItem {
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
  content_hash: string;
  is_on_current_path: boolean;
  is_internal: boolean;
  highlight_ranges: HighlightRange[];
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
  selected_in_results?: boolean;
  db_ready?: boolean;
}

export interface Stats {
  db_ready?: boolean;
  conversations: number;
  nodes: number;
  current_path_nodes: number;
  warnings: number;
  earliest_create_time: number | null;
  latest_update_time: number | null;
}

export interface Health {
  ok: boolean;
  db_ready?: boolean;
  database: { name: string; exists: boolean };
  schema_version: number;
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
