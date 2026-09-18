import { mkdir } from 'node:fs/promises';
import { join, relative } from 'node:path';

export const VISION_TYPES = ['protanopia', 'deuteranopia', 'tritanopia', 'achromatopsia'];

/** Diagnostic captures, not accessibility scores. All chart labels and tables remain available. */
export async function captureVisionDiagnostics(page, outputDirectory, name) {
  const directory = join(outputDirectory, 'diagnostics', 'vision');
  await mkdir(directory, { recursive: true });
  const session = await page.context().newCDPSession(page);
  const entries = [];
  try {
    for (const type of VISION_TYPES) {
      await session.send('Emulation.setEmulatedVisionDeficiency', { type });
      const path = join(directory, `${name}-${type}.png`);
      await page.screenshot({ path, animations: 'disabled', caret: 'hide', fullPage: true });
      entries.push({ file: relative(outputDirectory, path).replaceAll('\\', '/'), type, diagnosticOnly: true });
    }
  } finally {
    try {
      await session.send('Emulation.setEmulatedVisionDeficiency', { type: 'none' });
    } finally {
      await session.detach();
    }
  }
  return entries;
}
