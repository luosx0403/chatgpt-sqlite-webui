import type { CleanupWarning, ConversationPage, ConversationSummary, DisplayTextChunk, Health, ImportJob, MatchMode, MessageItem, MessagePage, PathMode, RawMessageResponse, SearchFilters, SearchMessageHit, SearchMessagePage, SortMode, Stats } from "../types";

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
    public readonly cleanupWarnings: CleanupWarning[] = [],
  ) {
    super(code);
    this.name = "ApiError";
  }

  get cleanupWarning(): string | null {
    return this.cleanupWarnings[0]?.code ?? null;
  }

  get cleanupErrorType(): string | null {
    return this.cleanupWarnings[0]?.error_type ?? null;
  }
}

const SAFE_DIAGNOSTIC = /^[A-Za-z0-9_:-]+$/;

function cleanupWarningFrom(value: unknown): CleanupWarning | null {
  if (!value || typeof value !== "object") return null;
  const item = value as { code?: unknown; error_type?: unknown; path_kind?: unknown };
  if (typeof item.code !== "string" || !SAFE_DIAGNOSTIC.test(item.code)) return null;
  const errorType = typeof item.error_type === "string" && SAFE_DIAGNOSTIC.test(item.error_type)
    ? item.error_type
    : "UnknownError";
  const pathKind = typeof item.path_kind === "string" && SAFE_DIAGNOSTIC.test(item.path_kind)
    ? item.path_kind
    : "import_job";
  return { code: item.code, error_type: errorType, path_kind: pathKind };
}

async function responseError(response: Response): Promise<ApiError> {
  let code = `http_${response.status}`;
  const cleanupWarnings: CleanupWarning[] = [];
  try {
    const payload = await response.json() as { detail?: unknown; code?: unknown };
    const nestedCode = payload.detail && typeof payload.detail === "object" && "code" in payload.detail
      ? (payload.detail as { code?: unknown }).code
      : undefined;
    const detail = typeof payload.detail === "string" ? payload.detail : payload.code ?? nestedCode;
    if (typeof detail === "string" && /^[a-z0-9_:-]+$/.test(detail)) code = detail;
    if (payload.detail && typeof payload.detail === "object") {
      const structured = payload.detail as { cleanup_warning?: unknown; cleanup_error_type?: unknown; cleanup_warnings?: unknown };
      if (Array.isArray(structured.cleanup_warnings)) {
        for (const value of structured.cleanup_warnings) {
          const warning = cleanupWarningFrom(value);
          if (warning && !cleanupWarnings.some((item) => item.code === warning.code && item.path_kind === warning.path_kind)) {
            cleanupWarnings.push(warning);
          }
        }
      }
      if (cleanupWarnings.length === 0 && typeof structured.cleanup_warning === "string" && SAFE_DIAGNOSTIC.test(structured.cleanup_warning)) {
        cleanupWarnings.push({
          code: structured.cleanup_warning,
          error_type: typeof structured.cleanup_error_type === "string" && SAFE_DIAGNOSTIC.test(structured.cleanup_error_type)
            ? structured.cleanup_error_type
            : "UnknownError",
          path_kind: "import_job",
        });
      }
    }
  } catch {
    // Keep the stable HTTP fallback; never expose server prose in the UI.
  }
  return new ApiError(code, response.status, cleanupWarnings);
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
  continuation?: string | null;
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
    continuation: args.continuation,
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
  return request<ConversationSummary>(`/api/by-id/conversation?${params({ conversation_id: id })}`, signal);
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
  return request<MessagePage>(`/api/by-id/messages?${params({ conversation_id: args.id })}&${query}`, args.signal);
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
  continuation?: string | null;
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
    count_total: args.countTotal === false ? "false" : undefined,
    continuation: args.continuation
  });
  return request<SearchMessagePage>(`/api/search/messages?${query}`, args.signal);
}

export function exportUrl(id: string, format: "md" | "txt", path: PathMode, includeInternal = false): string {
  const query = params({ conversation_id: id, format, path, include_internal: includeInternal });
  return `/api/by-id/export?${query}`;
}

