import type { ConversationSummary, Health, ImportJob, MatchMode, MessageItem, Page, PathMode, SearchFilters, SearchMessageHit, SortMode, Stats } from "../types";

async function request<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, headers: { Accept: "application/json" } });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

function params(input: Record<string, string | number | boolean | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(input)) {
    if (value !== undefined && value !== null && value !== "") q.set(key, String(value));
  }
  return q.toString();
}

export function getStats(signal?: AbortSignal): Promise<Stats> {
  return request<Stats>("/api/stats", signal);
}

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return request<Health>("/api/health", signal);
}

export function getConversations(args: {
  q: string;
  sort: SortMode;
  path: PathMode;
  filters?: SearchFilters;
  limit?: number;
  offset?: number;
  selectedId?: string | null;
  matchMode?: MatchMode;
  signal?: AbortSignal;
}): Promise<Page<ConversationSummary>> {
  const query = params({
    q: args.q,
    sort: args.sort,
    path: args.path,
    limit: args.limit ?? 50,
    offset: args.offset ?? 0,
    selected_id: args.selectedId,
    role: args.filters?.role,
    scope: args.filters?.scope,
    title: args.filters?.title,
    exact: args.filters?.exact,
    exclude: args.filters?.exclude,
    after: args.filters?.after,
    before: args.filters?.before,
    source: args.filters?.source,
    match_mode: args.matchMode
  });
  return request<Page<ConversationSummary>>(`/api/conversations?${query}`, args.signal);
}

export function getConversation(id: string, signal?: AbortSignal): Promise<ConversationSummary> {
  return request<ConversationSummary>(`/api/conversations/${encodeURIComponent(id)}`, signal);
}

export function getMessages(args: {
  id: string;
  q: string;
  path: PathMode;
  filters?: SearchFilters;
  limit?: number;
  offset?: number;
  aroundNodeId?: string;
  matchMode?: MatchMode;
  signal?: AbortSignal;
}): Promise<Page<MessageItem>> {
  const query = params({
    q: args.q,
    path: args.path,
    limit: args.limit ?? 300,
    offset: args.offset ?? 0,
    around_node_id: args.aroundNodeId,
    role: args.filters?.role,
    title: args.filters?.title,
    scope: args.filters?.scope,
    exact: args.filters?.exact,
    exclude: args.filters?.exclude,
    after: args.filters?.after,
    before: args.filters?.before,
    source: args.filters?.source,
    match_mode: args.matchMode
  });
  return request<Page<MessageItem>>(`/api/conversations/${encodeURIComponent(args.id)}/messages?${query}`, args.signal);
}

export function getMessageHits(args: {
  q: string;
  conversationId: string;
  path: PathMode;
  order?: "relevance" | "display";
  limit?: number;
  offset?: number;
  filters?: SearchFilters;
  matchMode?: MatchMode;
  countTotal?: boolean;
  signal?: AbortSignal;
}): Promise<Page<SearchMessageHit>> {
  const query = params({
    q: args.q,
    conversation_id: args.conversationId,
    path: args.path,
    order: args.order,
    limit: args.limit ?? 100,
    offset: args.offset ?? 0,
    role: args.filters?.role,
    title: args.filters?.title,
    scope: args.filters?.scope,
    exact: args.filters?.exact,
    exclude: args.filters?.exclude,
    after: args.filters?.after,
    before: args.filters?.before,
    source: args.filters?.source,
    match_mode: args.matchMode,
    count_total: args.countTotal === false ? "false" : undefined
  });
  return request<Page<SearchMessageHit>>(`/api/search/messages?${query}`, args.signal);
}

export function exportUrl(id: string, format: "md" | "txt", path: PathMode, includeInternal = false): string {
  const query = params({ format, path, include_internal: includeInternal });
  return `/api/conversations/${encodeURIComponent(id)}/export?${query}`;
}

export function getRawMessage(conversationId: string, nodeId: string, signal?: AbortSignal): Promise<{ raw_message: unknown }> {
  return request<{ raw_message: unknown }>(`/api/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(nodeId)}/raw`, signal);
}

export async function uploadImportZip(file: File, signal?: AbortSignal): Promise<ImportJob> {
  const form = new FormData();
  form.set("file", file);
  const response = await fetch("/api/import/upload", { method: "POST", body: form, signal });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  return response.json() as Promise<ImportJob>;
}

export function getImportJob(jobId: string, signal?: AbortSignal): Promise<ImportJob> {
  return request<ImportJob>(`/api/import/jobs/${encodeURIComponent(jobId)}`, signal);
}

export function getImportJobs(signal?: AbortSignal): Promise<Page<ImportJob> | { items: ImportJob[] }> {
  return request<{ items: ImportJob[] }>("/api/import/jobs", signal);
}
