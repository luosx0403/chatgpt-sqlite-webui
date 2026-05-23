import { useEffect, useRef, useState } from "react";
import type { ConversationSummary, Health, MatchMode, PathMode, SearchFilters, SearchScope, SortMode } from "../types";
import { shortDate } from "../utils/format";
import { analyzeQuerySyntax } from "../utils/querySyntax";

interface Props {
  t: (key: string) => string;
  query: string;
  setQuery: (value: string) => void;
  sort: SortMode;
  setSort: (value: SortMode) => void;
  path: PathMode;
  setPath: (value: PathMode) => void;
  matchMode: MatchMode;
  setMatchMode: (value: MatchMode) => void;
  filters: SearchFilters;
  setFilters: (value: SearchFilters) => void;
  conversations: ConversationSummary[];
  selectedId: string | null;
  focusIndex: number;
  setFocusIndex: (value: number) => void;
  onSelect: (id: string) => void;
  onLoadMore: () => void;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  total: number;
  hasMore: boolean;
  autoLoadMore: boolean;
  health: Health | null;
}

export default function Sidebar(props: Props) {
  const listRef = useRef<HTMLDivElement | null>(null);
  const querySyntax = analyzeQuerySyntax(props.query);
  const effectivePath = querySyntax.pathOverride || props.path;
  const effectiveScope = querySyntax.scopeOverride || props.filters.scope;
  const updateFilter = (key: keyof SearchFilters, value: string) => {
    props.setFilters({ ...props.filters, [key]: value });
  };

  const handleScroll = () => {
    const node = listRef.current;
    if (!node || !props.autoLoadMore || props.loading || props.loadingMore || !props.hasMore) return;
    if (node.scrollHeight <= node.clientHeight) return;
    if (node.scrollTop + node.clientHeight >= node.scrollHeight - 120) props.onLoadMore();
  };

  return (
    <aside className="sidebar">
      <div className="search-panel">
        <label className="search-label" htmlFor="global-search">{props.t("search")}</label>
        <input
          id="global-search"
          value={props.query}
          onChange={(event) => props.setQuery(event.target.value)}
          placeholder={props.t("searchPlaceholder")}
          maxLength={500}
        />
        <div className="controls-row">
          <select value={props.sort} onChange={(event) => props.setSort(event.target.value as SortMode)} aria-label={props.t("sort")}>
            <option value="relevance">{props.t("relevance")}</option>
            <option value="newest">{props.t("newest")}</option>
            <option value="oldest">{props.t("oldest")}</option>
            <option value="updated">{props.t("updated")}</option>
            <option value="created">{props.t("created")}</option>
            <option value="title">{props.t("title")}</option>
          </select>
          <select value={effectivePath} onChange={(event) => props.setPath(event.target.value as PathMode)} aria-label={props.t("searchPath")} disabled={Boolean(querySyntax.pathOverride)}>
            <option value="current">{props.t("currentPath")}</option>
            <option value="all">{props.t("allNodes")}</option>
          </select>
        </div>
        {props.query.length >= 500 && <p className="hint warning-text">{props.t("queryLengthLimit")}</p>}
        {querySyntax.pathOverride && <p className="hint">{props.t("queryOverridesPath")} {effectivePath === "all" ? props.t("allNodes") : props.t("currentPath")}</p>}
        <label className="search-mode-toggle">
          <input
            type="checkbox"
            checked={props.matchMode === "word"}
            onChange={(event) => props.setMatchMode(event.target.checked ? "word" : "contains")}
          />
          {props.t("wholeWord")}
        </label>
        <details className="advanced-panel">
          <summary>{props.t("advancedFilters")}</summary>
          <div className="advanced-grid">
            <label>
              {props.t("role")}
              <select value={props.filters.role} onChange={(event) => updateFilter("role", event.target.value)}>
                <option value="">{props.t("all")}</option>
                <option value="user">{props.t("user")}</option>
                <option value="assistant">{props.t("assistant")}</option>
                <option value="system">{props.t("systemRole")}</option>
                <option value="developer">{props.t("developer")}</option>
                <option value="tool">{props.t("toolRole")}</option>
                <option value="tool/system">{props.t("toolSystem")}</option>
              </select>
            </label>
            <label>
              {props.t("scope")}
              <select value={effectiveScope} onChange={(event) => updateFilter("scope", event.target.value as SearchScope)} disabled={Boolean(querySyntax.scopeOverride)}>
                <option value="all">{props.t("titleMessages")}</option>
                <option value="title">{props.t("titleOnly")}</option>
                <option value="message">{props.t("messagesOnly")}</option>
              </select>
              {querySyntax.scopeOverride && <span className="filter-note">{props.t("queryOverridesScope")} {effectiveScope === "title" ? props.t("titleOnly") : effectiveScope === "message" ? props.t("messagesOnly") : props.t("titleMessages")}</span>}
            </label>
            <label>
              {props.t("titleContains")}
              <input value={props.filters.title} onChange={(event) => updateFilter("title", event.target.value)} maxLength={200} />
            </label>
            <label>
              {props.t("exactPhrase")}
              <input value={props.filters.exact} onChange={(event) => updateFilter("exact", event.target.value)} maxLength={300} />
            </label>
            <label>
              {props.t("exclude")}
              <input value={props.filters.exclude} onChange={(event) => updateFilter("exclude", event.target.value)} maxLength={200} />
            </label>
            <label>
              {props.t("sourceShard")}
              <input value={props.filters.source} onChange={(event) => updateFilter("source", event.target.value)} maxLength={200} placeholder="conversations-000.json" />
            </label>
            <label>
              {props.t("after")}
              <input type="date" value={props.filters.after} onChange={(event) => updateFilter("after", event.target.value)} />
            </label>
            <label>
              {props.t("before")}
              <input type="date" value={props.filters.before} onChange={(event) => updateFilter("before", event.target.value)} />
            </label>
          </div>
        </details>
        <p className="hint">{props.t("searchHint")}</p>
        {props.health?.db_ready && !props.health.web_normalized_trigram_indexed && (
          <p className="hint search-index-warning">{props.t("searchIndexMayBeSlow")}</p>
        )}
      </div>
      <div className="results-meta">
        {props.loading ? <SearchLoadingProgress label={props.t("loading")} /> : `${props.conversations.length} ${props.t("of")} ${props.total} ${props.t("conversations")}`}
      </div>
      {props.error && <div className="error-box">{props.error}</div>}
      <div className="conversation-list" ref={listRef} onScroll={handleScroll} role="listbox" aria-label={props.t("conversations")}>
        {props.conversations.map((item, index) => {
          const badges = [
            (item.has_title_hits || item.title_match || item.reasons?.includes("title match")) ? props.t("titleHitBadge") : "",
            (item.has_internal_hits || item.snippets?.some((snippet) => snippet.is_internal)) ? props.t("internalHitBadge") : "",
            (item.has_branch_hits || item.snippets?.some((snippet) => snippet.is_on_current_path === false)) ? props.t("branchHitBadge") : "",
          ].filter(Boolean);
          return (
            <button
              type="button"
              key={item.conversation_id}
              className={`conversation-item ${props.selectedId === item.conversation_id ? "selected" : ""} ${props.focusIndex === index ? "focused" : ""}`}
              onFocus={() => props.setFocusIndex(index)}
              onClick={() => props.onSelect(item.conversation_id)}
            >
              <span className="conversation-title">{item.title || props.t("untitled")}</span>
              <span className="conversation-meta">
                {shortDate(item.update_time || item.create_time)}
                {item.hit_count ? ` · ${item.hit_count} ${props.t("hits")}` : ""}
              </span>
              {badges.length > 0 && (
                <span className="result-badges" aria-label={props.t("resultBadges")}>
                  {badges.map((badge) => <span className="result-badge" key={badge}>{badge}</span>)}
                </span>
              )}
              {item.reasons && item.reasons.length > 0 && <span className="reason-row">{item.reasons.map((reason) => reasonLabel(props.t, reason)).join(", ")}</span>}
              {item.snippets?.[0] && <span className="snippet">{item.snippets[0].snippet}</span>}
            </button>
          );
        })}
        {!props.loading && !props.conversations.length && <div className="empty-list">{props.t("noConversations")}</div>}
        {props.hasMore ? (
          <button type="button" className="load-more" onClick={props.onLoadMore} disabled={props.loadingMore}>
            {props.loadingMore ? props.t("loadingMore") : props.t("loadMore")}
          </button>
        ) : (
          props.conversations.length > 0 && <div className="end-list">{props.t("noMoreResults")}</div>
        )}
        <div className="scroll-sentinel" aria-hidden="true" />
      </div>
    </aside>
  );
}

