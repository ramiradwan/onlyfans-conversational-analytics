<!-- CODE-VERIFY: Check capture.mjs, appearance-contracts.mjs, vision-diagnostics.mjs and the visual-capture workflow before editing commands or coverage. -->

# Capture Bridge views

Run from the repository root:

```sh
node --test tools/visual-capture/*.test.mjs
VISUAL_CAPTURE_REVISION=$(git rev-parse HEAD) node tools/visual-capture/capture.mjs artifacts/visual-capture
npm run generate:theme --prefix frontend -- --color-report ../artifacts/visual-capture/color-qualification.json
```

The manifest records the source revision, ordinary fold/full captures, diagnostic captures and failures. Existing viewport, overflow, clipping and interaction assertions remain active. Numeric checks require the bundled font to load, keep headings in Inter, preserve the dashboard scale break and align desktop number baselines.

Only the populated Analytics screen receives color-vision simulations: protanopia, deuteranopia, tritanopia and achromatopsia, in both themes and at desktop and narrow widths. The Chromium emulation is reset after each set, including on failure. These images aid review; they are not accessibility scores.

The color report contains measured foreground/background pairs and separate categorical-color distances. Generation rejects failing required pairs and reserved financial-color reuse. Distances have no accessibility pass threshold. Review the labels, polarity symbols, keyboard tooltips and table alternative as well as the colors.

The workflow uploads screenshots, the manifest and the color report together under the exact Product commit. It does not merge or deploy the product.
