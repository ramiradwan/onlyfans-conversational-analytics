// Presentation state that lets the toolbar popup resume where the customer left it.
// Pairing, consent and connection state always come from the background runtime instead.
export const POPUP_CONTEXT_KEY = 'popup_context_v1';
export const POPUP_CONTEXT_MAX_AGE_MS = 15 * 60 * 1000;

const VIEWS = Object.freeze(['home', 'connection', 'manage']);
const KEYS = 'full_review_requested,initial_choice_dismissed,saved_at,view';
const EMPTY = Object.freeze({ view: 'home', full_review_requested: false, initial_choice_dismissed: false });

// The view and review position expire; the dismissal lasts for the browser session.
export function parsePopupContext(value, now = Date.now()) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return EMPTY;
  if (Object.keys(value).sort().join() !== KEYS) return EMPTY;
  if (!VIEWS.includes(value.view)
    || typeof value.full_review_requested !== 'boolean'
    || typeof value.initial_choice_dismissed !== 'boolean'
    || !Number.isSafeInteger(value.saved_at)) return EMPTY;
  const fresh = value.saved_at <= now && now - value.saved_at <= POPUP_CONTEXT_MAX_AGE_MS;
  return Object.freeze({
    view: fresh ? value.view : 'home',
    full_review_requested: fresh && value.full_review_requested,
    initial_choice_dismissed: value.initial_choice_dismissed,
  });
}

export async function loadPopupContext(area, now = Date.now()) {
  try {
    const stored = await area.get([POPUP_CONTEXT_KEY]);
    return parsePopupContext(stored?.[POPUP_CONTEXT_KEY] ?? null, now);
  } catch {
    return EMPTY;
  }
}

export async function savePopupContext(area, context, now = Date.now()) {
  const value = {
    view: VIEWS.includes(context.view) ? context.view : 'home',
    full_review_requested: context.full_review_requested === true,
    initial_choice_dismissed: context.initial_choice_dismissed === true,
    saved_at: now,
  };
  try { await area.set({ [POPUP_CONTEXT_KEY]: value }); } catch {}
}
