# Bridge Frontend Design Specification

**Status:** Durable design-system and interface contract

**Scope:** Responsive browser frontend

## 1. Purpose and scope

This document defines durable boundaries for the frontend visual system, layout, component behavior, interaction, accessibility, and verification. It is not an inventory of the current interface, a token catalogue, or a framework migration guide.

Feature scope, routes, roles, permissions, metrics, data semantics, consent policy, and protocol behavior are defined elsewhere. The frontend represents those contracts without redefining them. The [accepted architecture decisions](../docs/adr/README.md) and [communication specification](../communication-spec.md) remain authoritative for application behavior.

Authority is applied in this order:

1. applicable law, security policy, and WCAG 2.2;
2. accepted product and architecture contracts;
3. this design specification;
4. versioned design-system and framework implementation profiles.

## 2. Design direction

Bridge combines the responsiveness and finish expected from high-quality consumer applications with the stability and clarity of professional analytical software.

Familiar patterns are reused when they improve recognition without weakening control, comparison, or transparency. “Calm” describes controlled salience, coherent organization, predictable behavior, and clear system state; it does not prescribe sparse screens, pastel color, or low information density.

Demographic and persona labels do not prescribe palette, typography, spatial composition, or motion. Perceived quality supports usability but never substitutes for correctness or accessibility.

## 3. Token and component architecture

The design system distinguishes reference values, semantic roles, and component-specific decisions. These layers need not map to exactly three files or framework objects.

- Components consume semantic or component roles rather than reference values.
- Semantic names describe purpose or state rather than temporary appearance.
- Semantic roles remain stable across themes even when their values change.
- Component-specific tokens cover legitimate needs that shared semantic roles cannot express.
- Raw visual values stay in approved token, theme, asset, or visualization definitions rather than ordinary component code.
- Generated design-system files are build outputs, not manual editing surfaces.
- Repeated interaction and state behavior belongs in shared components or primitives.

The implementation profile defines the active token schema, public component APIs, framework mappings, and approved exceptions.

## 4. Color and themes

- Light and dark themes receive equivalent component and state coverage.
- Color roles keep the same meaning across themes and related workflows.
- Interactive color is not reused decoratively where it creates a false affordance.
- State, category, and urgency remain understandable without color alone.
- Text and required non-text elements meet WCAG 2.2 contrast requirements in their rendered context.
- Contrast on translucent, layered, hover, focus, selected, disabled, and data-visualization states is evaluated on the final composite.

Generated, adaptive, or user-derived palettes remain within the semantic role model. They are accepted only after contrast and state-distinguishability checks, with a validated fallback available. Exact palettes and theme-generation methods remain open design decisions.

## 5. Typography, iconography, and material

### Typography

- Type styles use named semantic roles and a limited, coherent hierarchy.
- Primary reading text remains legible at normal desktop viewing distance, browser zoom, and increased text size.
- Relative sizing respects browser and operating-system scaling.
- Metadata can be compact but does not become the only location for essential information.
- Truncation never becomes the only way to access meaning.

Font family, scale, weight, and measure belong to the implementation profile.

### Iconography

- Icons use a consistent visual family and retain recognizable silhouettes at their rendered size.
- Every icon-only control has an accessible name. Unfamiliar or ambiguous actions also include visible explanatory text.
- Directional icons mirror only when their meaning follows reading progression.
- Essential text is not embedded in icons or images.

### Surfaces and effects

- Surface changes clarify hierarchy, grouping, or interaction.
- Borders, elevation, translucency, blur, gradients, and rim effects preserve edge definition, contrast, and reading clarity.
- Content remains understandable when transparency, blur, animation, or advanced rendering effects are unavailable.

No material treatment, including glassmorphism, is a defining product characteristic.

## 6. Layout and responsive behavior

The primary workspace is desktop-oriented. Narrower and touch-capable configurations are defined by the implementation profile.

- Layout follows task priority, comparison needs, reading order, and available space.
- Related elements are grouped through proximity, alignment, hierarchy, or a common region; containers are added only when they improve structure.
- Dense views remain appropriate when scan paths, grouping, and control ownership are clear.
- Responsive changes preserve information meaning, reading order, and access to primary actions.
- Components reflow, resize, collapse, or progressively disclose secondary content instead of compressing the desktop composition.
- Browser zoom and text enlargement preserve supported workflows without clipping or loss of functionality.
- Touch-capable layouts provide WCAG-conforming targets or spacing while retaining keyboard and pointer operation.

