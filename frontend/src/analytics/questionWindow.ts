import { activityWeekMs, type QuestionId, type QuestionPlan } from './questionContract';

export function localDate(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}
function midnight(value: string): Date {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error('Choose both dates.');
  const [year, month, day] = value.split('-').map(Number);
  const result = new Date(year, month - 1, day);
  if (localDate(result) !== value) throw new Error('Choose a valid calendar date.');
  return result;
}
export function initialQuestionDates(now = new Date()) {
  const first = new Date(now);
  first.setDate(first.getDate() - 6);
  return { start: localDate(first), end: localDate(now) };
}
export function localMinute(value: Date): string {
  return `${localDate(value)}T${String(value.getHours()).padStart(2, '0')}:${String(value.getMinutes()).padStart(2, '0')}`;
}

export function makeActivityPlan(through: string, now = new Date()): QuestionPlan {
  const end = new Date(through);
  if (!Number.isFinite(end.getTime()) || localMinute(end) !== through || end > now) {
    throw new Error('Choose a valid comparison time up to now.');
  }
  return { question: 'participant_activity_decline.v1', start: new Date(end.getTime() - 2 * activityWeekMs).toISOString(),
    end: end.toISOString(), cutoff: end.toISOString(), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, page_size: 50 };
}

export function makeQuestionPlan(question: Exclude<QuestionId, 'participant_activity_decline.v1'>, startDate: string, endDate: string, now = new Date()): QuestionPlan {
  const start = midnight(startDate);
  const last = midnight(endDate);
  if (last < start || last > now) throw new Error('Choose an end date on or after the start date, no later than today.');
  last.setDate(last.getDate() + 1);
  const end = new Date(Math.min(last.getTime(), now.getTime()));
  if (start >= end) throw new Error('Choose a date range with saved activity.');
  return { question, start: start.toISOString(), end: end.toISOString(), cutoff: now.toISOString(),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, page_size: 50 };
}