function reasonLabel(t: (key: string) => string, reason: string): string {
  const labels: Record<string, string> = {
    "message match": t("reasonMessageMatch"),
    "title match": t("reasonTitleMatch"),
    "current path": t("reasonCurrentPath"),
    "exact phrase": t("reasonExactPhrase"),
    substring: t("reasonSubstring"),
    fts: t("reasonFts"),
    "fuzzy title": t("reasonFuzzyTitle"),
    "source match": t("reasonSourceMatch"),
    "role filter": t("reasonRoleFilter"),
    "date filter": t("reasonDateFilter"),
    "exclude filter": t("reasonExcludeFilter"),
    "filter match": t("reasonFilterMatch"),
  };
  return labels[reason] || t("reasonOther");
}

function SearchLoadingProgress({ label }: { label: string }) {
  const [step, setStep] = useState(0);
  const reducedMotion = useRef(false);
  const width = 16;
  const block = 4;

  useEffect(() => {
    reducedMotion.current = Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
    if (reducedMotion.current) return undefined;
    const timer = window.setInterval(() => setStep((value) => (value + 1) % width), 110);
    return () => window.clearInterval(timer);
  }, []);

  const position = reducedMotion.current ? 0 : step;
  const bar = Array.from({ length: width }, (_, index) => {
    const distance = (index - position + width) % width;
    return distance < block ? "█" : "░";
  }).join("");

  return (
    <span className="search-loading-progress" data-testid="search-loading-progress" role="status" aria-live="polite" aria-label={label}>
      <span className="search-loading-label">{label}</span>
      <span className="search-loading-bar" aria-hidden="true"> [{bar}]</span>
    </span>
  );
}
