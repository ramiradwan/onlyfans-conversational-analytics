export async function confirmFullDocument(page) {
  await page.evaluate(() => {
    for (const action of ['resume', 'refresh_identity']) {
      window.postMessage({ type: 'ofca.capture.control', version: 1, action }, location.origin);
    }
  });
}
