import { describe, expect, it } from 'vitest';
import {
  SUSPENDED_USER_APPLICATION_EXPLANATION,
  userApplicationCanParticipateInEncryptedDm,
  userApplicationGrantFromPolicy,
  userApplicationInstallationCanEditGrants,
  userApplicationInstallationPath,
  userApplicationInstallationUnavailableReason,
  type UserApplicationInstallation
} from './application-installations';

describe('user application installations', () => {
  it('uses the account-scoped lifecycle and encodes installation ids', () => {
    expect(userApplicationInstallationPath()).toBe('/users/@me/application-installations');
    expect(userApplicationInstallationPath('1/2')).toBe(
      '/users/@me/application-installations/1%2F2'
    );
  });

  it('derives account authorization from the application install policy', () => {
    expect(
      userApplicationGrantFromPolicy({
        supported_install_types: ['guild_install', 'user_install'],
        user_install_scopes: ['applications.commands', 'interactions.respond', 'attachments.read'],
        user_install_contexts: ['bot_dm', 'private_channel']
      })
    ).toEqual({
      scopes: ['applications.commands', 'interactions.respond', 'attachments.read'],
      contexts: ['bot_dm', 'private_channel'],
      intents: ['interactions']
    });
    expect(() =>
      userApplicationGrantFromPolicy({
        supported_install_types: ['guild_install'],
        user_install_scopes: ['applications.commands', 'interactions.respond'],
        user_install_contexts: ['guild']
      })
    ).toThrow(/does not support user installation/);
  });

  it('offers encrypted DM consent only for active participant-capable grants', () => {
    const installation: UserApplicationInstallation = {
      id: '1',
      application_ref: '2@apps.example',
      application_name: 'Tasks',
      application_description: null,
      application_icon_hash: null,
      bot_user_ref: '3@apps.example',
      user_ref: '4@chat.example',
      scopes: ['applications.commands'],
      intents: ['interactions'],
      contexts: ['private_channel'],
      e2ee_participant_capable: true,
      grant_revision: '1',
      status: 'active',
      revoked_at: null,
      created_at: null,
      updated_at: null
    };
    expect(userApplicationCanParticipateInEncryptedDm(installation)).toBe(true);
    expect(
      userApplicationCanParticipateInEncryptedDm({
        ...installation,
        e2ee_participant_capable: false
      })
    ).toBe(false);
    expect(
      userApplicationCanParticipateInEncryptedDm({
        ...installation,
        contexts: ['guild']
      })
    ).toBe(false);
    expect(
      userApplicationCanParticipateInEncryptedDm({
        ...installation,
        status: 'suspended'
      })
    ).toBe(false);
  });

  it('locks suspended and revoked grants while explaining suspension', () => {
    const installation: UserApplicationInstallation = {
      id: '1',
      application_ref: '2@apps.example',
      application_name: 'Tasks',
      application_description: null,
      application_icon_hash: null,
      bot_user_ref: '3@apps.example',
      user_ref: '4@chat.example',
      scopes: ['applications.commands'],
      intents: ['interactions'],
      contexts: ['private_channel'],
      e2ee_participant_capable: true,
      grant_revision: '1',
      status: 'active',
      revoked_at: null,
      created_at: null,
      updated_at: null
    };

    expect(userApplicationInstallationCanEditGrants(installation)).toBe(true);
    expect(userApplicationInstallationUnavailableReason(installation)).toBeNull();

    const suspended = { ...installation, status: 'suspended' as const };
    expect(userApplicationInstallationCanEditGrants(suspended)).toBe(false);
    expect(userApplicationInstallationUnavailableReason(suspended)).toBe(
      SUSPENDED_USER_APPLICATION_EXPLANATION
    );

    const revoked = { ...installation, status: 'revoked' as const };
    expect(userApplicationInstallationCanEditGrants(revoked)).toBe(false);
    expect(userApplicationInstallationUnavailableReason(revoked)).toMatch(/revoked/i);
  });
});
