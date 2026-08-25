# Bridge interaction and layout

This page defines durable layout and interaction rules for Bridge. Exact breakpoints, dimensions, and timing values belong to the active implementation profile.

## Layout and responsive behavior

Bridge is desktop-oriented, but supported narrower layouts must preserve meaning and access to primary actions.

- Group related information through spacing, alignment, hierarchy, or a shared region.
- Dense views are acceptable when scan paths and control ownership remain clear.
- Responsive changes preserve reading order, meaning, and primary actions.
- Components reflow, resize, collapse, or hide secondary detail instead of compressing the desktop layout until it becomes unusable.
- Browser zoom and text enlargement must not remove supported functionality.
- Touch-capable layouts provide accessible target sizing or spacing while preserving keyboard and pointer use.

## Navigation

- The current location is visually and programmatically identifiable.
- Navigation placement remains consistent within the same viewport class.
- Navigation works with browser history, deep links, refresh, and state restoration.
- Collapsed navigation keeps clear focus behavior and a way to return to the expanded state.
- Frequently used destinations remain distinguishable from secondary navigation.

## Component states

Interactive components expose the states relevant to their behavior, including hover where applicable, focus, active, selected, disabled, read-only, loading, and error states.

- State differences do not rely on color alone.
- Disabled, unavailable, read-only, empty, partial, and error states remain semantically distinct.
- Focus remains visible and follows a logical interaction order.
- Pointer actions have a keyboard path unless the action is inherently pointer-specific and an equivalent control exists.
- Drag, resize, reorder, pan, and similar gestures have a non-dragging alternative when the gesture itself is not essential.
- Essential information and actions do not appear only on hover.
- Modal dialogs manage initial focus, keep focus inside while open, and restore focus when closed. Non-modal overlays do not trap focus.
- Async actions acknowledge input and communicate continued work without blocking unrelated tasks.
- Loading or replacement work does not remove valid existing content unless the product contract requires it.
- Destructive or difficult-to-reverse actions provide confirmation or a recovery path.
- Optimistic UI is corrected visibly if the underlying operation fails.

## Motion

- Motion communicates state, continuity, or orientation.
- Decorative motion does not delay interaction or hide results.
- Reduced-motion preferences receive a functional low-motion alternative.
- Flashing content stays within WCAG 2.2 limits.

## Localization and content resilience

- Layouts tolerate translated text, long names, large values, and missing optional content.
- Dates, times, numbers, and currencies use locale-aware formatting.
- Logical DOM and focus order follow the rendered task sequence.
- Directional icons mirror only when their meaning follows reading direction.
- Controls do not depend on fixed copy length for sizing or alignment.
