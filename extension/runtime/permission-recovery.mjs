const ONLYFANS_ORIGIN_PATTERN = 'https://onlyfans.com/*';

export function requiredOriginsForMode(mode) {
  if (mode === 'preview') return [ONLYFANS_ORIGIN_PATTERN];
  if (mode === 'full') return [ONLYFANS_ORIGIN_PATTERN];
  return [];
}
