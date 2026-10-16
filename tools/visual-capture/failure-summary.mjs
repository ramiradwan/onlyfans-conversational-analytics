// CI may expose fixed categories and counts, never raw browser error strings.
export function failureSummary(driverErrors, observerErrors) {
  const counts = { driver: driverErrors.length, overflow: 0, geometry: 0, shift: 0, observation: 0, other: 0 };
  for (const error of observerErrors) {
    if (typeof error !== 'string') counts.other++;
    else if (error.endsWith(': overflow')) counts.overflow++;
    else if (error.endsWith(': moved or resized') || error.endsWith(': removed') || error.endsWith(': missing from captured frame')) counts.geometry++;
    else if (/layout[- ]shift|layout shift after first paint/iu.test(error)) counts.shift++;
    else if (/native (?:frame|observation)|geometry frame interval|no status frame|no settings (?:frames|transitions)/iu.test(error)) counts.observation++;
    else counts.other++;
  }
  return counts;
}
