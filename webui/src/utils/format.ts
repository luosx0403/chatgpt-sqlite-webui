export function formatDate(value: number | null | undefined): string {
  if (!value) return "";
  return new Date(value * 1000).toLocaleString();
}

export function shortDate(value: number | null | undefined): string {
  if (!value) return "";
  return new Date(value * 1000).toLocaleDateString();
}

export function roleLabel(role: string | null | undefined): string {
  if (!role) return "message";
  return role;
}
