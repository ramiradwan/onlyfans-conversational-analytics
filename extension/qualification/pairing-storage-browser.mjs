import { chromium } from "@playwright/test";
import { build } from "esbuild";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { waitForWorkerEntry } from "./snow-wasm-spike/process-lines.mjs";
// Loading this module verifies the vendored pairing contract against the
// consumer pin, so the bundle below cannot inline unpinned bytes.
import "../test-fixtures/pairing/vendored-vector.mjs";
const here = path.dirname(fileURLToPath(import.meta.url));
const temp = await mkdtemp(path.join(tmpdir(), "ofca-pairing-"));
const extension = path.join(temp, "extension"),
  profile = path.join(temp, "profile");
let context;
try {
  await build({
    stdin: {
      contents:
        "import {runStorageScenario,runRaceScenario} from './pairing-storage-scenario.mjs'; globalThis.runStorageScenario=runStorageScenario; globalThis.runRaceScenario=runRaceScenario; chrome.runtime.onMessage.addListener(()=>{});",
      resolveDir: here,
    },
    bundle: true,
    format: "esm",
    outfile: path.join(extension, "background.mjs"),
  });
  await writeFile(
    path.join(extension, "manifest.json"),
    JSON.stringify({
      manifest_version: 3,
      name: "Pairing storage qualification",
      version: "1.0.0",
      background: { service_worker: "background.mjs", type: "module" },
    }),
  );
  const browser = process.argv[2];
  async function launch() {
    context = await chromium.launchPersistentContext(profile, {
      ...(browser ? { executablePath: browser } : { channel: "chromium" }),
      headless: true,
      args: [
        `--disable-extensions-except=${extension}`,
        `--load-extension=${extension}`,
      ],
    });
    const worker = (
      context.serviceWorkers()[0] ??
      (await context.waitForEvent("serviceworker", { timeout: 15000 }))
    );
    await waitForWorkerEntry(worker, "runStorageScenario");
    return worker;
  }
  let worker = await launch();
  const seed = await worker.evaluate(() =>
    globalThis.runStorageScenario("seed"),
  );
  await context.close();
  context = null;
  worker = await launch();
  const restart = await worker.evaluate(
    (identity) => globalThis.runStorageScenario("restart", identity),
    seed.identity,
  );
  const races = await worker.evaluate(() => globalThis.runRaceScenario());
  console.log(
    JSON.stringify({
      browser: context.browser()?.version(),
      mv3: true,
      ...restart,
      ...races,
    }),
  );
} finally {
  await context?.close();
  // Both paths are created under our unique temporary directory above.
  if (!path.resolve(temp).startsWith(path.resolve(tmpdir()) + path.sep)) {
    throw new Error("temporary_directory_outside_workspace");
  }
  await rm(temp, { recursive: true, force: true });
}
