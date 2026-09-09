const SET_FIELDS = new Set([
  'default_scopes',
  'default_intents',
  'supported_install_types',
  'user_install_scopes',
  'user_install_contexts',
  'e2ee_modes',
  'directory_tags',
  'directory_supported_locales'
]);

function canonicalValue(value: unknown): string | undefined {
  return JSON.stringify(value, (_key, item) =>
    item && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)))
      : item
  );
}

/** Verify the echoed settings before replacing a user's draft with a save response. */
export function applicationSettingsMatch(
  saved: object,
  submitted: Record<string, unknown>
): boolean {
  return Object.entries(submitted).every(([key, expected]) => {
    const actual = (saved as Record<string, unknown>)[key];
    if (SET_FIELDS.has(key) && Array.isArray(expected) && Array.isArray(actual)) {
      return (
        canonicalValue([...new Set(actual)].sort()) ===
        canonicalValue([...new Set(expected)].sort())
      );
    }
    if (
      ['support_url', 'privacy_url', 'terms_url'].includes(key) &&
      typeof expected === 'string' &&
      typeof actual === 'string'
    ) {
      return new URL(actual).href === new URL(expected).href;
    }
    return canonicalValue(actual) === canonicalValue(expected);
  });
}