The implementation profile versions the supported CSS viewport ranges, orientations, zoom and text-scale conditions, and input modes. Supported workflows remain operable throughout that matrix. Breakpoints, grid systems, shell dimensions, card layouts, and use of container queries also belong to that profile.

## 7. Navigation and wayfinding

- Current location is visually and programmatically identifiable.
- Navigation patterns and placement remain consistent within the same viewport class.
- Navigation integrates with browser history, deep links, refresh, and state restoration according to the routing contract.
- Collapsed navigation retains clear access, focus behavior, and a recovery path.
- High-frequency destinations remain distinguishable from secondary navigation.

This specification does not prescribe route names, navigation count, shell composition, or role-specific destinations.

## 8. Component states and interaction

Shared interactive components expose the states relevant to their behavior, including rest, hover where applicable, focus, active, selected, disabled, read-only, loading, and error.

- State differences remain perceivable without relying on color alone.
- Focus stays visible and follows meaningful reading and interaction order.
- Pointer interactions have a keyboard path unless an equivalent control covers an inherently pointer-specific operation.
- Drag, resize, reorder, pan, multipoint, and path-based interactions provide a non-dragging single-pointer alternative unless the gesture itself is essential.
- Essential information and actions are not disclosed only on hover.
- Modal contexts set predictable initial focus, contain focus while open, provide keyboard-accessible completion or dismissal, and restore focus on close. Non-modal overlays do not trap focus.
- Async actions acknowledge input within the feedback threshold defined by the implementation profile and communicate continued work without blocking unrelated tasks.
- Valid existing content remains until its replacement is ready unless a product contract explicitly invalidates it.
- Disabled, unavailable, read-only, empty, partial, and error states remain semantically distinct.
- Destructive or difficult-to-reverse actions provide confirmation or a reliable recovery path.
- Optimistic feedback is corrected visibly when the underlying operation fails.

### Motion

- Motion communicates state, continuity, causality, or orientation.
- Decorative motion remains restrained and never delays interaction or obscures results.
- Structural transitions preserve object correspondence when it aids understanding.
- Reduced-motion preferences receive a functional low-motion alternative.
- Flashing content stays within WCAG 2.2 limits.
- Equivalent effects favor rendering paths that avoid layout instability and input delay.

Exact durations and easing curves belong to the implementation profile.

## 9. Data visualization

- Color assignments remain semantically stable or categorically consistent within a view.
- Whenever color carries meaning, the same meaning is available through labels, position, shape, pattern, direct annotation, or an equivalent text or table.
- Category count stays within the discriminability of the selected encodings.
- Every non-decorative visualization exposes an accessible name and a textual or tabular equivalent appropriate to the same task.
- Keyboard and assistive-technology users can reach the same information available through pointer interaction.
- Analytical comparison never depends on animation.
- Uncertainty, partial values, and unavailable values remain perceptibly and programmatically distinct without implying false precision.

The product contracts determine what data means; this specification governs how those meanings remain perceptible.

## 10. Accessibility and input

WCAG 2.2 Level AA is the release baseline. The rendered frontend provides:

- semantic structure and accessible names;
- complete keyboard operation and logical focus management;
- visible focus indicators;
- screen-reader-compatible status and error updates;
- required text and non-text contrast;
- non-color equivalents for meaningful state;
- usable zoom, text enlargement, and reflow;
- compliant target sizing or spacing;
- operability in supported platform high-contrast and forced-color modes;
- reduced-motion behavior; and
- accessible alternatives for complex visual content.

Accessibility is evaluated across component states, overlays, themes, responsive layouts, and supported input methods. Theme-generation settings and automated scans contribute evidence but do not establish conformance by themselves.

APCA can be recorded as a secondary diagnostic. It does not replace WCAG 2.2 conformance.

## 11. Localization and content resilience

- Layouts tolerate translated text, long names, large values, and missing optional content.
- Date, time, number, and currency presentation uses locale-aware formatting.
- Layout and directional icons mirror only when their meaning follows reading progression. Absolute-direction, media, time, and brand symbols retain their intended orientation.
- Logical DOM and focus order follow the rendered task sequence rather than a mechanical reversal.
- User-visible text remains external to images and decorative assets.
- Interfaces do not rely on fixed copy length for alignment or control sizing.

