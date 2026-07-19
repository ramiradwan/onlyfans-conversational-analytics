interface AgentPairingTicket {
  pairing_ticket: string;
  expires_at: string;
}

function csrfToken(): string | null {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null;
}

function isPairingTicket(value: unknown): value is AgentPairingTicket {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  return (
    Object.keys(candidate).length === 2 &&
    typeof candidate.pairing_ticket === 'string' &&
    candidate.pairing_ticket.length > 0 &&
    typeof candidate.expires_at === 'string' &&
    Number.isFinite(Date.parse(candidate.expires_at))
  );
}

/** Requests a short-lived, one-time Agent pairing credential for the authenticated account. */
export async function requestAgentPairingTicket(
  signal?: AbortSignal,
): Promise<AgentPairingTicket> {
  const csrf = csrfToken();
  if (!csrf) throw new Error('A CSRF token is required to pair the local Agent');
  const response = await fetch('/api/v1/agent/pairing', {
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'X-CSRF-Token': csrf,
    },
    method: 'POST',
    signal,
  });
  if (!response.ok) throw new Error(`Agent pairing request failed (${response.status})`);
  const value = (await response.json()) as unknown;
  if (!isPairingTicket(value)) throw new Error('Brain returned an invalid Agent pairing ticket');
  return value;
}
