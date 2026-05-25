import type { PathMode, SearchScope } from "../types";

const KNOWN_MODIFIERS = new Set(["role", "title", "source", "path", "scope", "before", "after"]);

export interface QuerySyntaxInfo {
  hasBodyText: boolean;
  hasSearchContext: boolean;
  hasTitleText: boolean;
  pathOverride: PathMode | null;
  scopeOverride: SearchScope | null;
}

export function analyzeQuerySyntax(raw: string): QuerySyntaxInfo {
  const text = raw.normalize("NFKC").toLocaleLowerCase().trim().replace(/\s+/g, " ");
  const info: QuerySyntaxInfo = { hasBodyText: false, hasSearchContext: false, hasTitleText: false, pathOverride: null, scopeOverride: null };
  if (!text) return info;
  for (const { token, quoted, negated, key } of queryTokens(text)) {
    if (!token) continue;
    if (quoted && !key) {
      info.hasSearchContext = true;
      if (!negated) info.hasBodyText = true;
      continue;
    }
    if (!key && token === "or" && !quoted) continue;
    if (!key && token.startsWith("-") && !token.startsWith("--") && token.length > 1) {
      info.hasSearchContext = true;
      continue;
    }
    if (key) {
      const value = token.normalize("NFKC").toLocaleLowerCase();
      if (value && (key === "role" || key === "source" || key === "before" || key === "after")) {
        info.hasSearchContext = true;
        continue;
      }
      if (value && key === "title") {
        info.hasSearchContext = true;
        info.hasTitleText = true;
        continue;
      }
      if (value && key === "path") {
        if (value === "current" || value === "all") {
          info.pathOverride = value;
        }
        continue;
      }
      if (value && key === "scope") {
        if (value === "all" || value === "title" || value === "message") {
          info.scopeOverride = value;
        }
        continue;
      }
      continue;
    }
    info.hasSearchContext = true;
    info.hasBodyText = true;
  }
  return info;
}

function queryTokens(text: string): Array<{ token: string; quoted: boolean; negated: boolean; key: string | null }> {
  const tokens: Array<{ token: string; quoted: boolean; negated: boolean; key: string | null }> = [];
  let index = 0;
  while (index < text.length) {
    while (index < text.length && /\s/.test(text[index])) index += 1;
    if (index >= text.length) break;
    let negated = false;
    if (text[index] === "-" && index + 1 < text.length && text[index + 1] !== "-" && !/\s/.test(text[index + 1])) {
      negated = true;
      index += 1;
    }
    if (text[index] === "\"") {
      const read = readQuoted(text, index + 1);
      tokens.push({ token: read.value, quoted: true, negated, key: null });
      index = read.index;
      continue;
    }
    const start = index;
    while (index < text.length && !/\s/.test(text[index]) && text[index] !== ":" && text[index] !== "\"") index += 1;
    const head = text.slice(start, index);
    if (index < text.length && text[index] === ":" && head) {
      const rawKey = head.normalize("NFKC").toLocaleLowerCase();
      if (KNOWN_MODIFIERS.has(rawKey)) {
        index += 1;
        if (index < text.length && text[index] === "\"") {
          const read = readQuoted(text, index + 1);
          tokens.push({ token: read.value, quoted: true, negated, key: rawKey });
          index = read.index;
        } else {
          const valueStart = index;
          while (index < text.length && !/\s/.test(text[index])) index += 1;
          tokens.push({ token: text.slice(valueStart, index), quoted: false, negated, key: rawKey });
        }
        continue;
      }
    }
    if (index < text.length && text[index] === ":") {
      index += 1;
      if (index < text.length && text[index] === "\"") {
        const read = readQuoted(text, index + 1);
        tokens.push({ token: `${head}:${read.value}`, quoted: false, negated, key: null });
        index = read.index;
      } else {
        const valueStart = index;
        while (index < text.length && !/\s/.test(text[index])) index += 1;
        tokens.push({ token: `${head}:${text.slice(valueStart, index)}`, quoted: false, negated, key: null });
      }
      if (negated) {
        const last = tokens[tokens.length - 1];
        tokens[tokens.length - 1] = { ...last, token: "-" + last.token };
      }
      continue;
    }
    if (index < text.length && text[index] === "\"") {
      const quoteStart = start;
      index += 1;
      while (index < text.length && text[index] !== "\"") index += 1;
      if (index < text.length) {
        index += 1;
        tokens.push({ token: text.slice(quoteStart, index), quoted: false, negated, key: null });
      } else {
        tokens.push({ token: text.slice(quoteStart, index), quoted: false, negated, key: null });
      }
      if (negated) {
        const last = tokens[tokens.length - 1];
        tokens[tokens.length - 1] = { ...last, token: "-" + last.token };
      }
      continue;
    }
    tokens.push({ token: negated ? `-${head}` : head, quoted: false, negated: false, key: null });
  }
  return tokens;
}

function readQuoted(text: string, index: number): { value: string; index: number } {
  let value = "";
  while (index < text.length) {
    const char = text[index];
    if (char === "\\" && index + 1 < text.length) {
      value += text[index + 1];
      index += 2;
      continue;
    }
    if (char === "\"") return { value, index: index + 1 };
    value += char;
    index += 1;
  }
  return { value, index };
}
