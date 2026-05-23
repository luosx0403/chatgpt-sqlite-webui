import { useEffect, useRef, useState } from "react";
import type { HighlightRange, MessageItem } from "../types";
import { formatDate, roleLabel } from "../utils/format";
import { getRawMessage } from "../api/client";
import type { MessageLayout } from "../settings";

interface Props {
  message: MessageItem;
  conversationId: string;
  active: boolean;
  layout: MessageLayout;
  showRawDefault: boolean;
  t: (key: string) => string;
  onCopy: (text: string) => Promise<boolean>;
  onSizeMayChange: () => void;
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
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}") || trimmed.length > 4000) return false;
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return false;
    const keys = Object.keys(parsed as Record<string, unknown>);
    return keys.some((key) => [
      "search_query",
      "open",
      "click",
      "find",
      "screenshot",
      "response_length",
      "ref_id",
    ].includes(key));
  } catch {
    return false;
  }
}

export default function MessageBlock({ message, conversationId, active, layout, showRawDefault, t, onCopy, onSizeMayChange }: Props) {
  const [showRaw, setShowRaw] = useState(showRawDefault);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [fullRaw, setFullRaw] = useState("");
  const [fullRawLoading, setFullRawLoading] = useState(false);
  const [fullRawError, setFullRawError] = useState("");
  const mountedRef = useRef(true);
  const measureFrameRef = useRef<number | null>(null);
  const rawControllerRef = useRef<AbortController | null>(null);
  const rawRequestIdRef = useRef(0);
  const role = roleLabel(message.role, t);
  const text = message.display_text || message.render_text || message.content_text || "";
  const placeholder = `[non-text content: ${message.content_type || "empty"}]`;
  const timestamp = formatDate(message.create_time || message.update_time);
  const shouldUseChat = layout === "chat";
  const isTechnicalPayload = looksLikeTechnicalPayload(message, text);
  const side = isTechnicalPayload ? "system" : chatSide(message);
  const shouldCollapseDetails = shouldUseChat && isTechnicalPayload;
  const articleClass = `message ${roleClass(message.role)} ${message.is_internal ? "message-internal" : ""} ${active ? "message-active" : ""}`;
  const messageIdentity = `${conversationId}:${message.node_id}:${message.message_id || ""}:${message.content_hash || ""}`;
  const messageIdentityRef = useRef(messageIdentity);
  messageIdentityRef.current = messageIdentity;
  const copy = () => { void onCopy(text || message.raw_preview || ""); };
  const notifySizeMayChange = () => {
    if (measureFrameRef.current !== null) return;
    measureFrameRef.current = window.requestAnimationFrame(() => {
      measureFrameRef.current = null;
      if (mountedRef.current) onSizeMayChange();
    });
  };
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      rawControllerRef.current?.abort();
      rawControllerRef.current = null;
      if (measureFrameRef.current !== null) {
        window.cancelAnimationFrame(measureFrameRef.current);
        measureFrameRef.current = null;
      }
    };
  }, []);
  useEffect(() => {
    rawRequestIdRef.current += 1;
    rawControllerRef.current?.abort();
    rawControllerRef.current = null;
    setShowRaw(showRawDefault);
    setDetailsOpen(false);
    setFullRaw("");
    setFullRawLoading(false);
    setFullRawError("");
    notifySizeMayChange();
  }, [messageIdentity, showRawDefault, layout]);
  useEffect(() => {
    if (active && shouldCollapseDetails) {
      setDetailsOpen(true);
      notifySizeMayChange();
    }
  }, [active, shouldCollapseDetails]);
  const openFullRaw = async () => {
    if (fullRaw) {
      rawRequestIdRef.current += 1;
      rawControllerRef.current?.abort();
      rawControllerRef.current = null;
      setFullRaw("");
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
      const data = await getRawMessage(conversationId, message.node_id, controller.signal);
      if (!mountedRef.current || requestId !== rawRequestIdRef.current || requestIdentity !== messageIdentityRef.current) return;
      setFullRaw(JSON.stringify(data.raw_message, null, 2));
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

  const header = (
      <header className="message-header">
        <span className="role-pill">{role}</span>
        <span>{timestamp}</span>
        {!message.is_on_current_path && <span className="branch-pill">{t("branch")}</span>}
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
      <pre className="message-text">
        {(() => {
          let markIndex = 0;
          return pieces(text || placeholder, message.highlight_ranges).map((part, index) => {
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
      {showRaw && (
        <>
          <pre className="raw-message">{message.raw_preview || t("noRawStored")}</pre>
          {message.has_raw && <button type="button" className="raw-full-button" onClick={openFullRaw} disabled={fullRawLoading}>{fullRawLoading ? t("fullRawLoading") : fullRaw ? t("closeFullRaw") : t("openFullRaw")}</button>}
          {fullRawError && <p className="raw-error">{fullRawError}</p>}
          {fullRaw && <pre className="raw-message raw-full">{fullRaw}</pre>}
        </>
      )}
    </>
  );

  if (shouldCollapseDetails) {
    return (
      <div className={`message-row message-row-chat message-row-${side}`}>
        <details className={`${articleClass} message-disclosure`} data-node-id={message.node_id} open={detailsOpen} onToggle={(event) => { setDetailsOpen(event.currentTarget.open); notifySizeMayChange(); }}>
          <summary className="message-summary">
            <span className="role-pill">{role}</span>
            <span>{timestamp}</span>
            {message.is_internal && <span className="branch-pill">{t("internal")}</span>}
            {!message.is_on_current_path && <span className="branch-pill">{t("branch")}</span>}
            <span>{message.content_type || "text"} {t("hiddenInternal")}</span>
          </summary>
          {header}
          {body}
        </details>
      </div>
    );
  }

  const article = (
    <article className={articleClass} data-node-id={message.node_id}>
      {header}
      {body}
    </article>
  );

  if (!shouldUseChat) return article;
  return <div className={`message-row message-row-chat message-row-${side}`}>{article}</div>;
}
