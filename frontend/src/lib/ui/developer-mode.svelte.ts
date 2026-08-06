export type ClientPreferenceSettings = Record<string, unknown> | null | undefined;

export function developerModeFromSettings(settings: ClientPreferenceSettings): boolean {
  return settings?.developer_mode === true;
}

class DeveloperModePreference {
  enabled = $state(false);

  apply(settings: ClientPreferenceSettings): void {
    this.enabled = developerModeFromSettings(settings);
  }

  reset(): void {
    this.enabled = false;
  }
}

export const developerMode = new DeveloperModePreference();
