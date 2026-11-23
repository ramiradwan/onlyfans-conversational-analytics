export function pickerLabel(document, hasMicrosoftShortcut) {
  return document.documentElement.dataset.microsoftPicker === 'true' && !hasMicrosoftShortcut
    ? 'Microsoft or other sign-in options'
    : 'Other sign-in options';
}
