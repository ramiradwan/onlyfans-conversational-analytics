// Synthetic fixture: dummy transport implementation
export function createDurableOutbox(event) {
  return { enqueued: event };
}
