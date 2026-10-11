/** One projection owns the rail's labels, completion and review destinations. */
export function setupProgress({ full, view, connected, journey }) {
  const complete = view === 'journey' && ['preview_available', 'full_ready'].includes(journey.id);
  const index = view === 'agree' ? 0 : ['mode', 'access'].includes(view) ? 1 : connected ? 3 : 2;
  const modeReview = view === 'mode';
  return {
    visible: full && !complete && view !== 'start',
    summary: complete ? (full ? 'Setup complete' : 'Preview is ready')
      : view === 'start' ? 'Choose Preview or Full analytics'
        : view === 'agree' ? 'Review terms and risk disclosure'
          : modeReview ? 'Choose a mode' : view === 'access' ? 'Allow site access' : journey.title,
    steps: [
      { id: 'agree', label: 'Agree', review: 'agree' },
      { id: 'mode', label: modeReview ? 'Choose a mode' : 'Site access', review: modeReview ? 'mode' : 'access' },
      { id: 'connect', label: 'Connect', review: null },
      { id: 'activate', label: 'Activate', review: null },
    ].map((step, position) => ({ ...step, hidden: position >= (full ? 4 : 2),
      complete: complete || position < index, current: !complete && position === index,
      locked: !complete && position > index })),
  };
}
