const limits = Object.freeze({ display_name: 80, username: 128 });
const safe = (value, maximum) => typeof value === 'string' && value.length > 0 && [...value].length <= maximum
  && value === value.trim() && !/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/u.test(value);

export function validProfileLabels(value) {
  return Object.entries(limits).every(([field, maximum]) => !Object.hasOwn(value, field) || safe(value[field], maximum));
}

// Presentation only. These labels never select or authorize a creator account.
export function profileLabels(value) {
  return Object.fromEntries(Object.entries(limits).flatMap(([field, maximum]) => {
    const label = typeof value?.[field] === 'string' ? value[field].trim() : null;
    return safe(label, maximum) ? [[field, label]] : [];
  }));
}
