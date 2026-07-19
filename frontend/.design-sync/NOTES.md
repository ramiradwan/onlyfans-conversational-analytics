# Design sync notes

- Source shape is `package`, but this repository is an application rather than a published component library. The converter intentionally uses the committed curated source entry `.design-sync/entry.ts`; `componentSrcMap` defines the public visual surface while the entry also exposes required preview providers and deterministic fixture hooks.
- `npm run build` outputs the application into `../app/static/dist`; it does not create a reusable package entry or declaration tree. The sync therefore relies on source-level contracts. Generated `.d.ts` files currently use permissive index signatures, so future syncs should add and maintain explicit `dtsPropsFor` contracts for components whose prop APIs must be strongly constrained.
- The current component surface has two source exports named `MessageBubble`. The sync exposes the newer inbox component (`src/components/inbox/MessageBubble.tsx`) and omits the older legacy bubble (`src/components/MessageBubble.tsx`) because a single bundle namespace cannot faithfully carry both under the same name.
- Theme styling is MUI v7 CSS-in-JS. `.design-sync/styles.css` carries the app's global body/color-scheme rules without its package-relative font import; `src/assets/fonts/fonts.css` and local WOFF2 files are explicitly shipped through `extraFonts`.
- The converter's simple path resolver does not probe directory indexes. `.design-sync/tsconfig.json` mirrors the app aliases and adds explicit `index.ts` / `index.tsx` targets for directory imports; keep it aligned with the app's alias map.
- Full rich-preview authoring was selected for the curated visual surface on 2026-07-19.

## Re-sync risks

- Component inclusion is explicit in `componentSrcMap`; newly added visual exports will not appear until that sparse map is extended deliberately.
- Source-level synthesized bundles have weaker automatic `.d.ts` extraction than a published package. Re-check generated contracts after component prop changes and keep `dtsPropsFor` overrides current.
- Application views and layout components may depend on Zustand stores, router context, or browser APIs. Their authored previews must own deterministic fixtures/context and must not make live backend or WebSocket calls.
- Preview data must remain static and representative; avoid embedding copies of evolving production fixtures when a small local view-model object is sufficient.
- The bundle assumes the repository's pinned npm lockfile and Node `>=18`; Playwright browser revision compatibility must be checked against the installed `.ds-sync` Playwright package before validation.
- No network-fetched fonts or styling assets are accepted; Inter is sourced from the repository's local WOFF2 files.
