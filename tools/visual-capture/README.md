<!-- CODE-VERIFY: Check capture.mjs, appearance-contracts.mjs, vision-diagnostics.mjs and the visual-capture workflow before editing commands or coverage. -->

# Capture Bridge views

Visual CI qualification requires three distinct clean hosted serial/split runs of the same candidate, independently verified against their original evidence. A timing report or local capture does not establish qualification. Add the stable visual check to branch rules only after that review.

Install the pinned frontend, extension and visual-capture dependencies and build the extension renderer before capture. A capture builds its own frontend fixture. On a shared workstation, acquire the agreed heavy-workload guard before installations, builds, Chromium tests or capture, and run one heavy qualification task at a time. Use an isolated checkout and synthetic temporary state. Do not reuse live application data, personal browser profiles, another task's listeners or a development Brain on port 17871.

Run from the repository root:

```sh
node --test tools/visual-capture/*.test.mjs
VISUAL_CAPTURE_REVISION=$(git rev-parse HEAD) node tools/visual-capture/capture.mjs artifacts/visual-capture
npm run generate:theme --prefix frontend -- --color-report ../artifacts/visual-capture/color-qualification.json
```

## Run one CI stage group

CI runs `dynamic` and `remaining` on separate runners. Dynamic owns the existing dynamic-transition population. Remaining owns frontend fold/full captures and diagnostic images, freshness transitions, review checks and static surfaces. Both keep the existing four-worker bound. The `all` group is the complete serial control.

```sh
node tools/visual-capture/ci/inventory.mjs --output artifacts/visual-inventory.json
VISUAL_CAPTURE_REVISION=$(git rev-parse HEAD) node tools/visual-capture/capture.mjs artifacts/visual-dynamic --stage-group dynamic
VISUAL_CAPTURE_REVISION=$(git rev-parse HEAD) node tools/visual-capture/capture.mjs artifacts/visual-remaining --stage-group remaining
VISUAL_CAPTURE_REVISION=$(git rev-parse HEAD) node tools/visual-capture/capture.mjs artifacts/visual-control --stage-group all
```

The inventory command derives the complete expected population from checked source without starting Chromium or building the product. Run capture commands serially on a local workstation and use a fresh output directory for each run. Explicit stage groups reject `VISUAL_CAPTURE_ONLY` and `STATIC_SURFACE_ONLY`; a filtered developer run cannot be submitted as complete CI evidence. The registered configuration/stage ID in restricted diagnostics identifies the failure; rerun its owning group with the command above. No timing threshold or retry turns a failed assertion into an accepted result.

The required contracts job runs the existing extension surface browser tests and all visual Node contracts alongside the independent capture matrix. The aggregate requires its success before final publication; the optional serial control waits for contracts. It also explicitly runs the pinned Playwright reporting sentinels, JavaScript diagnostic redaction tests, Python session diagnostic tests outside backend discovery, and the visual inventory/output safety tests. See [Test changes](../../docs/testing.md) for the reporting-safety commands and dispatch comparison procedure.

The additional visual output checks use these exact commands:

```sh
node --test tools/visual-capture/ci/*.test.mjs
node tools/visual-capture/ci/sentinels.mjs
```

The sentinel uses the actual pinned Playwright package with synthetic metadata and progress probes. It does not use application profiles or live data. Run it under the shared-workstation guard with the other browser-related checks.

The manifest records the source revision, ordinary fold/full captures, diagnostic captures and failures. Existing viewport, overflow, clipping and interaction assertions remain active. Numeric checks require the bundled font to load, keep headings in Inter, preserve the dashboard scale break and align desktop number baselines.

Provisioning stays in the same tab. The former `recovery:open` and `recovery:close` dialog observations now map to `approval:retry:pending` and `approval:retry:refused`, using the visible approval retry action. The static `approval-unavailable-help` ID remains stable and captures that focused action with its linked feedback; it does not reveal the retired “find the setup tab” control. Held approval and finalization responses are observed before startup resolves, since startup resumes those operations automatically. Geometry, focus, refusal, and single-finalization assertions still apply.

Only the populated Analytics screen receives color-vision simulations: protanopia, deuteranopia, tritanopia and achromatopsia, in both themes and at desktop and narrow widths. The Chromium emulation is reset after each set, including on failure. These images aid review; they are not accessibility scores.

The color report contains measured foreground/background pairs and separate categorical-color distances. Generation rejects failing required pairs and reserved financial-color reuse. Distances have no accessibility pass threshold. Review the labels, polarity symbols, keyboard tooltips and table alternative as well as the colors.

The workflow verifies each producer's source revision, workflow run, newest job attempt, full inventory, completed outcomes and approved file hashes. Successful producers publish immutable `visual-inputs-<group>-<source>-<run>-<attempt>` artifacts. Failed producers publish only independently validated restricted metadata or fixed diagnostic codes; arbitrary error strings, raw output directories and extra screenshots are not accepted as diagnostic artifacts.

The stable aggregate reconstructs the complete screenshot/report layout, generates the existing color report and binds every final approved file before uploading `visual-states-<source>`. That source-bound artifact is replaced only on success. The separate immutable verification receipt records the accepted producer attempts and phase durations; a partial directory or a historical successful producer cannot override a newer failed attempt.

For a hosted comparison, enable both `visual_qualification` and `visual_serial_control` in the existing visual workflow dispatch. Requesting a serial control without qualification fails. The control runs at the same source/run as both capture groups, and must independently match their full population and accepted outcomes before publication. Ordinary runs skip only this extra control. Three distinct clean dispatches are required; local captures and reruns of the same workflow run do not substitute for those samples.

Review checks run in isolated pages after the ordinary matrix. `review/acceptance.json` stores measured rail sizes and insets, hover/selection contrast, focus styles and restoration, brand pixels, typography, track alignment and motion behavior. Hover and keyboard-focus screenshots are stored alongside the report. A failed review check fails the visual workflow.
