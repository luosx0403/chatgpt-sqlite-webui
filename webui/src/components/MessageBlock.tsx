import { useState } from "react";
import type { HighlightRange, MessageItem } from "../types";
import { formatDate, roleLabel } from "../utils/format";
import { getRawMessage } from "../api/client";

interface Props {
  message: MessageItem;
  conversationId: string;
  active: boolean;
  collapseInternal: boolean;
  showRawDefault: boolean;
  t: (key: string) => string;
  onCopy: (text: string) => void;
}

function pieces(text: string, ranges: HighlightRange[]) {
  if (!ranges.length) return [text];
  const out: Array<string | { text: string; mark: true }> = [];
  let cursor = 0;
  for (const range of ranges) {
    if (range.start < cursor || range.start >= text.length) continue;
    if (range.start > cursor) out.push(text.slice(cursor, range.start));
    out.push({ text: text.slice(range.start, Math.min(range.end, text.length)), mark: true });
    cursor = Math.min(range.end, text.length);
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}

function roleClass(role: string | null): string {
  const safe = (role || "message").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "message";
  return `message-role-${safe}`;
}

export default function MessageBlock({ message, conversationId, active, collapseInternal, showRawDefault, t, onCopy }: Props) {
  const [showRaw, setShowRaw] = useState(showRawDefault);
  const [expandedInternal, setExpandedInternal] = useState(false);
  const [fullRaw, setFullRaw] = useState("");
  const role = roleLabel(message.role);
  const text = message.display_text || message.render_text || message.content_text || "";
  const isCollapsed = collapseInternal && message.is_internal && !expandedInternal;
  const placeholder = `[non-text content: ${message.content_type || "empty"}]`;
  const copy = () => onCopy(text || message.raw_preview || "");
  const openFullRaw = async () => {
    if (fullRaw) {
      setFullRaw("");
      return;
    }
    const data = await getRawMessage(conversationId, message.node_id);
    setFullRaw(JSON.stringify(data.raw_message, null, 2));
  };
  return (
    <article className={`message ${roleClass(message.role)} ${message.is_internal ? "message-internal" : ""} ${active ? "message-active" : ""}`} data-node-id={message.node_id}>
      <header className="message-header">
        <span className="role-pill">{role}</span>
        <span>{formatDate(message.create_time || message.update_time)}</span>
        {!message.is_on_current_path && <span className="branch-pill">{t("branch")}</span>}
        {message.is_internal && <span className="branch-pill">{t("internal")}</span>}
        {message.is_internal && collapseInternal && (
          <button type="button" className="icon-button" onClick={() => setExpandedInternal(!expandedInternal)}>
            {expandedInternal ? t("collapse") : t("showInternal")}
          </button>
        )}
        {message.has_raw && (
          <button type="button" className="icon-button" onClick={() => setShowRaw(!showRaw)}>
            {showRaw ? t("hideRawPreview") : t("showRawPreview")}
          </button>
        )}
        <button type="button" className="icon-button" onClick={copy} title={t("copy")}>{t("copy")}</button>
      </header>
      {isCollapsed ? (
        <p className="collapsed-note">{message.content_type || role} {t("hiddenInternal")}</p>
      ) : (
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
      )}
      {showRaw && (
        <>
          <pre className="raw-message">{message.raw_preview || t("noRawStored")}</pre>
          {message.has_raw && <button type="button" className="raw-full-button" onClick={openFullRaw}>{fullRaw ? t("closeFullRaw") : t("openFullRaw")}</button>}
          {fullRaw && <pre className="raw-message raw-full">{fullRaw}</pre>}
        </>
      )}
    </article>
  );
}
