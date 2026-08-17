import { api } from '$lib/api/client';

export interface AuthConfiguration {
  email_required: boolean;
  password_recovery_enabled: boolean;
  gif_picker_enabled: boolean;
  message_search_enabled: boolean;
  e2ee_activation_enabled: boolean;
  turnstile: {
    enabled: boolean;
    site_key: string | null;
  };
}

export function loadAuthConfiguration(signal?: AbortSignal): Promise<AuthConfiguration> {
  return api<AuthConfiguration>('/auth/config', { signal });
}
