import { getDefaultNormalizer } from '@testing-library/react';

/** Normalizes expected text as Testing Library normalizes rendered text, including locale spacing such as U+2009 and U+202F. */
export const renderedText = getDefaultNormalizer();

const calendarFormat = () =>
  new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC', year: 'numeric' });

/** Host-locale calendar date range for two `YYYY-MM-DD` dates. */
export function expectedCalendarRange(start: string, end: string): string {
  return calendarFormat().formatRange(new Date(`${start}T00:00:00Z`), new Date(`${end}T00:00:00Z`));
}

/** Host-locale calendar date for a `YYYY-MM-DD` date. */
export function expectedCalendarDate(value: string): string {
  return calendarFormat().format(new Date(`${value}T00:00:00Z`));
}
