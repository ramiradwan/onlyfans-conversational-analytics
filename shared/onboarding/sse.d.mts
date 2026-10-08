export function readOnboardingEvents(options: {
  fetch: typeof fetch; path: '/api/v1/provisioning/events' | '/api/v1/onboarding/events';
  headers?: Record<string, string>; signal?: AbortSignal; ready(): void; receive(message: unknown): void;
  onFocus?(journeyId: string): void;
  idleTimeoutMs?: number;
}): Promise<void>;
