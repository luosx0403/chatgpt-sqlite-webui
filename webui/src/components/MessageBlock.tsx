import { useEffect, useRef, useState } from "react";
import type { HighlightRange, MessageItem } from "../types";
import { formatDate, roleLabel } from "../utils/format";
import { CopyLimitError, IncompleteDisplayRecoveryError, MAX_BROWSER_COPY_BYTES, MAX_BROWSER_COPY_CHARS, getMessageDisplayChunk, getRawMessage } from "../api/client";
import type { MessageLayout } from "../settings";

interface Props {
  message: MessageItem;
  conversationId: string;
  stateContextKey: string;
  active: boolean;
  activeTargetOffset?: number | null;
  activeMatchLength?: number | null;
  activeRevision?: string | null;
  activeAnchorCursor?: string | null;
  layout: MessageLayout;
  showRawDefault: boolean;
  t: (key: string) => string;
  onCopy: (text: string) => Promise<boolean>;
  onSizeMayChange: () => void;
  currentPathFallbackToAll?: boolean;
}

interface PreservedMessageState {
  showRaw: boolean;
  detailsOpen: boolean;
  fullRaw: string;
  fullRawTruncated: boolean;
  expandedText: string | null;
  displayNextOffset: number | null;
  displayNextCursor: string | null;
  displayRecoveryIncomplete: boolean;
}

const preservedMessageStates = new Map<string, PreservedMessageState>();
const PRESERVED_STATE_MAX_ENTRIES = 100;
const PRESERVED_STATE_MAX_CHARS = 4 * 1024 * 1024;

function preserveMessageState(key: string, state: PreservedMessageState, showRawDefault: boolean) {
  if (
    state.showRaw === showRawDefault &&
    !state.detailsOpen &&
    !state.fullRaw &&
    !state.fullRawTruncated &&
    state.expandedText === null &&
    !state.displayRecoveryIncomplete
  ) {
    preservedMessageStates.delete(key);
    return;
  }
  preservedMessageStates.delete(key);
  preservedMessageStates.set(key, state);
  let chars = 0;
  for (const value of preservedMessageStates.values()) chars += value.fullRaw.length + (value.expandedText?.length ?? 0);
  while (preservedMessageStates.size > PRESERVED_STATE_MAX_ENTRIES || chars > PRESERVED_STATE_MAX_CHARS) {
    const oldest = preservedMessageStates.entries().next().value as [string, PreservedMessageState] | undefined;
    if (!oldest) break;
    preservedMessageStates.delete(oldest[0]);
    chars -= oldest[1].fullRaw.length + (oldest[1].expandedText?.length ?? 0);
  }
}

