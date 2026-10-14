export type OnboardingOwner = 'brain' | 'extension';
export interface OnboardingState {
  profile: 'local-onboarding-state.v1';
  kind: 'snapshot' | 'event';
  journey_id: string;
  source: OnboardingOwner;
  epoch: string;
  revision: number;
  account_generation: number;
  consent_generation: number;
  facts: Record<string, string>;
  pending_operation: { operation_id: string; status: 'pending' | 'unknown' } | null;
  reason: string;
}
export interface OnboardingCommand {
  profile: 'local-onboarding-command.v1';
  journey_id: string;
  operation_id: string;
  owner: OnboardingOwner;
  action: 'pair' | 'cancel_pairing' | 'pause' | 'resume' | 'reopen_helper';
  account_generation: number;
  consent_generation: number;
}
export interface OnboardingView {
  sources: Partial<Record<OnboardingOwner, {
    epoch: string; revision: number; certain: boolean; snapshot: OnboardingState | null;
  }>>;
  operations: Record<string, OnboardingCommand & {
    command_epoch: string; status: 'pending' | 'unknown' | 'confirmed' | 'rejected'; result_revision?: number; result_epoch?: string;
  }>;
}
export interface OnboardingAdapter {
  subscribe(receive: (message: unknown) => void, disconnect: () => void): () => void;
  readSnapshot(): Promise<unknown>;
  sendCommand(command: OnboardingCommand, epoch: string): Promise<void> | void;
  readOperation?(operationId: string): Promise<void> | void;
  focusWorkspace?(): void;
  invalidate?(): void;
}
export interface OnboardingClient {
  getState(): OnboardingView;
  subscribe(listener: () => void): () => void;
  attach(source: OnboardingOwner, adapter: OnboardingAdapter): () => void;
  command(command: OnboardingCommand): Promise<boolean>;
  disconnect(source: OnboardingOwner): void;
}
export function createOnboardingClient(options: {
  journeyId: string; validate(value: unknown): boolean; bufferLimit?: number; operationLimit?: number;
}): OnboardingClient;
