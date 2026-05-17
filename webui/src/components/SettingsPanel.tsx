import type { Language } from "../i18n";
import { DEFAULT_SETTINGS, type Settings } from "../settings";

interface Props {
  open: boolean;
  settings: Settings;
  t: (key: string) => string;
  onChange: (settings: Settings) => void;
  onClose: () => void;
}

export default function SettingsPanel({ open, settings, t, onChange, onClose }: Props) {
  if (!open) return null;
  const update = <K extends keyof Settings>(key: K, value: Settings[K]) => onChange({ ...settings, [key]: value });
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section className="modal settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title" onClick={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <h2 id="settings-title">{t("settings")}</h2>
          <button type="button" onClick={onClose}>{t("close")}</button>
        </header>
        <div className="settings-grid">
          <label>{t("language")}
            <select value={settings.language} onChange={(event) => update("language", event.target.value as Language)}>
              <option value="system">{t("systemDefault")}</option>
              <option value="en">{t("english")}</option>
              <option value="zh-Hans">{t("simplifiedChinese")}</option>
              <option value="zh-Hant">{t("traditionalChinese")}</option>
            </select>
          </label>
          <label>{t("theme")}
            <select value={settings.theme} onChange={(event) => update("theme", event.target.value as Settings["theme"])}>
              <option value="system">{t("system")}</option>
              <option value="light">{t("light")}</option>
              <option value="dark">{t("dark")}</option>
            </select>
          </label>
          <label>{t("density")}
            <select value={settings.density} onChange={(event) => update("density", event.target.value as Settings["density"])}>
              <option value="comfortable">{t("comfortable")}</option>
              <option value="compact">{t("compact")}</option>
            </select>
          </label>
          <label>{t("fontSize")}
            <input type="number" min="12" max="20" value={settings.fontSize} onChange={(event) => update("fontSize", Number(event.target.value))} />
          </label>
          <label>{t("sidebarWidth")}
            <input type="number" min="280" max="560" value={settings.sidebarWidth} onChange={(event) => update("sidebarWidth", Number(event.target.value))} />
          </label>
          <label>{t("messageMaxWidth")}
            <input type="number" min="640" max="1280" value={settings.messageMaxWidth} onChange={(event) => update("messageMaxWidth", Number(event.target.value))} />
          </label>
          <label>{t("listPageSize")}
            <input type="number" min="20" max="100" value={settings.listPageSize} onChange={(event) => update("listPageSize", Number(event.target.value))} />
          </label>
          <label>{t("messagePageSize")}
            <input type="number" min="50" max="300" value={settings.messagePageSize} onChange={(event) => update("messagePageSize", Number(event.target.value))} />
          </label>
        </div>
        <div className="settings-checks">
          <label><input type="checkbox" checked={settings.showInternalDefault} onChange={(event) => update("showInternalDefault", event.target.checked)} /> {t("defaultShowInternal")}</label>
          <label><input type="checkbox" checked={settings.showRawDefault} onChange={(event) => update("showRawDefault", event.target.checked)} /> {t("defaultShowRaw")}</label>
          <label><input type="checkbox" checked={settings.autoLoadMore} onChange={(event) => update("autoLoadMore", event.target.checked)} /> {t("autoLoadMore")}</label>
        </div>
        <footer className="modal-footer">
          <button type="button" onClick={() => onChange(DEFAULT_SETTINGS)}>{t("resetSettings")}</button>
        </footer>
      </section>
    </div>
  );
}
