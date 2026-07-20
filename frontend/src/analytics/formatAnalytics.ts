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
