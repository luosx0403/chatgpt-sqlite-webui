import type { Language } from "./i18n";

export type ThemeMode = "system" | "light" | "dark";
export type DensityMode = "comfortable" | "compact";

export interface Settings {
  language: Language;
  theme: ThemeMode;
  fontSize: number;
  density: DensityMode;
  sidebarWidth: number;
  messageMaxWidth: number;
  showInternalDefault: boolean;
  showRawDefault: boolean;
  autoLoadMore: boolean;
  listPageSize: number;
  messagePageSize: number;
}

export const DEFAULT_SETTINGS: Settings = {
  language: "system",
  theme: "system",
  fontSize: 14,
  density: "comfortable",
  sidebarWidth: 360,
  messageMaxWidth: 940,
  showInternalDefault: false,
  showRawDefault: false,
  autoLoadMore: true,
  listPageSize: 60,
  messagePageSize: 300,
};

export const SETTINGS_KEY = "chatgptArchiveWeb.settings.v2";

export function clampSettings(input: Partial<Settings>): Settings {
  return {
    ...DEFAULT_SETTINGS,
    ...input,
    fontSize: clampNumber(input.fontSize, 12, 20, DEFAULT_SETTINGS.fontSize),
    sidebarWidth: clampNumber(input.sidebarWidth, 280, 560, DEFAULT_SETTINGS.sidebarWidth),
    messageMaxWidth: clampNumber(input.messageMaxWidth, 640, 1280, DEFAULT_SETTINGS.messageMaxWidth),
    listPageSize: clampNumber(input.listPageSize, 20, 100, DEFAULT_SETTINGS.listPageSize),
    messagePageSize: clampNumber(input.messagePageSize, 50, 300, DEFAULT_SETTINGS.messagePageSize),
    language: ["system", "en", "zh-Hans", "zh-Hant"].includes(String(input.language)) ? input.language as Settings["language"] : DEFAULT_SETTINGS.language,
    theme: ["system", "light", "dark"].includes(String(input.theme)) ? input.theme as Settings["theme"] : DEFAULT_SETTINGS.theme,
    density: input.density === "compact" ? "compact" : "comfortable",
  };
}

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? clampSettings(JSON.parse(raw)) : DEFAULT_SETTINGS;
  } catch {
    return DEFAULT_SETTINGS;
  }
}

export function saveSettings(settings: Settings) {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

export function applySettings(settings: Settings) {
  const root = document.documentElement;
  const systemDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const theme = settings.theme === "system" ? (systemDark ? "dark" : "light") : settings.theme;
  root.dataset.theme = theme;
  root.dataset.density = settings.density;
  root.style.setProperty("--app-font-size", `${settings.fontSize}px`);
  root.style.setProperty("--sidebar-width", `${settings.sidebarWidth}px`);
  root.style.setProperty("--message-max-width", `${settings.messageMaxWidth}px`);
}

function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(min, Math.min(max, Math.round(number)));
}
