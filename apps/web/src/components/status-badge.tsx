/**
 * Status pill. Text is always present alongside the colour, so the state is not
 * conveyed by colour alone.
 */

export type StatusTone = "ok" | "error" | "warn";

export function StatusBadge({ tone, label }: { tone: StatusTone; label: string }) {
  return <span className={`badge badge-${tone}`}>{label}</span>;
}
