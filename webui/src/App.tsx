import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Sidebar from "./components/Sidebar";
import ConversationPane from "./components/ConversationPane";
import SearchHelp from "./components/SearchHelp";
import SettingsPanel from "./components/SettingsPanel";
import { getConversation, getConversations, getHealth, getImportJob, getStats, uploadImportZip } from "./api/client";
import { applySettings, clampSettings, loadSettings, saveSettings, type Settings } from "./settings";
import { createTranslator } from "./i18n";
import type { ConversationSummary, Health, ImportJob, PathMode, SearchFilters, SortMode, Stats } from "./types";

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

export default function App() {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [sort, setSort] = useState<SortMode>("relevance");
  const [path, setPath] = useState<PathMode>("current");
  const [filters, setFilters] = useState<SearchFilters>(DEFAULT_FILTERS);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(() => new URLSearchParams(window.location.search).get("conversation"));
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
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const listRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const detailControllerRef = useRef<AbortController | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const selectedRef = useRef<ConversationSummary | null>(null);
  const filtersKey = JSON.stringify(filters);
  const { t } = useMemo(() => createTranslator(settings.language), [settings.language]);

  const updateSettings = useCallback((next: Settings) => {
    const clamped = clampSettings(next);
    setSettings(clamped);
    saveSettings(clamped);
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
      const target = event.target as HTMLElement | null;
      const isTyping = Boolean(target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable));
      if (event.key === "Escape") {
        if (settingsOpen) setSettingsOpen(false);
        else if (helpOpen) setHelpOpen(false);
        else if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      }
      if ((!isTyping && event.key === "/") || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k")) {
        event.preventDefault();
        document.getElementById("global-search")?.focus();
      }
      if (!isTyping && !settingsOpen && !helpOpen) {
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

  const loadConversationDetail = useCallback((id: string, local?: ConversationSummary) => {
    setSelectedId(id);
    selectedIdRef.current = id;
    if (local) setSelected(local);
    detailControllerRef.current?.abort();
    const controller = new AbortController();
    detailControllerRef.current = controller;
    const requestId = ++detailRequestRef.current;
    getConversation(id, controller.signal)
      .then((detail) => {
        if (requestId === detailRequestRef.current) setSelected(detail);
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError" && requestId === detailRequestRef.current) setError(err.message);
      });
  }, []);

  const loadConversationPage = useCallback((offset: number, append: boolean) => {
    const controller = new AbortController();
    const requestId = ++listRequestRef.current;
    if (append) setLoadingMore(true);
    else setLoading(true);
    setError(null);
    getConversations({
      q: debouncedQuery,
      sort,
      path,
      filters,
      offset,
      limit: settings.listPageSize,
      selectedId: selectedIdRef.current,
      signal: controller.signal
    })
      .then((page) => {
        if (requestId !== listRequestRef.current) return;
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
        const currentSelectedId = selectedIdRef.current;
        const selectedStillMatches = currentSelectedId ? page.selected_in_results !== false : false;
        if (selectedStillMatches && currentSelectedId) {
          const localSelected = page.items.find((item) => item.conversation_id === currentSelectedId);
          if (!selectedRef.current || selectedRef.current.conversation_id !== currentSelectedId) {
            loadConversationDetail(currentSelectedId, localSelected);
          } else if (localSelected) {
            setSelected((current) => current?.conversation_id === currentSelectedId ? { ...current, ...localSelected } : current);
          }
        } else {
          const first = page.items[0] ?? null;
          selectedIdRef.current = first?.conversation_id ?? null;
          setSelectedId(first?.conversation_id ?? null);
          setSelected(first);
        }
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError" && requestId === listRequestRef.current) setError(err.message);
      })
      .finally(() => {
        if (requestId === listRequestRef.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      });
    return controller;
  }, [debouncedQuery, sort, path, filtersKey, loadConversationDetail, settings.listPageSize]);

  useEffect(() => {
    const controller = loadConversationPage(0, false);
    return () => controller.abort();
  }, [loadConversationPage]);

  useEffect(() => {
    if (!importJob || !["queued", "running"].includes(importJob.status)) return;
    const timer = window.setInterval(() => {
      getImportJob(importJob.job_id)
        .then((job) => {
          setImportJob(job);
          if (job.status === "succeeded" || job.status === "postcheck_failed") {
            refreshArchiveState();
            loadConversationPage(0, false);
          }
        })
        .catch((err: Error) => setImportError(err.message));
    }, 1200);
    return () => window.clearInterval(timer);
  }, [importJob?.job_id, importJob?.status, refreshArchiveState, loadConversationPage]);

  const selectConversation = (id: string) => {
    const local = conversations.find((item) => item.conversation_id === id);
    loadConversationDetail(id, local);
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
      .then((job) => setImportJob(job))
      .catch((err: Error) => setImportError(err.message))
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
      />
      <div
        className="sidebar-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label={t("sidebarWidth")}
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
        <div className="top-bar">
          <span>{header}</span>
          <div className="top-actions">
            <label className="button-like" htmlFor="import-zip-input">{t("importZip")}</label>
            <input
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
            <span className="privacy-note">{t("localOnly")}</span>
          </div>
          <input ref={searchRef} className="hidden-focus-target" tabIndex={-1} aria-hidden="true" />
        </div>
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
                <span>{t("jobStage")}: {importJob.stage}</span>
                <span>{t("jobElapsed")}: {importJob.elapsed_seconds.toFixed(1)}s</span>
                {importJob.summary && <span>{String(importJob.summary.valid_conversations ?? 0)} {t("conversations")}</span>}
                {importJob.web_index && <span>web-index ok</span>}
              </div>
            )}
            {importError && <span className="error-text">{importError}</span>}
            {importJob?.error && <span className="error-text">{importJob.error}</span>}
          </div>
        )}
        <ConversationPane
          conversation={selected}
          query={debouncedQuery}
          filters={filters}
          path={path}
          setPath={setPath}
          settings={settings}
          t={t}
        />
      </section>
      <SearchHelp open={helpOpen} t={t} onClose={() => setHelpOpen(false)} />
      <SettingsPanel open={settingsOpen} settings={settings} t={t} onChange={updateSettings} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
