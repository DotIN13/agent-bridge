/** Position in time, which is what a reader of a long-running job wants. */
export function timeAgo(at: string | null | undefined): string {
  if (!at) return "";
  const then = Date.parse(at);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

/**
 * Where in the run, not what the clock said.
 *
 * The gateway computes `elapsed_hms` — seconds since the job's *first* event —
 * and that is the number worth reading at a glance on a run that has been going
 * for four hours. Its own local time, with the offset attached, stays on the
 * tooltip. An event with no elapsed at all is one that arrived before the first
 * one did, which only happens on a job whose log has been trimmed.
 */
export function elapsed(event: { elapsedHms: string | null; elapsed: number | null; at: string | null }): string {
  if (event.elapsedHms) return event.elapsedHms;
  if (event.elapsed === null) return timeAgo(event.at);
  const total = Math.floor(event.elapsed);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `+${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
}
