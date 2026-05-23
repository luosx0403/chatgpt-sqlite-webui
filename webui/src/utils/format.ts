export function formatDate(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return new Date(value * 1000).toLocaleString();
}

export function shortDate(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return new Date(value * 1000).toLocaleDateString();
}

export function roleLabel(role: string | null | undefined, t: (key: string) => string): string {
  const normalized = (role || "").toLowerCase().replace("_", "/");
  if (normalized === "user") return t("user");
  if (normalized === "assistant") return t("assistant");
  if (normalized === "system") return t("systemRole");
  if (normalized === "developer") return t("developer");
  if (normalized === "tool") return t("toolRole");
  if (normalized === "tool/system") return t("toolSystem");
  return t("messageRole");
}