function pieces(text: string, ranges: HighlightRange[]) {
  if (!ranges.length) return [text];
  const out: Array<string | { text: string; mark: true }> = [];
  let cursor = 0;
  const normalizedRanges = ranges
    .map((range) => ({
      start: Math.max(0, Math.floor(range.start)),
      end: Math.min(text.length, Math.floor(range.end))
    }))
    .filter((range) => range.end > range.start && range.start < text.length)
    .sort((a, b) => a.start - b.start || b.end - a.end)
    .reduce<Array<{ start: number; end: number }>>((merged, range) => {
      const previous = merged[merged.length - 1];
      if (!previous || range.start > previous.end) merged.push({ ...range });
      else previous.end = Math.max(previous.end, range.end);
      return merged;
    }, []);
  for (const range of normalizedRanges) {
    const start = Math.max(0, Math.floor(range.start));
    const end = Math.min(text.length, Math.floor(range.end));
    if (start < cursor || start >= text.length || end <= start) continue;
    if (start > cursor) out.push(text.slice(cursor, start));
    out.push({ text: text.slice(start, end), mark: true });
    cursor = end;
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}

function roleClass(role: string | null): string {
  const safe = (role || "message").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "message";
  return `message-role-${safe}`;
}

function chatSide(message: MessageItem): "user" | "assistant" | "system" | "neutral" {
  const role = (message.role || "").toLowerCase();
  if (message.is_internal || role === "system" || role === "developer" || role.startsWith("tool")) return "system";
  if (role === "user") return "user";
  if (role === "assistant") return "assistant";
  return "neutral";
}

function looksLikeTechnicalPayload(message: MessageItem, text: string): boolean {
  const role = (message.role || "").toLowerCase();
  if (message.is_internal || role === "system" || role === "developer" || role.startsWith("tool")) return true;
  const contentType = (message.content_type || "").toLowerCase();
  if (role !== "assistant") return false;
  const trimmed = text.trim();
  if (contentType === "thoughts" || trimmed.startsWith("source analysis msg id:")) return true;
  return false;
}

export default function MessageBlock({ message, conversationId, stateContextKey, active, activeTargetOffset = null, activeMatchLength = null, activeRevision = null, activeAnchorCursor = null, layout, showRawDefault, t, onCopy, onSizeMayChange, currentPathFallbackToAll = false }: Props) {
  const messageIdentity = JSON.stringify([conversationId, message.node_id, message.message_id || "", message.content_hash || ""]);
  const preservedStateKey = JSON.stringify([stateContextKey, messageIdentity, showRawDefault ? "raw" : "plain"]);
  const savedState = preservedMessageStates.get(preservedStateKey);
  const [showRaw, setShowRaw] = useState(() => savedState?.showRaw ?? showRawDefault);
  const [detailsOpen, setDetailsOpen] = useState(() => savedState?.detailsOpen ?? false);
  const [fullRaw, setFullRaw] = useState(() => savedState?.fullRaw ?? "");
  const [fullRawTruncated, setFullRawTruncated] = useState(() => savedState?.fullRawTruncated ?? false);
  const [fullRawLoading, setFullRawLoading] = useState(false);
  const [fullRawError, setFullRawError] = useState("");
  const [expandedText, setExpandedText] = useState<string | null>(() => savedState?.expandedText ?? null);
  const [displayNextOffset, setDisplayNextOffset] = useState<number | null>(() =>
    savedState && Object.prototype.hasOwnProperty.call(savedState, "displayNextOffset")
      ? savedState.displayNextOffset
      : 0,
  );
  const [displayNextCursor, setDisplayNextCursor] = useState<string | null>(() => savedState?.displayNextCursor ?? null);
  const [displayRecoveryIncomplete, setDisplayRecoveryIncomplete] = useState(
    () => savedState?.displayRecoveryIncomplete ?? Boolean(message.display_text_resolver_input_truncated),
  );
  const [displayLoading, setDisplayLoading] = useState(false);
  const [displayError, setDisplayError] = useState("");
  const [activeWindowRange, setActiveWindowRange] = useState<HighlightRange | null>(null);
  const FULL_RAW_MAX_CHARS = 50000;
  const mountedRef = useRef(true);
  const measureFrameRef = useRef<number | null>(null);
  const focusFrameRef = useRef<number | null>(null);
  const rawControllerRef = useRef<AbortController | null>(null);
  const displayControllerRef = useRef<AbortController | null>(null);
  const rawRequestIdRef = useRef(0);
  const displayRequestIdRef = useRef(0);
  const resetBaselineRef = useRef(false);
  const bodyRef = useRef<HTMLPreElement | null>(null);
  const role = roleLabel(message.role, t);
  const previewText = message.display_text || "";
  const previewCodePointLength = message.display_text_returned_chars ?? Array.from(previewText).length;
  const text = expandedText ?? previewText;
  const placeholder = `[non-text content: ${message.content_type || "empty"}]`;
  const timestamp = formatDate(message.create_time ?? message.update_time);
  const shouldUseChat = layout === "chat";
  const isTechnicalPayload = looksLikeTechnicalPayload(message, text);
  const side = isTechnicalPayload ? "system" : chatSide(message);
  const shouldCollapseDetails = shouldUseChat && isTechnicalPayload;
  const articleClass = `message ${roleClass(message.role)} ${message.is_internal ? "message-internal" : ""} ${active ? "message-active" : ""}`;
  const showBranchBadge = !message.effective_visible_in_current_view && !message.current_path_fallback_to_all && !currentPathFallbackToAll;
  const preservedStateRef = useRef<PreservedMessageState>({ showRaw, detailsOpen, fullRaw, fullRawTruncated, expandedText, displayNextOffset, displayNextCursor, displayRecoveryIncomplete });
  preservedStateRef.current = { showRaw, detailsOpen, fullRaw, fullRawTruncated, expandedText, displayNextOffset, displayNextCursor, displayRecoveryIncomplete };
  const messageIdentityRef = useRef(messageIdentity);
  messageIdentityRef.current = messageIdentity;
  const copy = async () => {
    if (message.display_text_resolver_input_truncated || displayRecoveryIncomplete) {
      setDisplayRecoveryIncomplete(true);
      setDisplayError(t("displayRecoveryIncomplete"));
      return;
    }
    if (!message.display_text_truncated && expandedText === null) {
      await onCopy(text || message.raw_preview || "");
      return;
    }
    displayControllerRef.current?.abort();
    const controller = new AbortController();
    displayControllerRef.current = controller;
    const requestId = ++displayRequestIdRef.current;
    const requestIdentity = messageIdentity;
    setDisplayLoading(true);
    setDisplayError("");
    setActiveWindowRange(null);
    try {
      let offset = 0;
      let cursor: string | null = null;
      let complete = "";
      let completeBytes = 0;
      while (true) {
        const chunk = await getMessageDisplayChunk(conversationId, message.node_id, offset, 1048576, controller.signal, cursor);
        if (requestId !== displayRequestIdRef.current || requestIdentity !== messageIdentityRef.current) return;
        if (chunk.resolver_input_truncated || (!chunk.has_more && !chunk.total_chars_exact)) {
          setDisplayRecoveryIncomplete(true);
          throw new IncompleteDisplayRecoveryError();
        }
        completeBytes += new TextEncoder().encode(chunk.display_text).byteLength;
        if (complete.length + chunk.display_text.length > MAX_BROWSER_COPY_CHARS || completeBytes > MAX_BROWSER_COPY_BYTES) {
          throw new CopyLimitError();
        }
        complete += chunk.display_text;
        if (!chunk.has_more || chunk.next_offset === null) break;
        offset = chunk.next_offset;
        cursor = chunk.next_cursor;
      }
      setExpandedText(complete);
      setDisplayNextOffset(null);
      setDisplayNextCursor(null);
      await onCopy(complete || message.raw_preview || "");
    } catch (error) {
      if (
        mountedRef.current &&
        requestId === displayRequestIdRef.current &&
        requestIdentity === messageIdentityRef.current &&
        !(error instanceof Error && error.name === "AbortError")
      ) setDisplayError(
        error instanceof CopyLimitError
          ? t("copyTooLarge")
          : error instanceof IncompleteDisplayRecoveryError
            ? t("displayRecoveryIncomplete")
            : t("displayTextFailed"),
      );
    } finally {
      if (requestId === displayRequestIdRef.current && requestIdentity === messageIdentityRef.current) {
        displayControllerRef.current = null;
        setDisplayLoading(false);
        notifySizeMayChange();
      }
    }
  };
  const notifySizeMayChange = () => {
    if (measureFrameRef.current !== null) return;
    measureFrameRef.current = window.requestAnimationFrame(() => {
      measureFrameRef.current = null;
      if (mountedRef.current) onSizeMayChange();
    });
  };
  useEffect(() => {
    mountedRef.current = true;
    if (savedState) notifySizeMayChange();
    return () => {
      preserveMessageState(preservedStateKey, preservedStateRef.current, showRawDefault);
      mountedRef.current = false;
      rawControllerRef.current?.abort();
      displayControllerRef.current?.abort();
      rawControllerRef.current = null;
      if (measureFrameRef.current !== null) {
        window.cancelAnimationFrame(measureFrameRef.current);
        measureFrameRef.current = null;
      }
      if (focusFrameRef.current !== null) {
        window.cancelAnimationFrame(focusFrameRef.current);
        focusFrameRef.current = null;
      }
    };
  }, [preservedStateKey]);
  useEffect(() => {
    if (!resetBaselineRef.current) {
      resetBaselineRef.current = true;
      return;
    }
    rawRequestIdRef.current += 1;
    rawControllerRef.current?.abort();
    rawControllerRef.current = null;
    setShowRaw(showRawDefault);
    setDetailsOpen(false);
    setFullRaw("");
    setFullRawTruncated(false);
    setFullRawLoading(false);
    setFullRawError("");
    displayRequestIdRef.current += 1;
    displayControllerRef.current?.abort();
    displayControllerRef.current = null;
    setExpandedText(null);
    setDisplayNextOffset(0);
    setDisplayNextCursor(null);
    setDisplayRecoveryIncomplete(Boolean(message.display_text_resolver_input_truncated));
    setDisplayLoading(false);
    setDisplayError("");
    notifySizeMayChange();
  }, [messageIdentity, showRawDefault]);
  useEffect(() => {
    if (active && shouldCollapseDetails) {
      setDetailsOpen(true);
      notifySizeMayChange();
    }
  }, [active, shouldCollapseDetails]);
  useEffect(() => {
    if (!active || activeTargetOffset === null || activeTargetOffset < previewCodePointLength) {
      setActiveWindowRange(null);
      return;
    }
    const controller = new AbortController();
    const requestIdentity = messageIdentity;
    const requestId = ++displayRequestIdRef.current;
    displayControllerRef.current?.abort();
    displayControllerRef.current = controller;
    setDisplayLoading(true);
    void getMessageDisplayChunk(
      conversationId,
      message.node_id,
      activeAnchorCursor ? activeTargetOffset : 0,
      1048576,
      controller.signal,
      activeAnchorCursor,
      activeAnchorCursor ? null : activeTargetOffset,
    ).then((chunk) => {
      if (controller.signal.aborted || requestId !== displayRequestIdRef.current || requestIdentity !== messageIdentityRef.current) return;
      if (activeRevision && chunk.content_revision !== activeRevision) {
        setDisplayError(t("displayTextFailed"));
        return;
      }
      const codePointStart = Math.max(0, chunk.anchor_offset_in_chunk ?? 0);
      const codePointLength = Math.max(1, activeMatchLength ?? 1);
      const start = Array.from(chunk.display_text).slice(0, codePointStart).join("").length;
      const end = start + Array.from(chunk.display_text).slice(codePointStart, codePointStart + codePointLength).join("").length;
      setExpandedText(chunk.display_text);
      setDisplayNextOffset(null);
      setDisplayNextCursor(null);
      setActiveWindowRange({ start, end });
      requestAnimationFrame(() => bodyRef.current?.querySelector("mark.search-highlight-active")?.scrollIntoView({ block: "center" }));
    }).catch((error: unknown) => {
      if (!(error instanceof Error && error.name === "AbortError")) setDisplayError(t("displayTextFailed"));
    }).finally(() => {
      if (requestId === displayRequestIdRef.current) {
        setDisplayLoading(false);
        displayControllerRef.current = null;
        notifySizeMayChange();
      }
    });
    return () => controller.abort();
  }, [active, activeAnchorCursor, activeMatchLength, activeRevision, activeTargetOffset, conversationId, message.node_id, messageIdentity, previewCodePointLength, t]);
  const openFullRaw = async () => {
    if (fullRaw) {
      rawRequestIdRef.current += 1;
      rawControllerRef.current?.abort();
      rawControllerRef.current = null;
      setFullRaw("");
      setFullRawTruncated(false);
      setFullRawError("");
      setFullRawLoading(false);
      notifySizeMayChange();
      return;
    }
    rawControllerRef.current?.abort();
    const controller = new AbortController();
    rawControllerRef.current = controller;
    const requestId = ++rawRequestIdRef.current;
    const requestIdentity = messageIdentity;
    setFullRawLoading(true);
    setFullRawError("");
    notifySizeMayChange();
    try {
      const data = await getRawMessage(conversationId, message.node_id, controller.signal, FULL_RAW_MAX_CHARS);
      if (!mountedRef.current || requestId !== rawRequestIdRef.current || requestIdentity !== messageIdentityRef.current) return;
      if (data.truncated && typeof data.raw_text === "string") {
        setFullRaw(data.raw_text);
      } else if (typeof data.raw_message === "object" && data.raw_message !== null) {
        setFullRaw(JSON.stringify(data.raw_message, null, 2));
      } else if (typeof data.raw_message === "string") {
        setFullRaw(data.raw_message);
      } else {
        setFullRaw(String(data.raw_message ?? ""));
      }
      setFullRawTruncated(Boolean(data.truncated));
    } catch (error) {
      if (
        !mountedRef.current ||
        requestId !== rawRequestIdRef.current ||
        requestIdentity !== messageIdentityRef.current ||
        (error instanceof Error && error.name === "AbortError")
      ) return;
      setFullRawError(t("fullRawFailed"));
    } finally {
      if (mountedRef.current && requestId === rawRequestIdRef.current && requestIdentity === messageIdentityRef.current) {
        rawControllerRef.current = null;
        setFullRawLoading(false);
        notifySizeMayChange();
      }
    }
  };
  const toggleRawPreview = () => {
    setShowRaw((current) => {
      if (current) {
        rawRequestIdRef.current += 1;
        rawControllerRef.current?.abort();
        rawControllerRef.current = null;
        setFullRaw("");
        setFullRawError("");
        setFullRawLoading(false);
      }
      return !current;
    });
    notifySizeMayChange();
  };
  const loadDisplayText = async () => {
    displayControllerRef.current?.abort();
    const controller = new AbortController();
    displayControllerRef.current = controller;
    const requestId = ++displayRequestIdRef.current;
    const requestIdentity = messageIdentity;
    const offset = expandedText === null ? 0 : (displayNextOffset ?? 0);
    setDisplayLoading(true);
    setDisplayError("");
    try {
      const chunk = await getMessageDisplayChunk(conversationId, message.node_id, offset, 1048576, controller.signal, displayNextCursor);
      if (requestId !== displayRequestIdRef.current || requestIdentity !== messageIdentityRef.current) return;
      const incomplete = chunk.resolver_input_truncated || (!chunk.has_more && !chunk.total_chars_exact);
      if (incomplete) setDisplayRecoveryIncomplete(true);
      setExpandedText((current) => offset === 0 ? chunk.display_text : `${current ?? ""}${chunk.display_text}`);
      setDisplayNextOffset(chunk.next_offset);
      setDisplayNextCursor(chunk.next_cursor);
      if (!chunk.has_more) {
        if (focusFrameRef.current !== null) window.cancelAnimationFrame(focusFrameRef.current);
        focusFrameRef.current = window.requestAnimationFrame(() => {
          focusFrameRef.current = null;
          if (
            mountedRef.current &&
            requestId === displayRequestIdRef.current &&
            requestIdentity === messageIdentityRef.current &&
            bodyRef.current?.isConnected
          ) bodyRef.current.focus();
        });
      }
    } catch (error) {
      if (
        mountedRef.current &&
        requestId === displayRequestIdRef.current &&
        requestIdentity === messageIdentityRef.current &&
        !(error instanceof Error && error.name === "AbortError")
      ) setDisplayError(t("displayTextFailed"));
    } finally {
      if (requestId === displayRequestIdRef.current && requestIdentity === messageIdentityRef.current) {
        displayControllerRef.current = null;
        setDisplayLoading(false);
        notifySizeMayChange();
      }
    }
  };

  const header = (
      <header className="message-header">
        <span className="role-pill">{role}</span>
        <span>{timestamp}</span>
        {showBranchBadge && <span className="branch-pill">{t("branch")}</span>}
        {message.is_internal && <span className="branch-pill">{t("internal")}</span>}
        {message.has_raw && (
          <button type="button" className="icon-button" onClick={toggleRawPreview}>
            {showRaw ? t("hideRawPreview") : t("showRawPreview")}
          </button>
        )}
        <button type="button" className="icon-button" onClick={copy} title={t("copy")}>{t("copy")}</button>
      </header>
  );

  const body = (
    <>
      <pre ref={bodyRef} tabIndex={-1} className="message-text">
        {(() => {
          let markIndex = 0;
          const ranges = active && activeWindowRange ? [activeWindowRange] : message.highlight_ranges;
          return pieces(text || placeholder, ranges).map((part, index) => {
            if (typeof part === "string") return part;
            const isActiveMark = active && markIndex === 0;
            markIndex += 1;
            return (
              <mark
                key={index}
                className={`search-highlight${isActiveMark ? " search-highlight-active" : ""}`}
                data-active-search-hit={isActiveMark ? "true" : undefined}
              >
                {part.text}
              </mark>
            );
          });
        })()}
      </pre>
      {message.highlight_ranges_truncated && <p className="hint">{t("highlightRangesTruncated")}</p>}
      {active && message.highlight_truncated && message.highlight_ranges.length === 0 && <p className="hint">{t("activeHighlightUnavailable")}</p>}
      {(message.display_text_truncated || expandedText !== null || displayRecoveryIncomplete) && (
        <div className="hint">
          {message.display_text_truncated && expandedText === null && <span>{t("displayTextTruncated")} </span>}
          {(expandedText === null || displayNextOffset !== null) && (
            <button type="button" onClick={loadDisplayText} disabled={displayLoading}>
              {displayLoading ? t("displayTextLoading") : expandedText === null ? t("loadDisplayText") : t("loadMoreDisplayText")}
            </button>
          )}
          {displayError && <span className="error-text" role="status">{displayError}</span>}
          {displayRecoveryIncomplete && !displayError && (
            <span className="error-text" role="status">{t("displayRecoveryIncomplete")}</span>
          )}
        </div>
      )}
      {showRaw && (
        <>
          <pre className="raw-message">{message.raw_preview || t("noRawStored")}</pre>
          {message.has_raw && <button type="button" className="raw-full-button" onClick={openFullRaw} disabled={fullRawLoading}>{fullRawLoading ? t("fullRawLoading") : fullRaw ? t("closeFullRaw") : t("openFullRaw")}</button>}
          {fullRawError && <p className="raw-error">{fullRawError}</p>}
          {fullRaw && <pre className="raw-message raw-full">{fullRaw}</pre>}
          {fullRawTruncated && <p className="raw-error">{t("rawTruncated")}</p>}
        </>
      )}
    </>
  );

  if (shouldCollapseDetails) {
    return (
      <div className={`message-row message-row-chat message-row-${side}`}>
        <details className={`${articleClass} message-disclosure`} data-node-id={message.node_id} data-raw-current-path={String(message.is_on_current_path)} open={detailsOpen} onToggle={(event) => { setDetailsOpen(event.currentTarget.open); notifySizeMayChange(); }}>
          <summary className="message-summary">
            <span className="role-pill">{role}</span>
            <span>{timestamp}</span>
            {message.is_internal && <span className="branch-pill">{t("internal")}</span>}
            {showBranchBadge && <span className="branch-pill">{t("branch")}</span>}
            <span>{message.content_type || "text"} {t("hiddenInternal")}</span>
          </summary>
          {header}
          {body}
        </details>
      </div>
    );
  }

  const article = (
    <article className={articleClass} data-node-id={message.node_id} data-raw-current-path={String(message.is_on_current_path)}>
      {header}
      {body}
    </article>
  );

  if (!shouldUseChat) return article;
  return <div className={`message-row message-row-chat message-row-${side}`}>{article}</div>;
}