Supported locales and formal expansion budgets belong to product requirements and the implementation profile.

## 12. Performance and rendering

- Visual effects and motion stay within the runtime and bundle budgets defined by the implementation profile.
- Loading patterns preserve layout stability and reflect the shape of expected content where useful.
- Progressive and background work does not blank unrelated valid content.
- Design-system defaults avoid unnecessary rendering work; exceptional effects remain measurable and removable.
- The interface retains a clear hierarchy when advanced effects or animation are reduced.

### Bounded conversation history

- Realtime state contains conversation summaries and at most one latest-message preview; complete historical arrays are never treated as WebSocket view state.
- Historical messages are loaded through authenticated, bounded pages. The active conversation retains at most the configured page window, inactive conversations use bounded LRU retention, and rendered rows are virtualized.
- Prepending an older page preserves the first visible message and its pixel offset without moving keyboard or screen-reader focus.
- A live append follows the tail only when the reader is already near the bottom. Reading older history is never interrupted by forced scrolling.
- A projection-generation change invalidates stale pages and cursors. Recovery requests are bounded and keep loading, empty, unavailable, and locally exhausted states distinct.

### Readiness, consent, and truthfulness

- Acquisition coverage, projection readiness, and live freshness remain independently perceivable and programmatically available. A combined status follows the product-defined priority order and uses text plus an icon rather than color alone.
- “Up to date” is reserved for complete applicable acquisition, a current projection, current live freshness, and aligned desired/effective history configuration.
- Partial analytics identify their synchronized-subset basis, observed range, sample size, and as-of time. A partial zero never reads as an unqualified lifetime zero; unavailable projections never render sample/static fallback values.
- History controls distinguish desired from effective running/paused/revoked state. All authorized roles can inspect status, while mutation controls are shown only to creators and require explicit consent.
- Progress announcements describe meaningful phase changes, not page-level acquisition noise.

The implementation profile versions numeric budgets with the representative device and browser, viewport, dataset, network conditions, measurement method, and percentile. Supported browser targets remain part of the release criteria.

## 13. Governance and creative latitude

This specification remains independent of a particular MUI major version. A separate implementation profile maps these boundaries to the active MUI and MUI X releases, token files, component APIs, and migration constraints.

Framework migration does not alter accessibility, state semantics, or public design-system contracts without a separately reviewed change.

Designers retain latitude over palette values, typography, spacing, radii, density, composition, iconography, elevation, motion character, and responsive structure. Exceptions to shared patterns identify the concrete interface need they serve.

## 14. Verification and review

Mechanical constraints belong in tooling where they can be checked reliably. The implementation profile names the exact tools, rule sets, fixtures, environments, and blocking thresholds.

| Concern | Checked through |
|---|---|
| Semantic markup, accessible names, and common keyboard or pointer issues | Static analysis, component and browser tests, and manual keyboard runs |
| Raw visual values and token bypass | Static-analysis rules or restricted-syntax policies outside approved source files |
| Token references and component state APIs | Compile-time contracts and component tests |
| Theme and semantic contrast | Token-generation checks and rendered contrast tests |
| Light/dark, responsive, focus, and state coverage | Enumerated fixture coverage and deterministic visual regression |
| Reduced motion and flashing | Stylesheet checks, component tests, and manual verification |
| Charts and equivalent representations | Component fixtures plus keyboard and screen-reader review |
| Localization and bidirectional layout | Pseudolocale, RTL, and visual-regression fixtures |
| Accessibility environments | Versioned browser, assistive-technology, zoom, reflow, and forced-color test matrix |
| Performance | Repeatable build and runtime measurements against the versioned profile |

Hierarchy, density, familiarity, composite legibility, and the suitability of a visual treatment remain design-review judgments. Static analysis does not attempt to infer demographic preference, satisfaction, or adoption.

## 15. Evidence and revision boundary

Supporting evidence must be reviewed from a repository-local, versioned source before it becomes normative. Until then, this specification distinguishes standards and accepted product contracts from explicitly labeled design hypotheses.

This document changes when normative requirements or durable frontend design boundaries change. Token values, component inventories, framework releases, route changes, and feature behavior belong to versioned implementation or product records.
