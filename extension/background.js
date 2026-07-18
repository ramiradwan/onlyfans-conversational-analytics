import { createAgentRuntime } from './transport/agent-runtime.mjs';

export const agentRuntime = createAgentRuntime({
  onStartupError: () => {
    // Keep diagnostics free of account data, credentials, and captured content.
    console.error('[Agent] startup failed; a later extension wake will retry');
  },
});

// Listener registration is synchronous. Durable initialization continues without
// top-level await so a failed storage/config step cannot leave the worker unwakeable.
agentRuntime.registerListeners();
void agentRuntime.wake().catch(() => undefined);
