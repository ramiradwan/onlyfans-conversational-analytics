# Bridge design system

This page defines durable rules for Bridge visual tokens, themes, typography, icons, and surfaces. Exact values and framework mappings belong to the active implementation profile.

## Tokens

- Components use semantic or component-specific roles instead of raw visual values.
- Semantic names describe purpose or state, not a temporary appearance.
- A semantic role keeps the same meaning across themes even when its value changes.
- Component-specific tokens are used only when shared semantic roles do not fit the need.
- Raw colors, spacing values, and effects stay in approved token, theme, asset, or visualization definitions.
- Generated design-system files are build outputs and are not edited by hand.
- Repeated interaction or state behavior belongs in shared components or primitives.

## Color and themes

- Light and dark themes cover the same components and states.
- A color role keeps the same meaning across themes and related workflows.
- Interactive colors are not reused decoratively when that would imply an action.
- State, category, and urgency must remain understandable without color alone.
- Required text and non-text elements meet WCAG 2.2 contrast requirements in the rendered interface.
- Contrast checks include hover, focus, selected, disabled, translucent, layered, and visualization states.

Generated or user-derived palettes must use the same semantic roles and have a validated fallback.

## Typography

- Type styles use a small set of named semantic roles.
- Normal reading text remains legible with browser zoom and increased text size.
- Relative sizing respects browser and operating-system scaling.
- Essential information does not appear only as small metadata.
- Truncated text has another way to expose the complete meaning.

Font family, scale, weight, and line length belong to the implementation profile.

## Icons and surfaces

- Icons use a consistent visual family and remain recognizable at their rendered size.
- Every icon-only control has an accessible name.
- Unfamiliar actions include visible text when an icon alone is ambiguous.
- Essential text is not embedded in an image or icon.
- Borders, elevation, transparency, blur, gradients, and similar effects must improve hierarchy or interaction rather than decoration alone.
- Content remains understandable when advanced visual effects are unavailable.
