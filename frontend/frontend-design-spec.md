# Bridge frontend design

These documents define durable frontend design boundaries for Bridge. They do not define routes, permissions, data meaning, or protocol behavior.

Accepted architecture decisions and the [communication overview](../communication-spec.md) govern application behavior. WCAG 2.2 Level AA is the accessibility baseline.

## Design priorities

Bridge is a desktop-oriented analytical interface. Prefer clear structure, predictable state, readable data, and accessible interaction over visual novelty.

Use familiar interaction patterns when they help readers understand what will happen. Visual styling must not hide state, reduce legibility, or substitute for correct behavior.

Do not derive palette, typography, layout, or motion from demographic or persona labels.

## Design documents

- [Design system](design-system.md) — tokens, themes, typography, icons, and surfaces.
- [Interaction and layout](interaction-and-layout.md) — responsive layout, navigation, component states, motion, and localization.
- [Data and accessibility](data-and-accessibility.md) — charts, accessibility, bounded history, and truthful readiness state.
- [Quality and governance](quality-and-governance.md) — performance, verification, framework independence, and revision rules.

## Authority

Apply requirements in this order:

1. applicable law and security requirements;
2. accepted product and architecture contracts;
3. these frontend design documents;
4. versioned implementation profiles and framework details.

If a lower-level document conflicts with a higher-level source, correct the lower-level document.
