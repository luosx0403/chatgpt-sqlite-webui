import type { ConversationPage, ConversationSummary, DisplayTextChunk, Health, ImportJob, MatchMode, MessageItem, MessagePage, PathMode, RawMessageResponse, SearchFilters, SearchMessageHit, SearchMessagePage, SortMode, Stats } from "../types";

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
    public readonly cleanupWarning: string | null = null,
    public readonly cleanupErrorType: string | null = null,
  ) {
    super(code);
    this.name = "ApiError";
  }
}

async function responseError(response: Response): Promise<ApiError> {
  let code = `http_${response.status}`;
  let cleanupWarning: string | null = null;
  let cleanupErrorType: string | null = null;
  try {
    const payload = await response.json() as { detail?: unknown; code?: unknown };
    const nestedCode = payload.detail && typeof payload.detail === "object" && "code" in payload.detail
      ? (payload.detail as { code?: unknown }).code
      : undefined;
    const detail = typeof payload.detail === "string" ? payload.detail : payload.code ?? nestedCode;
    if (typeof detail === "string" && /^[a-z0-9_:-]+$/.test(detail)) code = detail;
    if (payload.detail && typeof payload.detail === "object") {
      const structured = payload.detail as { cleanup_warning?: unknown; cleanup_error_type?: unknown };
      if (typeof structured.cleanup_warning === "string") cleanupWarning = structured.cleanup_warning;
      if (typeof structured.cleanup_error_type === "string") cleanupErrorType = structured.cleanup_error_type;
    }
  } catch {
    // Keep the stable HTTP fallback; never expose server prose in the UI.
  }
  return new ApiError(code, response.status, cleanupWarning, cleanupErrorType);
}

async function request<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw await responseError(response);
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
}): Promise<ConversationPage> {
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
  return request<ConversationPage>(`/api/conversations?${query}`, args.signal);
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
  includeInternal?: boolean;
  matchMode?: MatchMode;
  signal?: AbortSignal;
}): Promise<MessagePage> {
  const query = params({
    q: args.q,
    path: args.path,
    limit: args.limit ?? 300,
    offset: args.offset ?? 0,
    around_node_id: args.aroundNodeId,
    include_internal: args.includeInternal,
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
  return request<MessagePage>(`/api/conversations/${encodeURIComponent(args.id)}/messages?${query}`, args.signal);
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
}): Promise<SearchMessagePage> {
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
  return request<SearchMessagePage>(`/api/search/messages?${query}`, args.signal);
}

export function exportUrl(id: string, format: "md" | "txt", path: PathMode, includeInternal = false): string {
  const query = params({ format, path, include_internal: includeInternal });
  return `/api/conversations/${encodeURIComponent(id)}/export?${query}`;
}

export async function getConversationCopyText(
  id: string,
  path: PathMode,
  includeInternal = false,
  signal?: AbortSignal,
): Promise<string> {
  const query = params({ path, include_internal: includeInternal });
  const response = await fetch(`/api/conversations/${encodeURIComponent(id)}/copy?${query}`, {
    signal,
    headers: { Accept: "text/plain" },
  });
  if (!response.ok) throw await responseError(response);
  return response.text();
}

export function getRawMessage(conversationId: string, nodeId: string, signal?: AbortSignal, maxChars = 50000): Promise<RawMessageResponse> {
  return request<RawMessageResponse>(`/api/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(nodeId)}/raw?max_chars=${maxChars}`, signal);
}

export function getMessageDisplayChunk(
  conversationId: string,
  nodeId: string,
  offset = 0,
  limit = 65536,
  signal?: AbortSignal,
): Promise<DisplayTextChunk> {
  const query = params({ offset, limit });
  return request<DisplayTextChunk>(`/api/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(nodeId)}/display?${query}`, signal);
}

export async function uploadImportZip(file: File, signal?: AbortSignal): Promise<ImportJob> {
  const form = new FormData();
  form.set("file", file);
  const response = await fetch("/api/import/upload", { method: "POST", body: form, signal });
  if (!response.ok) {
    throw await responseError(response);
  }
  return response.json() as Promise<ImportJob>;
}

export function getImportJob(jobId: string, signal?: AbortSignal): Promise<ImportJob> {
  return request<ImportJob>(`/api/import/jobs/${encodeURIComponent(jobId)}`, signal);
}

export function getImportJobs(signal?: AbortSignal): Promise<{ items: ImportJob[] }> {
  return request<{ items: ImportJob[] }>("/api/import/jobs", signal);
}
