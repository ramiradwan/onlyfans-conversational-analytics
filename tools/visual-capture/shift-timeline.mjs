/** LayoutShift.startTime is native even while Playwright mocks performance.now.
 * Delivery frames remain evidence, never authority for transition ownership. */
export function inspectShiftTimeline(frames, windows, rawShifts, clock) {
  const failures = new Set(), attribution = [];
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  if (clock?.basis !== 'pre-clock-performance/v1' || clock.clockVerified !== true || clock.completed !== true
    || !finite(clock.timeOrigin) || !finite(clock.capturedAt) || !finite(clock.endAt) || clock.endAt <= clock.capturedAt) {
    failures.add('Native layout-shift timeline is missing, unverified or incomplete');
  }
  if (!Array.isArray(rawShifts)) {
    failures.add('Append-only layout-shift ledger is missing');
    rawShifts = [];
  }
  let lastFrameAt = clock?.capturedAt;
  const delivered = [];
  for (const frame of frames) {
    if (!finite(frame.nativeAt) || frame.nativeAt < lastFrameAt || frame.nativeAt > clock?.endAt) failures.add('Invalid native frame timestamp');
    lastFrameAt = frame.nativeAt;
    delivered.push(...(frame.shifts ?? []));
  }
  if (JSON.stringify(delivered) !== JSON.stringify(rawShifts)) failures.add('Layout-shift ledger and raw frame entries differ');
  const ordered = [...windows].sort((a, b) => a.before?.nativeAt - b.before?.nativeAt);
  let priorEnd = clock?.capturedAt;
  for (const window of ordered) {
    const start = window.before?.nativeAt, end = window.after?.nativeAt;
    if (!finite(start) || !finite(end) || start < clock?.capturedAt || end > clock?.endAt || end <= start
      || !['content-transition', 'stable-status'].includes(window.geometry)) failures.add(`${window.id}: invalid native observation window`);
    if (start < priorEnd) failures.add(`${window.id}: overlapping native observation windows`);
    priorEnd = end;
  }
  rawShifts.forEach((entry, index) => {
    if (entry.sequence !== index + 1 || !finite(entry.at) || !finite(entry.deliveredAt) || !finite(entry.value) || entry.value < 0
      || entry.at < clock?.capturedAt || entry.deliveredAt < entry.at || entry.deliveredAt > clock?.endAt) {
      failures.add(`Layout shift ${index + 1}: invalid native ledger entry`);
    }
    // Strict boundaries make reduced timer precision ambiguous, never an excuse.
    const owners = ordered.filter(window => entry.at > window.before?.nativeAt && entry.at < window.after?.nativeAt);
    const owner = owners.length === 1 ? owners[0] : null;
    const boundary = ordered.some(window => entry.at === window.before?.nativeAt || entry.at === window.after?.nativeAt);
    const classification = entry.value === 0 ? 'zero-valued' : boundary || !owner ? 'unattributed' : owner.geometry;
    attribution.push({ sequence: entry.sequence, at: entry.at, deliveredAt: entry.deliveredAt, classification,
      window: owner && !boundary ? { kind: owner.kind, id: owner.id, index: owner.index } : null });
    if (entry.value > 0 && classification === 'unattributed') failures.add(`Layout shift ${entry.sequence}: outside a unique native transition window (gap, tail or boundary)`);
    if (entry.value > 0 && classification === 'stable-status') failures.add(`${owner.id}: layout shift after first paint`);
  });
  return { failures: [...failures], attribution };
}
