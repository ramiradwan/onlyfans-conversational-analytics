import type { CatchupFreshness } from '../protocol';
import type { BridgeConnectionState } from '../store/transportStore';

export interface FreshnessPresentation {
  label: string;
  title: string;
  sentence: string;
  icon: 'dot' | 'ring' | 'ring-active' | 'pause';
  tone: 'settled' | 'checking' | 'unknown' | 'attention' | 'user' | 'error';
  action?: { label: string; destination: 'settings' | 'creator' | 'setup' | 'reload' };
}
export interface FreshnessInput {
  freshness: CatchupFreshness | null | undefined;
  bridge: BridgeConnectionState;
  snapshotUsable: boolean;
  now: Date;
  timeZone: string;
}
export const viewerTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

export function formatSlotDateTime(iso: string, now: Date, timeZone: string): string | null {
  const date = new Date(iso);
  if (!Number.isFinite(date.getTime())) return null;
  const formatter = new Intl.DateTimeFormat('fi-FI', { timeZone, day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const parts = (value: Date) => Object.fromEntries(formatter.formatToParts(value).map(({ type, value: part }) => [type, part]));
  const p = parts(date);
  return `${p.day}.${p.month}${p.year === parts(now).year ? '' : `.${p.year}`} ${p.hour}.${p.minute}`;
}

const settings = { label: 'Open settings', destination: 'settings' as const };
const creator = { label: 'Open OnlyFans', destination: 'creator' as const };
const extension = { label: 'Open extension', destination: 'setup' as const };
const paused: Record<string, FreshnessPresentation> = {
  user_paused: { label: 'Paused by you', title: 'Paused by you', sentence: 'New messages are not collected until you resume.', icon: 'pause', tone: 'user', action: settings },
  consent_needed: { label: 'Paused · consent needed', title: 'Consent needed', sentence: 'Review the updated terms in the extension to resume.', icon: 'dot', tone: 'attention', action: extension },
  extension_offline: { label: 'Paused · browser offline', title: 'Browser offline', sentence: 'Keep your browser open while the extension reconnects.', icon: 'dot', tone: 'attention' },
  no_onlyfans_tab: { label: 'Paused · open OnlyFans', title: 'No OnlyFans tab', sentence: 'Open your creator account in the browser to resume.', icon: 'dot', tone: 'attention', action: creator },
  onlyfans_sleeping: { label: 'Paused · OnlyFans sleeping', title: 'OnlyFans tab is sleeping', sentence: 'Your browser paused the tab. Open it to resume.', icon: 'dot', tone: 'attention', action: creator },
  account_changed: { label: 'Paused · account changed', title: 'Account changed', sentence: 'The OnlyFans tab is signed in to a different account.', icon: 'dot', tone: 'attention', action: { ...settings, label: 'Review in Settings' } },
  applying_settings: { label: 'Paused · applying settings', title: 'Applying settings', sentence: 'The extension is applying your latest settings.', icon: 'dot', tone: 'attention' },
  capture_off: { label: 'Paused · history off', title: 'Collection is off', sentence: 'New messages are off in the browser extension.', icon: 'pause', tone: 'user', action: settings },
  extension_outdated: { label: 'Paused · extension outdated', title: 'Extension outdated', sentence: 'Update the browser extension to resume.', icon: 'dot', tone: 'attention', action: extension },
};
const unknown: FreshnessPresentation = { label: 'Messages not checked', title: 'Messages not checked', sentence: 'The first check starts when the extension is connected.', icon: 'ring', tone: 'unknown' };
const behind: Record<string, string> = {
  awaiting_check: 'A check is scheduled and starts automatically.',
  daily_cap: 'Today’s checks are used up. The next check runs tomorrow.',
  check_incomplete: 'The last check didn’t finish. It retries automatically.',
  not_observing: 'Reload your OnlyFans tab so new messages can arrive.',
};

export function presentFreshness({ freshness, bridge, snapshotUsable, now, timeZone }: FreshnessInput): FreshnessPresentation {
  if (['disconnected', 'reconnecting', 'error'].includes(bridge)) return {
    label: 'Paused · Brain offline', title: 'Desktop app offline', sentence: 'The dashboard lost its connection to the desktop app on this computer.',
    icon: 'dot', tone: 'attention', action: { label: 'Reload page', destination: 'reload' },
  };
  if (bridge !== 'connected' || !snapshotUsable || !freshness) return unknown;
  if (freshness.status === 'paused') return paused[freshness.reason ?? ''] ?? {
    label: 'Paused · needs attention', title: 'Needs attention', sentence: 'New messages are paused. Open Settings to see what to do.', icon: 'dot', tone: 'attention', action: settings,
  };
  if (freshness.status === 'checking') {
    const label = freshness.reason === 'canary' && freshness.last_closed_at !== null ? 'Up to date' : 'Checking messages';
    return { label, title: label, icon: 'ring-active', tone: label === 'Up to date' ? 'settled' : 'checking',
      sentence: freshness.reason === 'canary' ? 'Running the routine hourly check.' : freshness.reason === 'catch_up' ? 'Recovering messages that arrived while collection was paused.' : 'Checking for missed messages.' };
  }
  if (freshness.status === 'behind') {
    const date = freshness.uncertain_since && formatSlotDateTime(freshness.uncertain_since, now, timeZone);
    if (!date) return unknown;
    return { label: `Behind since ${date}`, title: 'Messages may be missing', sentence: behind[freshness.reason ?? ''] ?? behind.awaiting_check,
      icon: 'dot', tone: 'attention', ...(freshness.reason === 'not_observing' ? { action: creator } : {}) };
  }
  if (freshness.status === 'current') return { label: 'Up to date', title: 'Up to date', sentence: 'Messages are current while the browser is observing them.', icon: 'dot', tone: 'settled' };
  return unknown;
}

export function freshnessRows(freshness: CatchupFreshness | null | undefined, now: Date, timeZone: string) {
  if (!freshness) return [];
  const facts = [
    ['Messages may be missing since', freshness.uncertain_since],
    ['Last checked', freshness.last_closed_at], ['Watching since', freshness.observing_since],
  ];
  return facts.flatMap(([label, date]) => {
    const value = date && formatSlotDateTime(date, now, timeZone);
    return value ? [{ label: label!, value }] : [];
  });
}
