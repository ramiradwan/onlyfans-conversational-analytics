import { MemoryRouter, SetupPrompt } from 'onlyfans-analytics-frontend';

import './card.module.css';

export function FirstRun() {
  return (
    <MemoryRouter>
      <SetupPrompt title="Finish setup" />
    </MemoryRouter>
  );
}

export function ExtensionConnected() {
  return (
    <MemoryRouter>
      <SetupPrompt extensionConnected title="Finish setup" />
    </MemoryRouter>
  );
}

export function FullAnalyticsRemaining() {
  return (
    <MemoryRouter>
      <SetupPrompt extensionConnected historyEnabled title="Finish setup" />
    </MemoryRouter>
  );
}
