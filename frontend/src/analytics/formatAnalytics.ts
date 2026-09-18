export function formatCount(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
}
export function formatDecimal(value: number | null, fractionDigits = 1): string {
  if (value === null) return 'Unavailable';
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

export function formatRatioPercent(value: number | null, fractionDigits = 0): string {
  if (value === null) return 'Unavailable';
  return new Intl.NumberFormat(undefined, {
    style: 'percent',
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

export function formatPercentValue(value: number | null, fractionDigits = 1): string {
  if (value === null) return 'Unavailable';
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value) + '%';
}

export function formatSentimentScore(value: number | null): string {
  if (value === null) return 'Unavailable';
  const magnitude = new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Math.abs(value));
  if (value > 0) return '+' + magnitude;
  if (value < 0) return '−' + magnitude;
  return magnitude;
}

export function sentimentLabel(value: number | null): 'Positive' | 'Neutral' | 'Negative' | 'Unavailable' {
  if (value === null) return 'Unavailable';
  if (value > 0.05) return 'Positive';
  if (value < -0.05) return 'Negative';
  return 'Neutral';
}

export function formatDateLabel(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  }).format(new Date(value));
}

export function formatDurationFromSeconds(value: number | null): string {
  if (value === null) return 'Unavailable';
  if (value < 60) return formatDecimal(value, 0) + ' sec';
  return formatDecimal(value / 60, 1) + ' min';
}

/** Keeps locale-specific percent placement and spacing intact while styling the unit. */
export function formatRatioPercentParts(value: number, fractionDigits = 0, locales?: Intl.LocalesArgument): Intl.NumberFormatPart[] {
  return new Intl.NumberFormat(locales, {
    style: 'percent', minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits,
  }).formatToParts(value);
}

/** The product's existing minute label, separated without changing its wording. */
export function formatMinutesParts(value: number): Intl.NumberFormatPart[] {
  return [
    ...new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).formatToParts(value),
    { type: 'literal', value: ' ' }, { type: 'unit', value: 'min' },
  ];
}
