# Bridge data and accessibility

This page defines how Bridge presents analytical data, history, system readiness, and accessibility-sensitive state.

## Data visualization

- Color meanings stay stable within a view.
- Information carried by color also has a label, position, shape, pattern, annotation, or text equivalent.
- Keep category counts within what the chosen visual encodings can distinguish reliably.
- Charts expose an accessible name and a text or table representation appropriate to the same task.
- Keyboard and assistive-technology users can reach the same information available through pointer interaction.
- Analytical comparison does not depend on animation.
- Partial, uncertain, and unavailable values remain visibly and programmatically distinct.

## Accessibility baseline

Bridge targets WCAG 2.2 Level AA.

The rendered interface provides:

- semantic structure and accessible names;
- complete keyboard operation and logical focus management;
- visible focus indicators;
- screen-reader-compatible status and error updates;
- required text and non-text contrast;
- non-color equivalents for meaningful state;
- usable zoom, text enlargement, and reflow;
- supported high-contrast and forced-color behavior;
- reduced-motion behavior;
- accessible alternatives for complex visual content.

Automated scans provide evidence but do not establish accessibility conformance by themselves.

## Bounded conversation history

- WebSocket state contains conversation summaries and at most one latest-message preview, not complete message history.
- Historical messages are loaded through authenticated, bounded REST pages.
- Page caches and rendered rows stay bounded.
- Loading an older page preserves the reader's visible position and keyboard focus.
- A live append follows the bottom only when the reader is already near it.
- Projection-generation changes invalidate stale pages and cursors.
- Loading, empty, unavailable, and locally exhausted states remain distinct.

## Readiness and truthfulness

Acquisition coverage, projection readiness, and live freshness are separate states and remain separately available to the user.

- **Up to date** is used only when applicable acquisition is complete, the projection is current, live freshness is current, and desired and effective history settings agree.
- Combined status indicators use text and an icon or other non-color signal instead of color alone.
- Partial analytics identify their basis, observed and complete ranges where applicable, sample size, as-of time, and projection revision.
- A partial zero is not presented as an unqualified lifetime zero.
- An unavailable projection does not fall back to sample or static values.
- History controls distinguish desired state from effective running, paused, or revoked state.
- Authorized roles may inspect status; mutation controls are limited to creators where required by the product contract.
- Progress announcements describe meaningful phase changes rather than low-level page activity.
