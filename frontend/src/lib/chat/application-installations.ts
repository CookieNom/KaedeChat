import { api } from '$lib/api/client';

export type UserApplicationContext = 'guild' | 'bot_dm' | 'private_channel';
export type UserApplicationInstallationStatus = 'active' | 'suspended' | 'revoked';

export const SUSPENDED_USER_APPLICATION_EXPLANATION =
  'This application is suspended. Its commands are unavailable, and access settings cannot be changed until it is restored. You can still revoke it.';

export interface UserApplicationInstallation {
  id: string;
  application_ref: string;
  application_name: string;
  application_description: string | null;
  application_icon_hash: string | null;
  bot_user_ref: string;
  user_ref: string;
  scopes: string[];
  intents: string[];
  contexts: UserApplicationContext[];
  e2ee_participant_capable: boolean;
  grant_revision: string;
  status: UserApplicationInstallationStatus;
  revoked_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface UserApplicationGrant {
  scopes: string[];
  contexts: UserApplicationContext[];
  intents: string[];
}

export interface UserApplicationInstallPolicy {
  supported_install_types: Array<'guild_install' | 'user_install'>;
  user_install_scopes: string[];
  user_install_contexts: UserApplicationContext[];
}

export function userApplicationGrantFromPolicy(
  policy: UserApplicationInstallPolicy
): UserApplicationGrant {
  if (!policy.supported_install_types.includes('user_install')) {
    throw new Error('This application does not support user installation.');
  }
  return {
    scopes: [...policy.user_install_scopes],
    contexts: [...policy.user_install_contexts],
    intents: ['interactions']
  };
}

export function userApplicationInstallationPath(installationId?: string): string {
  const base = '/users/@me/application-installations';
  return installationId ? `${base}/${encodeURIComponent(installationId)}` : base;
}

export function userApplicationCanParticipateInEncryptedDm(
  installation: UserApplicationInstallation
): boolean {
  return (
    installation.status === 'active' &&
    installation.e2ee_participant_capable &&
    (installation.contexts.includes('private_channel') || installation.contexts.includes('bot_dm'))
  );
}

export function userApplicationInstallationCanEditGrants(
  installation: UserApplicationInstallation
): boolean {
  return installation.status === 'active';
}

export function userApplicationInstallationUnavailableReason(
  installation: UserApplicationInstallation
): string | null {
  if (installation.status === 'suspended') {
    return SUSPENDED_USER_APPLICATION_EXPLANATION;
  }
  if (installation.status === 'revoked') {
    return 'This application installation was revoked. Its commands and access settings are unavailable.';
  }
  return null;
}

export async function listUserApplicationInstallations(
  signal?: AbortSignal
): Promise<UserApplicationInstallation[]> {
  return api<UserApplicationInstallation[]>(userApplicationInstallationPath(), { signal });
}

export async function installUserApplication(
  applicationRef: string,
  grant: UserApplicationGrant
): Promise<UserApplicationInstallation> {
  return api<UserApplicationInstallation>(userApplicationInstallationPath(), {
    method: 'POST',
    body: JSON.stringify({ application_ref: applicationRef, ...grant })
  });
}

export async function updateUserApplicationInstallation(
  installationId: string,
  grant: Partial<UserApplicationGrant>
): Promise<UserApplicationInstallation> {
  return api<UserApplicationInstallation>(userApplicationInstallationPath(installationId), {
    method: 'PATCH',
    body: JSON.stringify(grant)
  });
}

export async function revokeUserApplicationInstallation(installationId: string): Promise<void> {
  await api<void>(userApplicationInstallationPath(installationId), { method: 'DELETE' });
}
