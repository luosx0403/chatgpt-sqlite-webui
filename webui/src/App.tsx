import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Sidebar from "./components/Sidebar";
import ConversationPane from "./components/ConversationPane";
import SearchHelp from "./components/SearchHelp";
import SettingsPanel from "./components/SettingsPanel";
import { ApiError, getConversation, getConversations, getHealth, getImportJob, getStats, uploadImportZip } from "./api/client";
import { applySettings, clampSettings, loadSettings, saveSettings, type Settings } from "./settings";
import { createTranslator } from "./i18n";
import type { ConversationSummary, Health, ImportJob, MatchMode, PathMode, SearchDiagnostics, SearchFilters, SearchScope, SortMode, Stats } from "./types";
import { isInteractiveTarget } from "./utils/interaction";

const DEFAULT_FILTERS: SearchFilters = {
  role: "",
  scope: "all",
  title: "",
  exact: "",
  exclude: "",
  after: "",
  before: "",
  source: ""
};
const MATCH_MODE_KEY = "chatgptArchiveWeb.searchMatchMode.v1";

function mergeConversationSearchMeta(detail: ConversationSummary, meta?: ConversationSummary | null): ConversationSummary {
  if (!meta || meta.conversation_id !== detail.conversation_id) return detail;
  return {
    ...detail,
    hit_count: meta.hit_count,
    snippets: meta.snippets,
    reasons: meta.reasons,
    score: meta.score,
    message_match: meta.message_match,
    title_match: meta.title_match,
    has_title_hits: meta.has_title_hits,
    has_internal_hits: meta.has_internal_hits,
    has_branch_hits: meta.has_branch_hits,
  };
}

function clearConversationSearchMeta(item: ConversationSummary): ConversationSummary {
  const {
    hit_count,
    snippets,
    reasons,
    score,
    message_match,
    title_match,
    has_title_hits,
    has_internal_hits,
    has_branch_hits,
    ...detail
  } = item;
  void hit_count;
  void snippets;
  void reasons;
  void score;
  void message_match;
  void title_match;
  void has_title_hits;
  void has_internal_hits;
  void has_branch_hits;
  return detail;
}

function loadMatchMode(): MatchMode {
  const params = new URLSearchParams(window.location.search);
  const urlMode = params.get("match_mode") || params.get("matchMode");
  if (urlMode === "word" || urlMode === "contains") return urlMode;
  try {
    return localStorage.getItem(MATCH_MODE_KEY) === "word" ? "word" : "contains";
  } catch {
    return "contains";
  }
}

function readUrlParams(): {
  query: string;
  sort: SortMode;
  path: PathMode;
  matchMode: MatchMode;
  selectedId: string | null;
  filters: SearchFilters;
} {
  const params = new URLSearchParams(window.location.search);
  const query = params.get("q") || "";
  const sort = (params.get("sort") as SortMode) || "relevance";
  const path = (params.get("path") as PathMode) || "current";
  const matchMode = loadMatchMode();
  const selectedId = params.get("conversation") || null;
  const allowedSorts: SortMode[] = ["relevance", "newest", "oldest", "updated", "created", "title"];
  const allowedPaths: PathMode[] = ["current", "all"];
  const allowedScopes: SearchScope[] = ["all", "title", "message"];
  const allowedRoles: string[] = ["", "user", "assistant", "tool", "system", "developer", "tool/system", "tool_system"];
  const filters: SearchFilters = { ...DEFAULT_FILTERS };
  if (params.has("role")) {
    const rawRole = params.get("role") || "";
    filters.role = (allowedRoles.includes(rawRole) ? rawRole : "") as SearchFilters["role"];
  }
  if (params.has("scope")) {
    const rawScope = params.get("scope") || "all";
    filters.scope = (allowedScopes.includes(rawScope as SearchScope) ? rawScope : "all") as SearchScope;
  }
  if (params.has("title")) filters.title = params.get("title") || "";
  if (params.has("exact")) filters.exact = params.get("exact") || "";
  if (params.has("exclude")) filters.exclude = params.get("exclude") || "";
  if (params.has("after")) filters.after = params.get("after") || "";
  if (params.has("before")) filters.before = params.get("before") || "";
  if (params.has("source")) filters.source = params.get("source") || "";
  return {
    query,
    sort: allowedSorts.includes(sort) ? sort : "relevance",
    path: allowedPaths.includes(path) ? path : "current",
    matchMode,
    selectedId,
    filters,
  };
}

