import { DashboardOverview } from 'onlyfans-analytics-frontend';

import './card.module.css';

export function UpToDate() {
  return (
    <DashboardOverview
      conversations="128"
      messages="2,436"
      received="1,402"
      sent="1,034"
      split={{ received: 1402, sent: 1034 }}
    />
  );
}

export function SyncingHistory() {
  return (
    <DashboardOverview
      conversations="46"
      messages="812"
      progress={{ label: 'History 36% synced', percent: 36 }}
      received="478"
      sent="334"
    />
  );
}

export function Loading() {
  return (
    <DashboardOverview conversations="—" isLoading messages="—" received="—" sent="—" />
  );
}
