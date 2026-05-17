import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { ConversationSummary, MessageItem, PathMode, SearchFilters } from "../types";
import { exportUrl, getMessageHits, getMessages } from "../api/client";
import { formatDate } from "../utils/format";
import MessageBlock from "./MessageBlock";
import type { Settings } from "../settings";

interface Props {
  conversation: ConversationSummary | null;
  query: string;
  filters: SearchFilters;
  path: PathMode;
  setPath: (value: PathMode) => void;
  settings: Settings;
  t: (key: string) => string;
}

export default function ConversationPane({ conversation, query, filters, path, setPath, settings, t }: Props) {
  const [messages, setMessages] = useState<MessageItem[]>([]);
  const [messageTotal, setMessageTotal] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [showInternal, setShowInternal] = useState(settings.showInternalDefault);
  const [hitIds, setHitIds] = useState<string[]>([]);
  const [hitIndex, setHitIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [copyStatus, setCopyStatus] = useState("");
  const parentRef = useRef<HTMLDivElement | null>(null);
  const messageRequestRef = useRef(0);
  const scrollRequestRef = useRef(0);
  const fallbackTargetsRef = useRef<Set<string>>(new Set());
  const replacementLoadRef = useRef(false);
  const missingHitWindowRef = useRef<Set<string>>(new Set());
  const filtersKey = JSON.stringify(filters);
  const highlightQuery = useMemo(() => [query, filters.exact ? `"${filters.exact}"` : ""].filter(Boolean).join(" "), [query, filters.exact]);

  useEffect(() => {
    setShowInternal(settings.showInternalDefault);
  }, [conversation?.conversation_id, settings.showInternalDefault]);

  useEffect(() => {
    fallbackTargetsRef.current.clear();
    missingHitWindowRef.current.clear();
  }, [conversation?.conversation_id, path, highlightQuery]);

  const loadMessages = useCallback((offset: number, append: boolean, aroundNodeId?: string) => {
    if (!conversation) return new AbortController();
    const controller = new AbortController();
    const requestId = ++messageRequestRef.current;
    if (!append) replacementLoadRef.current = true;
    if (append) setLoadingMore(true);
    else setLoading(true);
    setError(null);
    getMessages({ id: conversation.conversation_id, q: highlightQuery, path, offset, limit: settings.messagePageSize, aroundNodeId, signal: controller.signal })
      .then((page) => {
        if (requestId !== messageRequestRef.current) return;
        setMessages((current) => append ? [...current, ...page.items] : page.items);
        setMessageTotal(page.total);
        setHasMore(page.has_more);
        setNextOffset(page.next_offset);
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError" && requestId === messageRequestRef.current) setError(err.message);
      })
      .finally(() => {
        if (requestId === messageRequestRef.current) {
          if (!append) replacementLoadRef.current = false;
          setLoading(false);
          setLoadingMore(false);
        }
      });
    return controller;
  }, [conversation?.conversation_id, path, highlightQuery, settings.messagePageSize]);

  useEffect(() => {
    if (!conversation) {
      setMessages([]);
      setMessageTotal(0);
      setHasMore(false);
      setNextOffset(null);
      return;
    }
    const controller = loadMessages(0, false);
    return () => controller.abort();
  }, [conversation?.conversation_id, path, highlightQuery, loadMessages]);

  useEffect(() => {
    if (!conversation || !highlightQuery.trim()) {
      setHitIds([]);
      setHitIndex(0);
      return;
    }
    const controller = new AbortController();
    const loadHits = async () => {
      const ids: string[] = [];
      let offset = 0;
      for (;;) {
        const page = await getMessageHits({ q: highlightQuery, conversationId: conversation.conversation_id, path, order: "display", limit: 100, offset, filters, signal: controller.signal });
        ids.push(...page.items.map((item) => item.node_id));
        if (!page.has_more || page.next_offset === null) break;
        offset = page.next_offset;
      }
      return ids;
    };
    loadHits()
      .then((ids) => {
        setHitIds(ids);
        setHitIndex(0);
      })
      .catch((err: Error) => {
        if (err.name !== "AbortError") setHitIds([]);
      });
    return () => controller.abort();
  }, [conversation?.conversation_id, path, highlightQuery, filtersKey]);

  const rowVirtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 180,
    overscan: 8
  });

  const activeNode = hitIds[hitIndex] || null;
  const activeIndex = useMemo(() => messages.findIndex((msg) => msg.node_id === activeNode), [messages, activeNode]);

  const scrollActiveHitIntoView = useCallback((nodeId: string, rowIndex: number) => {
    const requestId = ++scrollRequestRef.current;
    rowVirtualizer.scrollToIndex(rowIndex, { align: "center" });

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
      foundRow = foundRow || Boolean(row);
      foundMark = foundMark || Boolean(mark);
      if (mark) {
        mark.scrollIntoView({ block: "center", inline: "nearest", behavior: "auto" });
        if (isVisibleInScroller(mark, scrollEl)) return;
      }
      if (row && frame >= 10) {
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
  }, [hasMore, loadMessages, messages.length, nextOffset, rowVirtualizer]);

  useEffect(() => {
    if (!activeNode || activeIndex < 0) return;
    return scrollActiveHitIntoView(activeNode, activeIndex);
  }, [activeNode, activeIndex, messages.length, scrollActiveHitIntoView]);

  useEffect(() => {
    if (activeNode && activeIndex < 0 && !loading && !replacementLoadRef.current && !missingHitWindowRef.current.has(activeNode)) {
      missingHitWindowRef.current.add(activeNode);
      loadMessages(0, false, activeNode);
    }
  }, [activeNode, activeIndex, loading, loadMessages]);

  const copyText = async (text: string) => {
    try {
      await navigator.clipboard?.writeText(text);
      setCopyStatus(t("copied"));
    } catch {
      setCopyStatus(t("copyFailed"));
    }
    window.setTimeout(() => setCopyStatus(""), 1400);
  };
  const messageText = (m: MessageItem) => m.display_text || m.render_text || m.content_text || "";
  const formatMessagesForCopy = (items: MessageItem[]) => items.map((m) => `${m.role || "message"}:\n${messageText(m)}`).join("\n\n");
  const copyConversation = async () => {
    if (!conversation) return;
    try {
      const allMessages: MessageItem[] = [];
      let offset = 0;
      for (;;) {
        const page = await getMessages({ id: conversation.conversation_id, q: "", path, offset, limit: 300 });
        allMessages.push(...page.items);
        if (!page.has_more || page.next_offset === null) break;
        offset = page.next_offset;
      }
      await copyText(formatMessagesForCopy(allMessages));
    } catch {
      await copyText(formatMessagesForCopy(messages));
    }
  };
  const copyVisible = () => {
    const parent = parentRef.current;
    if (!parent) return copyConversation();
    const visible = rowVirtualizer.getVirtualItems().map((row) => messages[row.index]).filter(Boolean);
    return copyText(formatMessagesForCopy(visible));
  };
  const jump = (delta: number) => {
    if (!hitIds.length) return;
    const next = (hitIndex + delta + hitIds.length) % hitIds.length;
    setHitIndex(next);
    const target = hitIds[next];
    if (target && !messages.some((message) => message.node_id === target)) {
      loadMessages(0, false, target);
    }
  };

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
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
  });
  const loadMore = () => {
    if (!hasMore || nextOffset === null || loading || loadingMore) return;
    loadMessages(nextOffset, true);
  };

  if (!conversation) {
    return <main className="reader empty-state">{query ? t("emptySearch") : t("selectConversation")}</main>;
  }

  return (
    <main className="reader">
      <header className="reader-header">
        <div>
          <h1>{conversation.title || t("untitled")}</h1>
          <p>
            {t("created")} {formatDate(conversation.create_time)} · {t("updated")} {formatDate(conversation.update_time)} · {messages.length} {t("of")} {messageTotal || conversation.current_path_nodes || conversation.node_count || 0} {t("messages")}
          </p>
        </div>
        <div className="reader-actions">
          <select value={path} onChange={(event) => setPath(event.target.value as PathMode)} aria-label={t("messagePath")}>
            <option value="current">{t("currentPath")}</option>
            <option value="all">{t("allNodes")}</option>
          </select>
          <label className="toggle-inline">
            <input type="checkbox" checked={showInternal} onChange={(event) => setShowInternal(event.target.checked)} />
            {t("showInternalMessages")}
          </label>
          <button type="button" onClick={() => jump(-1)} disabled={!hitIds.length}>{t("prevHit")}</button>
          <button type="button" onClick={() => jump(1)} disabled={!hitIds.length}>{t("nextHit")}</button>
          <button type="button" onClick={copyVisible}>{t("copyVisible")}</button>
          <button type="button" onClick={copyConversation}>{t("copyConversation")}</button>
          <a className="button-link" href={exportUrl(conversation.conversation_id, "md", path)}>{t("downloadMd")}</a>
          <a className="button-link" href={exportUrl(conversation.conversation_id, "txt", path)}>{t("downloadTxt")}</a>
        </div>
      </header>
      {error && <div className="error-box">{error}</div>}
      <div className="hit-counter">{hitIds.length ? `${hitIndex + 1} / ${hitIds.length} ${t("hits")}` : query ? t("noHits") : ""}{copyStatus ? ` · ${copyStatus}` : ""}</div>
      <div className="message-page-meta">
        {loading ? t("loading") : `${t("showing")} ${messages.length} ${t("of")} ${messageTotal} ${t("messages")}`}
        {hasMore && <button type="button" onClick={loadMore} disabled={loadingMore}>{loadingMore ? t("loadingMore") : t("loadMoreMessages")}</button>}
      </div>
      <div ref={parentRef} className="message-scroll" aria-label={t("messages")}>
        <div style={{ height: `${rowVirtualizer.getTotalSize()}px`, position: "relative" }}>
          {rowVirtualizer.getVirtualItems().map((virtualRow) => {
            const message = messages[virtualRow.index];
            return (
              <div
                key={message.node_id}
                className="virtual-row"
                style={{ transform: `translateY(${virtualRow.start}px)` }}
                ref={rowVirtualizer.measureElement}
                data-index={virtualRow.index}
              >
                <MessageBlock
                  message={message}
                  conversationId={conversation.conversation_id}
                  active={message.node_id === activeNode}
                  collapseInternal={!showInternal}
                  showRawDefault={settings.showRawDefault}
                  t={t}
                  onCopy={copyText}
                />
              </div>
            );
          })}
        </div>
      </div>
    </main>
  );
}