function syncUrlState(state: {
  q: string;
  sort: string;
  path: string;
  matchMode: string;
  selectedId: string | null;
  filters: SearchFilters;
  layout: string;
  showInternal: boolean;
}) {
  try {
    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.sort && state.sort !== "relevance") params.set("sort", state.sort);
    if (state.path && state.path !== "current") params.set("path", state.path);
    if (state.matchMode && state.matchMode !== "contains") params.set("match_mode", state.matchMode);
    if (state.selectedId) params.set("conversation", state.selectedId);
    if (state.layout && state.layout !== "chat") params.set("layout", state.layout);
    if (state.showInternal) params.set("show_internal", "true");
    if (state.filters.role) params.set("role", state.filters.role);
    if (state.filters.scope && state.filters.scope !== "all") params.set("scope", state.filters.scope);
    if (state.filters.title) params.set("title", state.filters.title);
    if (state.filters.exact) params.set("exact", state.filters.exact);
    if (state.filters.exclude) params.set("exclude", state.filters.exclude);
    if (state.filters.after) params.set("after", state.filters.after);
    if (state.filters.before) params.set("before", state.filters.before);
    if (state.filters.source) params.set("source", state.filters.source);
    const query = params.toString();
    const url = query ? `${window.location.pathname}?${query}` : window.location.pathname;
    if (url !== `${window.location.pathname}${window.location.search}`) {
      // Keep refresh/share URLs current without creating a history entry per keystroke.
      // Back/Forward restoration should be added with deliberate pushState boundaries
      // for submitted searches or conversation selection, not from this hot path.
      window.history.replaceState(null, "", url);
    }
  } catch {
    // URL sync is best-effort.
  }
}

function canonicalShareUrl(state: {
  q: string;
  sort: string;
  path: string;
  matchMode: string;
  selectedId: string | null;
  filters: SearchFilters;
  layout: string;
  showInternal: boolean;
}): string {
  const url = new URL(window.location.href);
  const params = new URLSearchParams();
  params.set("sort", state.sort || "relevance");
  params.set("path", state.path || "current");
  params.set("match_mode", state.matchMode || "contains");
  params.set("layout", state.layout || "chat");
  params.set("show_internal", state.showInternal ? "true" : "false");
  params.set("scope", state.filters.scope || "all");
  if (state.q) params.set("q", state.q);
  if (state.selectedId) params.set("conversation", state.selectedId);
  if (state.filters.role) params.set("role", state.filters.role);
  if (state.filters.title) params.set("title", state.filters.title);
  if (state.filters.exact) params.set("exact", state.filters.exact);
  if (state.filters.exclude) params.set("exclude", state.filters.exclude);
  if (state.filters.after) params.set("after", state.filters.after);
  if (state.filters.before) params.set("before", state.filters.before);
  if (state.filters.source) params.set("source", state.filters.source);
  url.search = params.toString();
  url.hash = "";
  return url.toString();
}

function importStageLabel(t: (key: string) => string, stage: string): string {
  const normalized = stage.toLowerCase().replace(/[-\s]+/g, "_");
  const labels: Record<string, string> = {
    queued: t("stageQueued"),
    inspect: t("stageInspect"),
    inspecting: t("stageInspect"),
    validating_upload: t("stageInspect"),
    import: t("stageImport"),
    importing: t("stageImport"),
    source_scan_complete: t("stageImport"),
    shard_complete: t("stageImport"),
    fts_rebuild_complete: t("stageImport"),
    import_index_rebuild_complete: t("stageImport"),
    pragma_optimize_complete: t("stageImport"),
    verify: t("stageVerify"),
    verifying: t("stageVerify"),
    stats: t("stageStats"),
    web_index: t("stageWebIndex"),
    web_indexing: t("stageWebIndex"),
    webindex: t("stageWebIndex"),
    web_index_recovery: t("stageWebIndexRecovery"),
    webindex_recovery: t("stageWebIndexRecovery"),
    finished: t("stageFinished"),
    succeeded: t("stageFinished"),
    postcheck_failed: t("stagePostcheckFailed"),
    verify_failed: t("importError_verify_failed"),
    stats_failed: t("importError_stats_failed"),
    web_index_failed: t("importError_web_index_failed"),
    input_preflight: t("stageImport"),
    source_scan: t("stageImport"),
    json_decode: t("stageImport"),
    top_level_contract: t("stageImport"),
    transaction: t("stageImport"),
    failed: t("importError_import_transaction_failed"),
  };
  return labels[normalized] || t("stageUnknown");
}

