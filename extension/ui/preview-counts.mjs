/** The owner stores calendar days in UTC; older responses have aggregate counts only. */
export function previewCounts(summary, now = new Date()) {
  if (!Array.isArray(summary?.days)) return { label: 'Last seven days', counts: summary ?? {} };
  const day = now.toISOString().slice(0, 10);
  const counts = summary.days.find((value) => value.day === day) ?? {};
  return { label: 'Today (UTC)', counts };
}
