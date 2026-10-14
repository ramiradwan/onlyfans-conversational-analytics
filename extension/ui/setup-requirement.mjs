/** Group status changes that describe the same visible setup requirement. */
export function setupRequirement({ model, view, selected, reviewFull, journey }) {
  if (view === 'desktop') return 'desktop-handoff';
  if (view === 'start') return 'start-choice';
  if (view === 'agree') return `agreement:${model.legal.requires_reauthorization ? 'reauthorization' : 'initial'}:${selected ?? 'software'}`;
  if (view === 'mode') return `mode-choice:${reviewFull ? 'full' : 'preview'}`;
  if (view === 'access') return 'site-access';
  const { status, pairing } = model;
  switch (journey.id) {
    case 'desktop_app_needed':
    case 'desktop_app_unavailable': return 'desktop-connection';
    case 'setup_incomplete':
      if (status.phase === 'permission_required') return 'site-access';
      if (pairing.state === 'setup_incomplete') return 'creator-account';
      return 'extension-connection';
    case 'pairing_required':
    case 'pairing_failed':
    case 'pairing_not_ready': return 'pairing';
    case 'pairing_in_progress': return pairing.state === 'compare' ? 'pairing-comparison' : 'pairing';
    case 'activation_checking':
    case 'activation_required':
    case 'activation_unavailable': return 'activation';
    case 'full_unavailable':
      if (status.delivery?.transport_state !== 'authenticated') return 'desktop-connection';
      if (status.delivery?.browser_tab_sleeping) return 'creator-tab';
      return 'analysis';
    case 'activation_active': return 'analysis';
    case 'full_ready': return 'analytics-ready';
    case 'preview_available': return 'preview';
    case 'paused': return 'paused';
    case 'analytics_off': return 'analytics-off';
    default: return `journey:${journey.id}`;
  }
}
