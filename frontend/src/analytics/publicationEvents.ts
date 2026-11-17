export type SubscribePublications = (changed: () => void) => () => void;

export const subscribeAnalyticsPublications: SubscribePublications = changed => {
  if (typeof EventSource === 'undefined') return () => {};
  const stream = new EventSource('/api/v1/insights/events');
  stream.addEventListener('analytics-changed', changed);
  return () => {
    stream.removeEventListener('analytics-changed', changed);
    stream.close();
  };
};
