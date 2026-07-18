import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { ConversationSummary, MatchMode, MessageItem, PathMode, SearchFilters, SearchMessageHit } from "../types";
import { CopyLimitError, IncompleteDisplayRecoveryError, MAX_BROWSER_COPY_BYTES, MAX_BROWSER_COPY_CHARS, assertBrowserCopyLimit, exportUrl, getConversationCopyText, getMessageDisplayChunk, getMessageHits, getMessages } from "../api/client";
import { formatDate } from "../utils/format";
import { analyzeQuerySyntax } from "../utils/querySyntax";
import MessageBlock from "./MessageBlock";
import type { Settings } from "../settings";
import { isInteractiveTarget } from "../utils/interaction";

const MAX_NAVIGABLE_HIT_MESSAGES = 1000;
const HIT_NAVIGATION_PAGE_SIZE = 100;
const HIT_PREFETCH_THRESHOLD = 10;

interface Props {
  conversation: ConversationSummary | null;
  query: string;
  filters: SearchFilters;
  matchMode: MatchMode;
  path: PathMode;
  setPath: (value: PathMode) => void;
  settings: Settings;
  showInternal: boolean;
  setShowInternal: (value: boolean) => void;
  t: (key: string) => string;
}

export default function ConversationPane({ conversation, query, filters, matchMode, path, setPath, settings, showInternal, setShowInternal, t }: Props) {
  const [messages, setMessages] = useState<MessageItem[]>([]);
  const [messageTotal, setMessageTotal] = useState(0);
  const [visibleTotal, setVisibleTotal] = useState(0);
  const [emptyHiddenCount, setEmptyHiddenCount] = useState(0);
  const [internalHiddenCount, setInternalHiddenCount] = useState(0);
  const [currentPathFallbackToAll, setCurrentPathFallbackToAll] = useState(false);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hitItems, setHitItems] = useState<SearchMessageHit[]>([]);
  const [hitExactTotal, setHitExactTotal] = useState<number | null>(null);
  const [hitLimitReached, setHitLimitReached] = useState(false);
  const [hitHasMore, setHitHasMore] = useState(false);
  const [hitNextOffset, setHitNextOffset] = useState<number | null>(null);
  const [hitLoadingMore, setHitLoadingMore] = useState(false);
  const [hitIndex, setHitIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [copyStatus, setCopyStatus] = useState("");
  const [copyBusy, setCopyBusy] = useState<"visible" | "conversation" | null>(null);
  const parentRef = useRef<HTMLDivElement | null>(null);
  const measureFrameRef = useRef<number | null>(null);
  const messageRequestRef = useRef(0);
  const hitRequestRef = useRef(0);
  const messageControllerRef = useRef<AbortController | null>(null);
  const hitControllerRef = useRef<AbortController | null>(null);
  const hitAppendInFlightRef = useRef(false);
  const copyControllerRef = useRef<AbortController | null>(null);
  const copyRequestRef = useRef(0);
  const copyStatusTimerRef = useRef<number | null>(null);
  const scrollRequestRef = useRef(0);
  const fallbackTargetsRef = useRef<Set<string>>(new Set());
  const replacementLoadRef = useRef(false);
  const missingHitWindowRef = useRef<Set<string>>(new Set());
  const filtersKey = JSON.stringify(filters);
  const highlightQuery = query;
  const querySyntax = useMemo(() => analyzeQuerySyntax(highlightQuery), [highlightQuery]);
  const effectivePath = querySyntax.pathOverride || path;
  const effectiveScope = querySyntax.scopeOverride || filters.scope;
  const effectiveFilters = useMemo(() => ({ ...filters, scope: effectiveScope }), [filters, effectiveScope]);
  const effectiveFiltersKey = JSON.stringify(effectiveFilters);
  const hasBodySearchText = useMemo(() => Boolean(effectiveScope !== "title" && (querySyntax.hasBodyText || filters.exact)), [effectiveScope, filters.exact, querySyntax.hasBodyText]);
  const searchActive = useMemo(() => Boolean(
    querySyntax.hasSearchContext ||
    hasBodySearchText ||
    filters.role ||
    filters.title ||
    filters.exact ||
    filters.exclude ||
    filters.after ||
    filters.before ||
    filters.source,
  ), [filters, hasBodySearchText, querySyntax.hasSearchContext]);

  const readerDataContextKey = JSON.stringify({
    conversationId: conversation?.conversation_id ?? null,
    effectivePath,
    highlightQuery,
    matchMode,
    filters: effectiveFilters,
    showInternal,
    pageSize: settings.messagePageSize,
  });
  const readerLayoutContextKey = JSON.stringify({
    layout: settings.messageLayout,
    density: settings.density,
    fontSize: settings.fontSize,
    messageMaxWidth: settings.messageMaxWidth,
  });
  const messageStateContextRef = useRef({ dataKey: readerDataContextKey, epoch: 0 });
  if (messageStateContextRef.current.dataKey !== readerDataContextKey) {
    messageStateContextRef.current = {
      dataKey: readerDataContextKey,
      epoch: messageStateContextRef.current.epoch + 1,
    };
  }
  const messageStateContextKey = `${messageStateContextRef.current.epoch}:${readerDataContextKey}`;
  const readerDataContextRef = useRef(readerDataContextKey);
  readerDataContextRef.current = readerDataContextKey;

  useLayoutEffect(() => {
    messageControllerRef.current?.abort();
    hitControllerRef.current?.abort();
    copyControllerRef.current?.abort();
    messageRequestRef.current += 1;
    hitRequestRef.current += 1;
    copyRequestRef.current += 1;
    if (copyStatusTimerRef.current !== null) {
      window.clearTimeout(copyStatusTimerRef.current);
      copyStatusTimerRef.current = null;
    }
    setCopyStatus("");
    setMessages([]);
    setMessageTotal(0);
    setVisibleTotal(0);
    setEmptyHiddenCount(0);
    setInternalHiddenCount(0);
    setCurrentPathFallbackToAll(false);
    setNextOffset(null);
    setHasMore(false);
    setHitItems([]);
    setHitExactTotal(null);
    setHitLimitReached(false);
    setHitHasMore(false);
    setHitNextOffset(null);
    setHitLoadingMore(false);
    hitAppendInFlightRef.current = false;
    setHitIndex(0);
    setCopyBusy(null);
    setCopyStatus("");
    fallbackTargetsRef.current.clear();
    missingHitWindowRef.current.clear();
    replacementLoadRef.current = false;
    scrollRequestRef.current += 1;
  }, [readerDataContextKey]);

  useEffect(() => {
    fallbackTargetsRef.current.clear();
    missingHitWindowRef.current.clear();
  }, [conversation?.conversation_id, effectivePath, highlightQuery, matchMode, effectiveFiltersKey, showInternal]);

  const loadMessages = useCallback((offset: number, append: boolean, aroundNodeId?: string) => {
    if (!conversation) return new AbortController();
    const controller = new AbortController();
    const requestedContextKey = readerDataContextKey;
    if (!append) messageControllerRef.current?.abort();
    messageControllerRef.current = controller;
    const requestId = ++messageRequestRef.current;
    if (!append) replacementLoadRef.current = true;
    if (append) setLoadingMore(true);
    else setLoading(true);
    setError(null);
    getMessages({ id: conversation.conversation_id, q: highlightQuery, path: effectivePath, filters: effectiveFilters, offset, limit: settings.messagePageSize, aroundNodeId, includeInternal: showInternal, matchMode, signal: controller.signal })
      .then((page) => {
        if (requestId !== messageRequestRef.current || readerDataContextRef.current !== requestedContextKey) return;
        if (!Array.isArray(page.items)) throw new Error("invalid_response");
        setMessages((current) => append ? [...current, ...page.items] : page.items);
        setMessageTotal(page.total);
        setVisibleTotal(page.visible_total ?? page.total);
        setEmptyHiddenCount(page.empty_hidden_count ?? 0);
        setInternalHiddenCount(page.internal_hidden_count ?? 0);
        setCurrentPathFallbackToAll(Boolean(page.current_path_fallback_to_all || conversation.current_path_fallback_to_all));
        setHasMore(page.has_more);
        setNextOffset(page.next_offset);
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError" && requestId === messageRequestRef.current && readerDataContextRef.current === requestedContextKey) setError(t("requestFailed"));
      })
      .finally(() => {
        if (requestId === messageRequestRef.current && readerDataContextRef.current === requestedContextKey) {
          if (!append) replacementLoadRef.current = false;
          setLoading(false);
          setLoadingMore(false);
        }
      });
    return controller;
  }, [conversation?.conversation_id, conversation?.current_path_fallback_to_all, effectivePath, highlightQuery, matchMode, effectiveFiltersKey, settings.messagePageSize, showInternal, readerDataContextKey, t]);

  useEffect(() => {
    if (!conversation) return;
    const controller = loadMessages(0, false);
    return () => controller.abort();
  }, [readerDataContextKey, loadMessages]);

  const visibleMessages = useMemo(
    () => messages.filter((message) => !message.is_empty_mapping_node && (showInternal || !message.is_internal)),
    [messages, showInternal],
  );
  const messageKeys = useMemo(() => {
    const counts = new Map<string, number>();
    const keys = new Map<MessageItem, string>();
    messages.forEach((message, index) => {
      const base = JSON.stringify([
        conversation?.conversation_id || "conversation",
        message.node_id || "missing-node",
        message.message_id || "missing-message",
        message.content_hash || "missing-hash",
      ]);
      const occurrence = counts.get(base) || 0;
      counts.set(base, occurrence + 1);
      keys.set(message, JSON.stringify([base, occurrence]));
    });
    return keys;
  }, [conversation?.conversation_id, messages]);
  const messageKey = useCallback((message: MessageItem | undefined, index: number) => {
    if (!message) return JSON.stringify([conversation?.conversation_id || "conversation", "missing", index]);
    return messageKeys.get(message) || JSON.stringify([conversation?.conversation_id || "conversation", message.node_id || message.message_id || index, index]);
  }, [conversation?.conversation_id, messageKeys]);
  const visibleMessageKeys = useMemo(
    () => JSON.stringify(visibleMessages.map((message, index) => messageKey(message, index))),
    [messageKey, visibleMessages],
  );

  useEffect(() => {
    setHitItems([]);
    setHitExactTotal(null);
    setHitLimitReached(false);
    setHitHasMore(false);
    setHitNextOffset(null);
    setHitLoadingMore(false);
    hitAppendInFlightRef.current = false;
    setHitIndex(0);
    const requestId = ++hitRequestRef.current;
    const requestedContextKey = readerDataContextKey;
    if (!conversation || !searchActive) {
      return;
    }
    const controller = new AbortController();
    hitControllerRef.current?.abort();
    hitControllerRef.current = controller;
    const loadHits = () => getMessageHits({ q: highlightQuery, conversationId: conversation.conversation_id, path: effectivePath, order: "display", limit: HIT_NAVIGATION_PAGE_SIZE, offset: 0, filters: effectiveFilters, matchMode, countTotal: true, signal: controller.signal });
    loadHits()
      .then((page) => {
        if (requestId !== hitRequestRef.current || readerDataContextRef.current !== requestedContextKey) return;
        if (!Array.isArray(page.items)) throw new Error("invalid_response");
        setHitItems(page.items);
        setHitExactTotal(page.total_exact ? page.total : null);
        setHitNextOffset(page.next_offset);
        setHitHasMore(Boolean(page.has_more && page.next_offset !== null));
        setHitLimitReached(Boolean(page.has_more && page.items.length >= MAX_NAVIGABLE_HIT_MESSAGES));
        setHitIndex(0);
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError" && requestId === hitRequestRef.current && readerDataContextRef.current === requestedContextKey) setHitItems([]);
      });
    return () => controller.abort();
  }, [readerDataContextKey, searchActive]);

  const rowVirtualizer = useVirtualizer({
    count: visibleMessages.length,
    getScrollElement: () => parentRef.current,
    getItemKey: useCallback((index: number) => messageKey(visibleMessages[index], index), [messageKey, visibleMessages]),
    estimateSize: () => 180,
    overscan: 8
  });

  const measureMessagesSoon = useCallback(() => {
    if (measureFrameRef.current !== null) return;
    const measureVisibleRows = () => {
      const rows = parentRef.current?.querySelectorAll<HTMLElement>(".virtual-row");
      if (rows?.length) {
        rows.forEach((row) => rowVirtualizer.measureElement(row));
      } else {
        rowVirtualizer.measure();
      }
    };
    measureFrameRef.current = window.requestAnimationFrame(() => {
      measureVisibleRows();
      measureFrameRef.current = window.requestAnimationFrame(() => {
        measureFrameRef.current = null;
        measureVisibleRows();
      });
    });
  }, [rowVirtualizer]);

  useEffect(() => () => {
    copyControllerRef.current?.abort();
    copyControllerRef.current = null;
    copyRequestRef.current += 1;
    if (measureFrameRef.current !== null) {
      window.cancelAnimationFrame(measureFrameRef.current);
      measureFrameRef.current = null;
    }
    if (copyStatusTimerRef.current !== null) {
      window.clearTimeout(copyStatusTimerRef.current);
      copyStatusTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    const node = parentRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measureMessagesSoon());
    observer.observe(node);
    return () => observer.disconnect();
  }, [measureMessagesSoon]);

  useLayoutEffect(() => {
    const scroller = parentRef.current;
    if (!scroller) return;
    const scrollerTop = scroller.getBoundingClientRect().top;
    const anchor = Array.from(scroller.querySelectorAll<HTMLElement>("[data-node-id]"))
      .find((element) => element.getBoundingClientRect().bottom >= scrollerTop);
    const anchorId = anchor?.dataset.nodeId;
    const anchorOffset = anchor ? anchor.getBoundingClientRect().top - scrollerTop : 0;
    rowVirtualizer.measure();
    measureMessagesSoon();
    const frame = window.requestAnimationFrame(() => {
      if (!anchorId) return;
      const nextAnchor = Array.from(scroller.querySelectorAll<HTMLElement>("[data-node-id]"))
        .find((element) => element.dataset.nodeId === anchorId);
      if (nextAnchor) {
        scroller.scrollTop += nextAnchor.getBoundingClientRect().top - scrollerTop - anchorOffset;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [readerLayoutContextKey, rowVirtualizer, measureMessagesSoon]);

  const hiddenInternalHits = useMemo(() => hitItems.filter((hit) => hit.is_internal && !showInternal), [hitItems, showInternal]);
  const isCurrentFallbackHit = useCallback((hit: SearchMessageHit) => Boolean(hit.effective_visible_in_current_view || hit.current_path_fallback_to_all || currentPathFallbackToAll || conversation?.current_path_fallback_to_all), [conversation?.current_path_fallback_to_all, currentPathFallbackToAll]);
  const currentViewHits = useMemo(
    () => hitItems.filter((hit) => !(hit.is_internal && !showInternal) && !(effectivePath === "current" && !isCurrentFallbackHit(hit))),
    [hitItems, effectivePath, showInternal, isCurrentFallbackHit],
  );
  const loadMoreHits = useCallback(() => {
    if (
      !conversation || !searchActive || !hitHasMore || hitNextOffset === null ||
      hitAppendInFlightRef.current || hitItems.length >= MAX_NAVIGABLE_HIT_MESSAGES
    ) return;
    hitAppendInFlightRef.current = true;
    setHitLoadingMore(true);
    const controller = new AbortController();
    hitControllerRef.current?.abort();
    hitControllerRef.current = controller;
    const requestId = ++hitRequestRef.current;
    const requestedContextKey = readerDataContextKey;
    const remaining = MAX_NAVIGABLE_HIT_MESSAGES - hitItems.length;
    getMessageHits({
      q: highlightQuery,
      conversationId: conversation.conversation_id,
      path: effectivePath,
      order: "display",
      limit: Math.min(HIT_NAVIGATION_PAGE_SIZE, remaining),
      offset: hitNextOffset,
      filters: effectiveFilters,
      matchMode,
      countTotal: false,
      signal: controller.signal,
    })
      .then((page) => {
        if (requestId !== hitRequestRef.current || readerDataContextRef.current !== requestedContextKey) return;
        if (!Array.isArray(page.items)) throw new Error("invalid_response");
        const loadedCount = Math.min(MAX_NAVIGABLE_HIT_MESSAGES, hitItems.length + page.items.length);
        setHitItems((current) => [...current, ...page.items].slice(0, MAX_NAVIGABLE_HIT_MESSAGES));
        setHitNextOffset(page.next_offset);
        setHitHasMore(Boolean(page.has_more && page.next_offset !== null && loadedCount < MAX_NAVIGABLE_HIT_MESSAGES));
        setHitLimitReached(Boolean(page.has_more && loadedCount >= MAX_NAVIGABLE_HIT_MESSAGES));
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError" && requestId === hitRequestRef.current && readerDataContextRef.current === requestedContextKey) {
          setHitHasMore(false);
        }
      })
      .finally(() => {
        if (requestId === hitRequestRef.current && readerDataContextRef.current === requestedContextKey) {
          hitAppendInFlightRef.current = false;
          setHitLoadingMore(false);
        }
      });
  }, [conversation?.conversation_id, effectiveFiltersKey, effectivePath, highlightQuery, hitHasMore, hitItems.length, hitNextOffset, matchMode, readerDataContextKey, searchActive]);
  const titleOnlyContext = Boolean(
    filters.title ||
    (effectiveScope === "title" && (querySyntax.hasBodyText || querySyntax.hasTitleText || filters.exact)),
  );
  const titleOnlyMatch = Boolean(searchActive && hitItems.length === 0 && (
    titleOnlyContext || conversation?.title_match || conversation?.has_title_hits || conversation?.reasons?.includes("title match")
  ));
  const hiddenSnippetInternal = Boolean(!showInternal && (conversation?.has_internal_hits || conversation?.snippets?.some((snippet) => snippet.is_internal)));
  const activeHit = currentViewHits[hitIndex] ?? null;
  const activeNode = activeHit?.node_id || null;
  const activeIndex = useMemo(() => visibleMessages.findIndex((msg) => msg.node_id === activeNode), [visibleMessages, activeNode]);

  useEffect(() => {
    if (!currentViewHits.length && hitIndex !== 0) setHitIndex(0);
    else if (currentViewHits.length && hitIndex >= currentViewHits.length) setHitIndex(0);
  }, [currentViewHits.length, hitIndex]);

  useEffect(() => {
    rowVirtualizer.measure();
    measureMessagesSoon();
  }, [visibleMessageKeys, conversation?.conversation_id, rowVirtualizer, measureMessagesSoon]);

  useEffect(() => {
    measureMessagesSoon();
  }, [
    visibleMessageKeys,
    visibleMessages.length,
    showInternal,
    highlightQuery,
    matchMode,
    effectiveFiltersKey,
    activeNode,
    hitIndex,
    settings.messageLayout,
    settings.density,
    settings.fontSize,
    settings.messageMaxWidth,
    conversation?.conversation_id,
    measureMessagesSoon,
  ]);

  const scrollActiveHitIntoView = useCallback((nodeId: string, rowIndex: number) => {
    const requestId = ++scrollRequestRef.current;
    rowVirtualizer.scrollToIndex(rowIndex, { align: "center" });
    measureMessagesSoon();

    const isVisibleInScroller = (element: HTMLElement, scroller: HTMLElement) => {
      const elementRect = element.getBoundingClientRect();
      const scrollerRect = scroller.getBoundingClientRect();
      return elementRect.top >= scrollerRect.top && elementRect.bottom <= scrollerRect.bottom && elementRect.height > 0;
    };

    let frame = 0;
    let animationId = 0;
    let foundRow = false;
    let foundMark = false;
    const tryScroll = () => {
      if (requestId !== scrollRequestRef.current) return;
      const scrollEl = parentRef.current;
      if (!scrollEl) return;
      const row = Array.from(scrollEl.querySelectorAll<HTMLElement>("[data-node-id]")).find((element) => element.dataset.nodeId === nodeId);
      const mark = row?.querySelector<HTMLElement>(".search-highlight-active, .search-highlight");
      const virtualRow = row?.closest<HTMLElement>(".virtual-row");
      foundRow = foundRow || Boolean(row);
      foundMark = foundMark || Boolean(mark);
      if (virtualRow) rowVirtualizer.measureElement(virtualRow);
      if (row && frame < 3) {
        measureMessagesSoon();
      } else if (mark) {
        mark.scrollIntoView({ block: "center", inline: "nearest", behavior: "auto" });
        if (virtualRow) rowVirtualizer.measureElement(virtualRow);
        measureMessagesSoon();
        if (isVisibleInScroller(mark, scrollEl)) return;
      } else if (row && frame >= 10) {
        row.scrollIntoView({ block: "center", inline: "nearest", behavior: "auto" });
        return;
      }
      if (!row) rowVirtualizer.scrollToIndex(rowIndex, { align: "center" });
      if (frame >= 12) {
        if (!fallbackTargetsRef.current.has(nodeId)) {
          fallbackTargetsRef.current.add(nodeId);
          loadMessages(0, false, nodeId);
          return;
        }
        console.warn("Unable to fully reveal active search hit", {
          targetNodeId: nodeId,
          activeIndex: rowIndex,
          messagesLength: messages.length,
          hasMore,
          nextOffset,
          foundRow,
          foundMark,
        });
        if (row) row.scrollIntoView({ block: "center", inline: "nearest", behavior: "auto" });
        return;
      }
      frame += 1;
      animationId = window.requestAnimationFrame(tryScroll);
    };
    animationId = window.requestAnimationFrame(tryScroll);
    return () => window.cancelAnimationFrame(animationId);
  }, [hasMore, loadMessages, measureMessagesSoon, messages.length, nextOffset, rowVirtualizer]);

  useEffect(() => {
    if (!activeNode || activeIndex < 0) return;
    return scrollActiveHitIntoView(activeNode, activeIndex);
  }, [activeNode, activeIndex, visibleMessages.length, scrollActiveHitIntoView]);

  useEffect(() => {
    if (activeNode && activeIndex < 0 && !loading && !replacementLoadRef.current && !missingHitWindowRef.current.has(activeNode)) {
      missingHitWindowRef.current.add(activeNode);
      loadMessages(0, false, activeNode);
    }
  }, [activeNode, activeIndex, loading, loadMessages]);

  const copyText = async (text: string, expectedContextKey?: string, requestId?: number): Promise<boolean> => {
    const clearCopyStatusLater = (delay: number) => {
      if (copyStatusTimerRef.current !== null) window.clearTimeout(copyStatusTimerRef.current);
      const contextKey = readerDataContextRef.current;
      const generation = copyRequestRef.current;
      copyStatusTimerRef.current = window.setTimeout(() => {
        copyStatusTimerRef.current = null;
        if (readerDataContextRef.current === contextKey && copyRequestRef.current === generation) setCopyStatus("");
      }, delay);
    };
    try {
      assertBrowserCopyLimit(text);
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      if (expectedContextKey && (readerDataContextRef.current !== expectedContextKey || requestId !== copyRequestRef.current)) return false;
      setCopyStatus(t("copied"));
      clearCopyStatusLater(1400);
      return true;
    } catch {
      if (expectedContextKey && (readerDataContextRef.current !== expectedContextKey || requestId !== copyRequestRef.current)) return false;
      setCopyStatus(t("copyFailed"));
      clearCopyStatusLater(1800);
      return false;
    }
  };
  const messageText = (m: MessageItem) => m.display_text || "";
  const completeMessageText = async (message: MessageItem, signal: AbortSignal): Promise<string> => {
    if (message.display_text_resolver_input_truncated) throw new IncompleteDisplayRecoveryError();
    if (!message.display_text_truncated) return messageText(message);
    let offset = 0;
    let cursor: string | null = null;
    let complete = "";
    let completeBytes = 0;
    for (;;) {
      const chunk = await getMessageDisplayChunk(conversation!.conversation_id, message.node_id, offset, 1048576, signal, cursor);
      if (chunk.resolver_input_truncated || (!chunk.has_more && !chunk.total_chars_exact)) {
        throw new IncompleteDisplayRecoveryError();
      }
      completeBytes += new TextEncoder().encode(chunk.display_text).byteLength;
      if (complete.length + chunk.display_text.length > MAX_BROWSER_COPY_CHARS || completeBytes > MAX_BROWSER_COPY_BYTES) {
        throw new CopyLimitError();
      }
      complete += chunk.display_text;
      if (!chunk.has_more || chunk.next_offset === null) return complete;
      offset = chunk.next_offset;
      cursor = chunk.next_cursor;
    }
  };
  const formatMessagesForCopy = async (items: MessageItem[], signal: AbortSignal): Promise<string> => {
    const parts: string[] = [];
    let chars = 0;
    let bytes = 0;
    for (const message of items) {
      if (message.is_empty_mapping_node) continue;
      const complete = await completeMessageText(message, signal);
      if (complete.trim()) {
        const part = `${message.role || "message"}:\n${complete}`;
        chars += part.length + (parts.length ? 2 : 0);
        bytes += new TextEncoder().encode(part).byteLength + (parts.length ? 2 : 0);
        if (chars > MAX_BROWSER_COPY_CHARS || bytes > MAX_BROWSER_COPY_BYTES) throw new CopyLimitError();
        parts.push(part);
      }
    }
    return parts.join("\n\n");
  };
  const runCopy = async (mode: "visible" | "conversation") => {
    if (copyBusy) return;
    copyControllerRef.current?.abort();
    const controller = new AbortController();
    copyControllerRef.current = controller;
    const requestedContextKey = readerDataContextKey;
    const requestId = ++copyRequestRef.current;
    setCopyBusy(mode);
    setCopyStatus(t("preparingCopy"));
    try {
      const copyValue = mode === "conversation"
        ? await getConversationCopyText(conversation!.conversation_id, effectivePath, showInternal, controller.signal)
        : await formatMessagesForCopy(visibleMessages, controller.signal);
      if (readerDataContextRef.current !== requestedContextKey || requestId !== copyRequestRef.current) return;
      await copyText(copyValue, requestedContextKey, requestId);
    } catch (error) {
      if (controller.signal.aborted || readerDataContextRef.current !== requestedContextKey || requestId !== copyRequestRef.current) return;
      setCopyStatus(
        error instanceof CopyLimitError
          ? t("copyTooLarge")
          : error instanceof IncompleteDisplayRecoveryError
            ? t("copyIncomplete")
            : mode === "conversation"
              ? t("copyConversationFailed")
              : t("copyFailed"),
      );
      if (copyStatusTimerRef.current !== null) window.clearTimeout(copyStatusTimerRef.current);
      const contextKey = readerDataContextRef.current;
      const generation = copyRequestRef.current;
      copyStatusTimerRef.current = window.setTimeout(() => {
        copyStatusTimerRef.current = null;
        if (readerDataContextRef.current === contextKey && copyRequestRef.current === generation) setCopyStatus("");
      }, 1800);
    } finally {
      if (readerDataContextRef.current === requestedContextKey && requestId === copyRequestRef.current) setCopyBusy(null);
    }
  };
  const copyConversation = () => runCopy("conversation");
  const copyVisible = () => runCopy("visible");
  const jump = useCallback((delta: number) => {
    if (!currentViewHits.length) return;
    if (delta > 0 && hitHasMore && hitIndex >= Math.max(0, currentViewHits.length - HIT_PREFETCH_THRESHOLD)) {
      loadMoreHits();
    }
    if (delta > 0 && hitHasMore && hitIndex === currentViewHits.length - 1) return;
    const next = (hitIndex + delta + currentViewHits.length) % currentViewHits.length;
    setHitIndex(next);
    const target = currentViewHits[next]?.node_id;
    if (target && !messages.some((message) => message.node_id === target)) {
      loadMessages(0, false, target);
    }
  }, [currentViewHits, hitHasMore, hitIndex, loadMessages, loadMoreHits, messages]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (isInteractiveTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key.toLowerCase() === "n") {
        event.preventDefault();
        jump(1);
      }
      if (event.key.toLowerCase() === "p") {
        event.preventDefault();
        jump(-1);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [jump]);
  const loadMore = () => {
    if (!hasMore || nextOffset === null || loading || loadingMore) return;
    loadMessages(nextOffset, true);
  };
  const onMessageScroll = () => {
    const node = parentRef.current;
    if (!settings.autoLoadMore || !node || !hasMore || loading || loadingMore || nextOffset === null) return;
    if (node.scrollHeight - node.scrollTop - node.clientHeight < 640) loadMessages(nextOffset, true);
  };

  if (!conversation) {
    return <main className="reader empty-state">{query ? t("emptySearch") : t("selectConversation")}</main>;
  }

  const totalHitCount = hitItems.length;
  const hiddenInternalCount = hiddenInternalHits.length + (hiddenSnippetInternal && !hiddenInternalHits.length ? 1 : 0);
  const readerVisibleTotal = showInternal ? Math.max(0, messageTotal - emptyHiddenCount) : visibleTotal;
  const hiddenNodeSummaryParts = [
    emptyHiddenCount > 0 ? `${emptyHiddenCount} ${t("emptyNodesHidden")}` : "",
    !showInternal && internalHiddenCount > 0 ? `${internalHiddenCount} ${t("internalMessagesHidden")}` : "",
  ].filter(Boolean);
  const hiddenNodeSummary = hiddenNodeSummaryParts.length ? ` · ${hiddenNodeSummaryParts.join(" · ")}` : "";
  const filterOnlyMatch = Boolean(
    searchActive && !hasBodySearchText && !titleOnlyMatch && !totalHitCount && !hiddenInternalCount &&
    (filters.role || filters.source || filters.after || filters.before || querySyntax.hasFilterOnlyContext),
  );
  const hitCounterText = searchActive
    ? filterOnlyMatch
      ? t("filterOnlyMatchNotice")
      : currentViewHits.length
      ? `${hitIndex + 1} / ${currentViewHits.length} ${t("visibleHits")} · ${t("showing")} ${totalHitCount}${hitExactTotal !== null ? ` ${t("of")} ${hitExactTotal}` : ""} ${t("totalHits")}`
      : titleOnlyMatch
        ? t("titleOnlyHitNotice")
        : totalHitCount || hiddenInternalCount
          ? t("hiddenHitsOnlyNotice")
          : t("noHits")
    : "";

  return (
    <main className="reader">
      <header className="reader-header">
        <div>
          <h1>{conversation.title || t("untitled")}</h1>
          <p>
            {t("created")} {formatDate(conversation.create_time)} · {t("updated")} {formatDate(conversation.update_time)} · {visibleMessages.length} {t("of")} {readerVisibleTotal || 0} {t("visibleMessages")}{hiddenNodeSummary}
          </p>
        </div>
        <div className="reader-actions">
          <select value={effectivePath} onChange={(event) => setPath(event.target.value as PathMode)} aria-label={t("messagePath")} disabled={Boolean(querySyntax.pathOverride)}>
            <option value="current">{t("currentPath")}</option>
            <option value="all">{t("allNodes")}</option>
          </select>
          <label className="toggle-inline">
            <input type="checkbox" checked={showInternal} onChange={(event) => setShowInternal(event.target.checked)} />
            {t("showInternalMessages")}
          </label>
          <button type="button" onClick={() => jump(-1)} disabled={!currentViewHits.length}>{t("prevHit")}</button>
          <button type="button" onClick={() => jump(1)} disabled={!currentViewHits.length}>{t("nextHit")}</button>
          <button type="button" onClick={copyVisible} disabled={Boolean(copyBusy)}>{copyBusy === "visible" ? t("preparingCopy") : t("copyVisible")}</button>
          <button type="button" onClick={copyConversation} disabled={Boolean(copyBusy)}>{copyBusy === "conversation" ? t("preparingCopy") : effectivePath === "all" ? t("copyAllNodesConversation") : t("copyConversation")}</button>
          <a className="button-link" href={exportUrl(conversation.conversation_id, "md", effectivePath, showInternal)} title={t("downloadUsesCurrentReaderPath")}>{t("downloadMd")}</a>
          <a className="button-link" href={exportUrl(conversation.conversation_id, "txt", effectivePath, showInternal)} title={t("downloadUsesCurrentReaderPath")}>{t("downloadTxt")}</a>
        </div>
      </header>
      {error && <div className="error-box">{error}</div>}
      {effectivePath === "current" && currentPathFallbackToAll && <div className="fallback-note" role="status">{t("currentPathFallbackAll")}</div>}
      <div className="hit-counter">{hitCounterText}{copyStatus ? ` · ${copyStatus}` : ""}</div>
      {(searchActive || querySyntax.pathOverride || querySyntax.scopeOverride || hiddenNodeSummaryParts.length > 0) && (
        <div className="search-visibility-notes" role="status">
          {filterOnlyMatch && <span>{t("filterOnlyMatchDescription")}</span>}
          {titleOnlyMatch && <span>{t("titleOnlyHitDescription")}</span>}
          {hiddenInternalCount > 0 && !showInternal && (
            <span>
              {hiddenInternalCount} {t("internalHitsHidden")}
              <button type="button" onClick={() => setShowInternal(true)}>{t("showInternalToView")}</button>
            </span>
          )}
          {Boolean((totalHitCount || hiddenInternalCount) && !currentViewHits.length && !titleOnlyMatch && !filterOnlyMatch) && <span>{t("hiddenHitsOnlyDescription")}</span>}
          {hitLoadingMore && <span>{t("loadingMore")}</span>}
          {hitLimitReached && <span>{t("hitNavigationLimited")}</span>}
          {hasBodySearchText && <span>{t("browserFindLimited")}</span>}
          {querySyntax.pathOverride && <span>{t("queryOverridesPath")} {effectivePath === "all" ? t("allNodes") : t("currentPath")}</span>}
          {querySyntax.scopeOverride && <span>{t("queryOverridesScope")} {effectiveScope === "title" ? t("titleOnly") : effectiveScope === "message" ? t("messagesOnly") : t("titleMessages")}</span>}
          {hiddenNodeSummaryParts.length > 0 && (
            <span>
              {hiddenNodeSummaryParts.join(" · ")}
              {!showInternal && internalHiddenCount > 0 && <button type="button" onClick={() => setShowInternal(true)}>{t("showInternalToView")}</button>}
            </span>
          )}
        </div>
      )}
      <div className="message-page-meta">
        {loading ? t("loading") : `${t("showing")} ${visibleMessages.length} ${t("of")} ${readerVisibleTotal} ${t("visibleMessages")}${hiddenNodeSummary}`}
        {hasMore && <button type="button" onClick={loadMore} disabled={loadingMore}>{loadingMore ? t("loadingMore") : t("loadMoreMessages")}</button>}
      </div>
      <div ref={parentRef} className="message-scroll" aria-label={t("messages")} data-message-layout={settings.messageLayout} onScroll={onMessageScroll}>
        <div style={{ height: `${rowVirtualizer.getTotalSize()}px`, position: "relative" }}>
          {rowVirtualizer.getVirtualItems().map((virtualRow) => {
            const message = visibleMessages[virtualRow.index];
            return (
              <VirtualMessageRow
                key={virtualRow.key}
                virtualKey={String(virtualRow.key)}
                index={virtualRow.index}
                start={virtualRow.start}
                measureElement={rowVirtualizer.measureElement}
              >
                {(onSizeMayChange) => (
                  <MessageBlock
                    message={message}
                    conversationId={conversation.conversation_id}
                    stateContextKey={messageStateContextKey}
                    active={message.node_id === activeNode}
                    activeTargetOffset={message.node_id === activeNode ? activeHit?.match_char_offset : null}
                    activeMatchLength={message.node_id === activeNode ? activeHit?.match_length : null}
                    activeRevision={message.node_id === activeNode ? activeHit?.display_anchor_revision : null}
                    layout={settings.messageLayout}
                    showRawDefault={settings.showRawDefault}
                    t={t}
                    onCopy={copyText}
	                    onSizeMayChange={onSizeMayChange}
	                    currentPathFallbackToAll={currentPathFallbackToAll || Boolean(conversation.current_path_fallback_to_all)}
	                  />
                )}
              </VirtualMessageRow>
            );
          })}
        </div>
      </div>
    </main>
  );
}

function VirtualMessageRow({
  virtualKey,
  index,
  start,
  measureElement,
  children,
}: {
  virtualKey: string;
  index: number;
  start: number;
  measureElement: (element: HTMLElement | null) => void;
  children: (onSizeMayChange: () => void) => ReactNode;
}) {
  const rowRef = useRef<HTMLDivElement | null>(null);
  const frameRef = useRef<number | null>(null);

  const scheduleMeasure = useCallback(() => {
    if (frameRef.current !== null) return;
    frameRef.current = window.requestAnimationFrame(() => {
      if (rowRef.current) measureElement(rowRef.current);
      frameRef.current = window.requestAnimationFrame(() => {
        frameRef.current = null;
        if (rowRef.current) measureElement(rowRef.current);
      });
    });
  }, [measureElement]);

  const setRowRef = useCallback((node: HTMLDivElement | null) => {
    rowRef.current = node;
    measureElement(node);
    if (node) scheduleMeasure();
  }, [measureElement, scheduleMeasure]);

  useEffect(() => {
    const node = rowRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => scheduleMeasure());
    observer.observe(node);
    return () => observer.disconnect();
  }, [virtualKey, scheduleMeasure]);

  useEffect(() => {
    scheduleMeasure();
    return () => {
      if (frameRef.current !== null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
    };
  }, [virtualKey, scheduleMeasure]);

  return (
    <div
      ref={setRowRef}
      className="virtual-row"
      style={{ transform: `translateY(${start}px)` }}
      data-index={index}
    >
      {children(scheduleMeasure)}
    </div>
  );
}
