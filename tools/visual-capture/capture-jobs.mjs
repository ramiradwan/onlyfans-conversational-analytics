export async function runCaptureJobs(cases, capture, workers = 4) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(workers, cases.length) }, async () => {
    while (next < cases.length) await capture(cases[next++]);
  }));
}
