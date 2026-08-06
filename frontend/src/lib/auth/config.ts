import { api } from '$lib/api/client';

export interface AuthConfiguration {
  email_required: boolean;
  password_recovery_enabled: boolean;
}

export function loadAuthConfiguration(signal?: AbortSignal): Promise<AuthConfiguration> {
  return api<AuthConfiguration>('/auth/config', { signal });
}