function importErrorLabel(t: (key: string) => string, code: string | null | undefined): string {
  const direct = new Set([
    "no_conversation_sources",
    "ambiguous_conversation_sources",
    "source_scan_failed",
    "invalid_conversation_json",
    "non_finite_json_number",
    "conversation_json_top_level_not_list",
    "import_transaction_failed",
    "verify_failed",
    "stats_failed",
    "web_index_failed",
    "import_job_active",
    "upload_zip_no_conversation_sources",
    "upload_zip_ambiguous_conversation_sources",
    "upload_origin_not_allowed",
    "upload_origin_required",
  ]);
  if (code && direct.has(code)) return t(`importError_${code}`);
  if (code === "upload_content_length_required") return t("importError_upload_content_length_required");
  if (code && (code.includes("too_large") || code.includes("too_many") || code.includes("compression_ratio"))) return t("importError_upload_limits");
  if (code && (code.includes("not_zip") || code.includes("invalid_zip") || code.includes("valid_zip"))) return t("importError_invalid_zip");
  return t("importError_unknown");
}

export default function App() {
  const urlState = useRef(readUrlParams()).current;
  const [query, setQuery] = useState(urlState.query);
  const [debouncedQuery, setDebouncedQuery] = useState(urlState.query);
  const [sort, setSort] = useState<SortMode>(urlState.sort || "relevance");
  const [path, setPath] = useState<PathMode>(urlState.path || "current");
  const [matchMode, setMatchModeState] = useState<MatchMode>(urlState.matchMode);
  const [filters, setFilters] = useState<SearchFilters>(urlState.filters);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(urlState.selectedId);
  const [selected, setSelected] = useState<ConversationSummary | null>(null);
  const [total, setTotal] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [hasMore, setHasMore] = useState(false);
  const [stats, setStats] = useState<Stats | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importJob, setImportJob] = useState<ImportJob | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [uploadingImport, setUploadingImport] = useState(false);
  const [settings, setSettings] = useState<Settings>(() => loadSettings());
  const [showInternal, setShowInternal] = useState(() => new URLSearchParams(window.location.search).get("show_internal") === "true" || (new URLSearchParams(window.location.search).get("show_internal") === null && loadSettings().showInternalDefault));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [storageWarning, setStorageWarning] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [diagnostics, setDiagnostics] = useState<SearchDiagnostics | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const listRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const detailControllerRef = useRef<AbortController | null>(null);
  const listControllerRef = useRef<AbortController | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const selectedRef = useRef<ConversationSummary | null>(null);
  const importPollGenerationRef = useRef(0);
  const importRefreshDoneRef = useRef<string | null>(null);
  const filtersKey = JSON.stringify(filters);
  const searchContextKey = JSON.stringify({ q: debouncedQuery, sort, path, matchMode, filters, listPageSize: settings.listPageSize });
  const searchContextRef = useRef(searchContextKey);
  searchContextRef.current = searchContextKey;
  const { t } = useMemo(() => createTranslator(settings.language), [settings.language]);

  useEffect(() => {
    syncUrlState({ q: debouncedQuery, sort, path, matchMode, selectedId, filters, layout: settings.messageLayout, showInternal });
  }, [debouncedQuery, sort, path, matchMode, selectedId, filtersKey, settings.messageLayout, showInternal]);

  const setMatchMode = useCallback((value: MatchMode) => {
    setMatchModeState(value);
    try {
      localStorage.setItem(MATCH_MODE_KEY, value);
    } catch {
      // Search preference is best-effort local UI state.
    }
  }, []);

  const updateSettings = useCallback((next: Settings) => {
    const clamped = clampSettings(next);
    setSettings(clamped);
    setStorageWarning(!saveSettings(clamped));
    applySettings(clamped);
  }, []);

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), 220);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal).then(setHealth).catch(() => undefined);
    getStats(controller.signal).then(setStats).catch(() => undefined);
    return () => controller.abort();
  }, []);

  const refreshArchiveState = useCallback(() => {
    const controller = new AbortController();
    getHealth(controller.signal).then(setHealth).catch(() => undefined);
    getStats(controller.signal).then(setStats).catch(() => undefined);
    return controller;
  }, []);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const interactive = isInteractiveTarget(event.target);
      if (event.key === "Escape") {
        if (settingsOpen) setSettingsOpen(false);
        else if (helpOpen) setHelpOpen(false);
        else if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      }
      if ((!interactive && !event.metaKey && !event.ctrlKey && !event.altKey && event.key === "/") || ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k")) {
        event.preventDefault();
        document.getElementById("global-search")?.focus();
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const activeElement = document.activeElement instanceof Element ? document.activeElement : null;
      const sidebarNavigation = Boolean(activeElement?.closest(".sidebar"));
      if (!interactive && sidebarNavigation && !settingsOpen && !helpOpen) {
        if (event.key === "ArrowDown" || event.key.toLowerCase() === "j") {
          event.preventDefault();
          setFocusIndex((value) => Math.min(conversations.length - 1, value + 1));
        }
        if (event.key === "ArrowUp" || event.key.toLowerCase() === "k") {
          event.preventDefault();
          setFocusIndex((value) => Math.max(0, value - 1));
        }
        if (event.key === "Enter" && conversations[focusIndex]) {
          event.preventDefault();
          selectConversation(conversations[focusIndex].conversation_id);
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [conversations, focusIndex, settingsOpen, helpOpen]);

  const loadConversationDetail = useCallback((id: string, local?: ConversationSummary, requestedContextKey = searchContextRef.current) => {
    setSelectedId(id);
    selectedIdRef.current = id;
    if (local) setSelected(local);
    detailControllerRef.current?.abort();
    const controller = new AbortController();
    detailControllerRef.current = controller;
    const requestId = ++detailRequestRef.current;
    getConversation(id, controller.signal)
      .then((detail) => {
        if (requestId === detailRequestRef.current && selectedIdRef.current === id && searchContextRef.current === requestedContextKey) {
          setSelected(mergeConversationSearchMeta(detail, local || null));
        }
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError" && requestId === detailRequestRef.current && selectedIdRef.current === id && searchContextRef.current === requestedContextKey) setError(t("requestFailed"));
      });
  }, [t]);

  const loadConversationPage = useCallback((offset: number, append: boolean) => {
    if (!append) listControllerRef.current?.abort();
    const controller = new AbortController();
    if (!append) listControllerRef.current = controller;
    const requestId = ++listRequestRef.current;
    const requestedSelectedId = selectedIdRef.current;
    const requestedContextKey = searchContextKey;
    if (append) setLoadingMore(true);
    else setLoading(true);
    setError(null);
    getConversations({
      q: debouncedQuery,
      sort,
      path,
      filters,
      matchMode,
      offset,
      limit: settings.listPageSize,
      selectedId: requestedSelectedId,
      signal: controller.signal
    })
      .then((page) => {
        if (requestId !== listRequestRef.current || searchContextRef.current !== requestedContextKey) return;
        setConversations((current) => {
          const merged = append ? [...current, ...page.items] : page.items;
          const seen = new Set<string>();
          return merged.filter((item) => {
            if (seen.has(item.conversation_id)) return false;
            seen.add(item.conversation_id);
            return true;
          });
        });
        if (!append) setFocusIndex(0);
        setTotal(page.total);
        setHasMore(page.has_more);
        setNextOffset(page.next_offset);
        if (append) return;
        setDiagnostics(page.diagnostics ?? null);
        if (selectedIdRef.current !== requestedSelectedId) return;
        const selectedStillMatches = requestedSelectedId ? page.selected_in_results !== false : false;
        if (selectedStillMatches && requestedSelectedId) {
          const localSelected = page.items.find((item) => item.conversation_id === requestedSelectedId) || page.selected_item;
          if (!selectedRef.current || selectedRef.current.conversation_id !== requestedSelectedId) {
            loadConversationDetail(requestedSelectedId, localSelected, requestedContextKey);
          } else if (localSelected) {
            setSelected((current) => current?.conversation_id === requestedSelectedId ? { ...current, ...localSelected } : current);
          } else {
            setSelected((current) => current?.conversation_id === requestedSelectedId ? clearConversationSearchMeta(current) : current);
          }
        } else {
          const first = page.items[0] ?? null;
          selectedIdRef.current = first?.conversation_id ?? null;
          setSelectedId(first?.conversation_id ?? null);
          setSelected(first);
        }
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError" && requestId === listRequestRef.current) setError(t("requestFailed"));
      })
      .finally(() => {
        if (requestId === listRequestRef.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      });
    return controller;
  }, [debouncedQuery, sort, path, matchMode, filtersKey, loadConversationDetail, settings.listPageSize, searchContextKey, t]);

  useEffect(() => {
    const controller = loadConversationPage(0, false);
    return () => controller.abort();
  }, [loadConversationPage]);

  useEffect(() => {
    if (!importJob || !["queued", "running"].includes(importJob.status)) return;
    const jobId = importJob.job_id;
    const generation = ++importPollGenerationRef.current;
    const controller = new AbortController();
    let timer: number | null = null;
    const poll = async () => {
      try {
        const job = await getImportJob(jobId, controller.signal);
        if (controller.signal.aborted || generation !== importPollGenerationRef.current) return;
        setImportJob(job);
        if (job.status === "succeeded" || job.status === "postcheck_failed" || job.status === "failed") {
          if (job.canonical_commit_succeeded && importRefreshDoneRef.current !== jobId) {
            importRefreshDoneRef.current = jobId;
            refreshArchiveState();
            loadConversationPage(0, false);
          }
          return;
        }
        timer = window.setTimeout(poll, 1200);
      } catch (err) {
        if (!controller.signal.aborted && generation === importPollGenerationRef.current) {
          setImportError(importErrorLabel(t, err instanceof ApiError ? err.code : null));
          timer = window.setTimeout(poll, 1200);
        }
      }
    };
    timer = window.setTimeout(poll, 1200);
    return () => {
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [importJob?.job_id, refreshArchiveState, loadConversationPage, t]);

  const selectConversation = (id: string) => {
    const local = conversations.find((item) => item.conversation_id === id);
    loadConversationDetail(id, local);
  };

  const copyCurrentUrl = async () => {
    try {
      await navigator.clipboard.writeText(canonicalShareUrl({
        q: query,
        sort,
        path,
        matchMode,
        selectedId: selectedIdRef.current,
        filters,
        layout: settings.messageLayout,
        showInternal,
      }));
    } catch {
      setError(t("copyFailed"));
    }
  };

  const loadMore = () => {
    if (!hasMore || nextOffset === null || loading || loadingMore) return;
    loadConversationPage(nextOffset, true);
  };

  const startImport = () => {
    if (!importFile || uploadingImport || (importJob && ["queued", "running"].includes(importJob.status))) return;
    setUploadingImport(true);
    setImportError(null);
    uploadImportZip(importFile)
      .then((job) => {
        setImportJob(job);
        setImportFile(null);
        if (importInputRef.current) importInputRef.current.value = "";
      })
      .catch((err: unknown) => setImportError(importErrorLabel(t, err instanceof ApiError ? err.code : null)))
      .finally(() => setUploadingImport(false));
  };

  const header = useMemo(() => {
    if (!stats) return t("appTitle");
    return `${stats.conversations.toLocaleString()} ${t("conversations")} · ${stats.nodes.toLocaleString()} ${t("nodes")}`;
  }, [stats, t]);

  return (
    <div className="app-shell">
      <Sidebar
        t={t}
        query={query}
        setQuery={setQuery}
        sort={sort}
        setSort={setSort}
        path={path}
        setPath={setPath}
        matchMode={matchMode}
        setMatchMode={setMatchMode}
        filters={filters}
        setFilters={setFilters}
        conversations={conversations}
        selectedId={selectedId}
        focusIndex={focusIndex}
        setFocusIndex={setFocusIndex}
        onSelect={selectConversation}
        onLoadMore={loadMore}
        loading={loading}
        loadingMore={loadingMore}
        error={error}
        total={total}
        hasMore={hasMore}
        autoLoadMore={settings.autoLoadMore}
        health={health}
        diagnostics={debouncedQuery || filters.role || filters.title || filters.exact || filters.exclude || filters.after || filters.before || filters.source ? diagnostics : null}
      />
      <div
        className="sidebar-resizer"
        role="separator"
        tabIndex={0}
        aria-orientation="vertical"
        aria-label={t("sidebarWidth")}
        aria-valuemin={280}
        aria-valuemax={560}
        aria-valuenow={settings.sidebarWidth}
        onKeyDown={(event) => {
          const step = event.shiftKey ? 40 : 10;
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight" && event.key !== "Home" && event.key !== "End") return;
          event.preventDefault();
          const width = event.key === "Home" ? 280 : event.key === "End" ? 560 : settings.sidebarWidth + (event.key === "ArrowLeft" ? -step : step);
          updateSettings({ ...settings, sidebarWidth: width });
        }}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          const move = (moveEvent: PointerEvent) => updateSettings({ ...settings, sidebarWidth: moveEvent.clientX });
          const up = () => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", up);
        }}
      />
      <section className="main-column">
        <div className="top-bar" data-testid="top-toolbar">
          <span>{header}</span>
          <div className="top-actions">
            <label className="button-like" htmlFor="import-zip-input">{t("importZip")}</label>
            <input
              ref={importInputRef}
              id="import-zip-input"
              data-testid="import-zip-input"
              className="hidden-file-input"
              type="file"
              accept=".zip,application/zip"
              disabled={uploadingImport || Boolean(importJob && ["queued", "running"].includes(importJob.status))}
              onChange={(event) => setImportFile(event.currentTarget.files?.[0] ?? null)}
            />
            <button type="button" onClick={() => setHelpOpen(true)}>{t("searchHelp")}</button>
            <button type="button" onClick={() => setSettingsOpen(true)}>{t("settings")}</button>
            <button type="button" onClick={copyCurrentUrl}>{t("copyUrl")}</button>
            <span className="privacy-note">{health?.remote_access ? t("remoteAccessEnabled") : t("localOnly")}</span>
          </div>
          <input ref={searchRef} className="hidden-focus-target" tabIndex={-1} aria-hidden="true" />
        </div>
        {storageWarning && <div className="storage-warning warning-text" role="status">{t("settingsStorageWarning")}</div>}
        {(importFile || importJob || importError || health?.db_ready === false) && (
          <div className="import-panel" data-testid="import-panel">
            {health?.db_ready === false && <strong>{t("noArchiveYet")}</strong>}
            {importFile && <span>{t("importReady")}: {importFile.name} ({Math.round(importFile.size / 1024 / 1024)} MB)</span>}
            {importFile && (!importJob || !["queued", "running"].includes(importJob.status)) && (
              <button type="button" data-testid="import-start-button" onClick={startImport} disabled={uploadingImport}>
                {uploadingImport ? t("loading") : t("startImport")}
              </button>
            )}
            {importJob && (
              <div data-testid="import-status">
                <span>{importJob.status === "succeeded" ? t("importSucceeded") : importJob.status === "postcheck_failed" ? t("importPostcheckFailed") : importJob.status === "failed" ? t("importFailed") : t("importRunning")}</span>
                <span>{t("jobStage")}: {importStageLabel(t, importJob.stage)}</span>
                <span>{t("jobElapsed")}: {importJob.elapsed_seconds.toFixed(1)}s</span>
                {importJob.summary && <span>{String(importJob.summary.valid_conversations ?? 0)} {t("conversations")}</span>}
                {importJob.web_index && <span>{t("webIndexOk")}</span>}
                {["queued", "running"].includes(importJob.status) && <span>{t("importProgressVolatile")}</span>}
              </div>
            )}
            {importError && <span className="error-text">{importError}</span>}
            {importJob?.error_code && <span className="error-text">{importErrorLabel(t, importJob.error_code)}</span>}
            {importJob?.cleanup_warning && <span className="warning-text">{t("importCleanupWarning")}</span>}
          </div>
        )}
        <ConversationPane
          conversation={selected}
          query={debouncedQuery}
          filters={filters}
          matchMode={matchMode}
          path={path}
          setPath={setPath}
          settings={settings}
          showInternal={showInternal}
          setShowInternal={setShowInternal}
          t={t}
        />
      </section>
      <SearchHelp open={helpOpen} t={t} onClose={() => setHelpOpen(false)} />
      <SettingsPanel open={settingsOpen} settings={settings} t={t} onChange={updateSettings} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