export const MAX_BROWSER_COPY_BYTES = 16 * 1024 * 1024;
export const MAX_BROWSER_COPY_CHARS = 8 * 1024 * 1024;

export class CopyLimitError extends Error {
  constructor() {
    super("copy_limit_exceeded");
    this.name = "CopyLimitError";
  }
}

export class IncompleteDisplayRecoveryError extends Error {
  constructor() {
    super("display_recovery_incomplete");
    this.name = "IncompleteDisplayRecoveryError";
  }
}

export function isRecoverableDisplayCursorError(error: unknown): boolean {
  return error instanceof ApiError && (
    error.code === "invalid_display_cursor" ||
    error.code === "display_cursor_stale"
  );
}

export function assertBrowserCopyLimit(text: string): void {
  if (text.length > MAX_BROWSER_COPY_CHARS || new TextEncoder().encode(text).byteLength > MAX_BROWSER_COPY_BYTES) {
    throw new CopyLimitError();
  }
}

export async function getConversationCopyText(
  id: string,
  path: PathMode,
  includeInternal = false,
  signal?: AbortSignal,
): Promise<string> {
  const query = params({ conversation_id: id, path, include_internal: includeInternal });
  const response = await fetch(`/api/by-id/copy?${query}`, {
    signal,
    headers: { Accept: "text/plain" },
  });
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new CopyLimitError();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parts: string[] = [];
  let bytes = 0;
  let chars = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_BROWSER_COPY_BYTES) throw new CopyLimitError();
      const part = decoder.decode(value, { stream: true });
      chars += part.length;
      if (chars > MAX_BROWSER_COPY_CHARS) throw new CopyLimitError();
      parts.push(part);
    }
    const tail = decoder.decode();
    chars += tail.length;
    if (chars > MAX_BROWSER_COPY_CHARS) throw new CopyLimitError();
    parts.push(tail);
    return parts.join("");
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

export async function getVisibleMessagesCopyText(
  conversationId: string,
  nodeIds: string[],
  signal?: AbortSignal,
): Promise<string> {
  const response = await fetch(`/api/by-id/copy-visible?${params({ conversation_id: conversationId })}`, {
    method: "POST",
    signal,
    headers: { Accept: "text/plain", "Content-Type": "application/json" },
    body: JSON.stringify({ node_ids: nodeIds }),
  });
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new CopyLimitError();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parts: string[] = [];
  let bytes = 0;
  let chars = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_BROWSER_COPY_BYTES) throw new CopyLimitError();
      const part = decoder.decode(value, { stream: true });
      chars += part.length;
      if (chars > MAX_BROWSER_COPY_CHARS) throw new CopyLimitError();
      parts.push(part);
    }
    const tail = decoder.decode();
    chars += tail.length;
    if (chars > MAX_BROWSER_COPY_CHARS) throw new CopyLimitError();
    parts.push(tail);
    return parts.join("");
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

export function getRawMessage(conversationId: string, nodeId: string, signal?: AbortSignal, maxChars = 50000): Promise<RawMessageResponse> {
  const query = params({ conversation_id: conversationId, node_id: nodeId, max_chars: maxChars });
  return request<RawMessageResponse>(`/api/by-id/raw?${query}`, signal);
}

export function getMessageDisplayChunk(
  conversationId: string,
  nodeId: string,
  offset = 0,
  limit = 1048576,
  signal?: AbortSignal,
  cursor?: string | null,
  anchorCharOffset?: number | null,
): Promise<DisplayTextChunk> {
  const query = params({ conversation_id: conversationId, node_id: nodeId, offset, limit, cursor, anchor_char_offset: anchorCharOffset });
  return request<DisplayTextChunk>(`/api/by-id/display?${query}`, signal);
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

export async function cancelWebIndex(jobId: string, signal?: AbortSignal): Promise<ImportJob> {
  const response = await fetch(`/api/import/jobs/${encodeURIComponent(jobId)}/web-index/cancel`, {
    method: "POST",
    signal,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<ImportJob>;
}

export function getImportJobs(signal?: AbortSignal): Promise<{ items: ImportJob[] }> {
  return request<{ items: ImportJob[] }>("/api/import/jobs", signal);
}
