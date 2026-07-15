interface Props {
  open: boolean;
  t: (key: string) => string;
  onClose: () => void;
}

export default function SearchHelp({ open, t, onClose }: Props) {
  const dialogRef = useModalFocus(open, onClose);
  if (!open) return null;
  const rows = [
    "helpPlain", "helpOr", "helpPhrase", "helpExclude", "helpCjk", "helpRole",
    "helpTitle", "helpDate", "helpSource", "helpFilterOnly", "helpOverrides", "helpBrowserFind", "helpAdvanced",
  ];
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section ref={dialogRef} tabIndex={-1} className="modal" role="dialog" aria-modal="true" aria-labelledby="search-help-title" onClick={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <h2 id="search-help-title">{t("searchHelp")}</h2>
          <button type="button" onClick={onClose}>{t("close")}</button>
        </header>
        <p>{t("helpIntro")}</p>
        <ul className="help-list">
          {rows.map((key) => <li key={key}>{t(key)}</li>)}
        </ul>
      </section>
    </div>
  );
}
import { useModalFocus } from "../hooks/useModalFocus";
