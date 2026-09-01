import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: apiMock }));

import { Permission } from '$lib/generated/permissions';
import {
  announcementTargets,
  canReadAnnouncementChannel,
  canPublishAnnouncementMessage,
  channelFollowSystemMessageText,
  createAnnouncementFollow,
  deleteAnnouncementFollow,
  isPublishedAnnouncement,
  listAnnouncementFollows,
  MESSAGE_FLAG_CROSSPOSTED,
  publishAnnouncementMessage
} from './announcements';
import type { Channel, Guild, Message, UserSummary } from './types';

const user: UserSummary = {
  id: '7',
  origin_domain: 'chat.example',
  username: 'maple',
  display_name: null,
  avatar_hash: null,
  handle: 'maple@chat.example'
};

function channel(id: string, type: number, permissions: bigint, domain = 'chat.example'): Channel {
  return {
    id,
    origin_domain: domain,
    guild_id: '1',
    guild_domain: domain,
    type,
    name: `channel-${id}`,
    topic: null,
    position: Number(id),
    parent_id: null,
    parent_domain: null,
    permissions: permissions.toString(),
    rate_limit_per_user: 0,
    last_message_id: null,
    last_message_domain: null
  };
}

function guild(id: string, name: string, channels: Channel[]): Guild {
  return {
    id,
    origin_domain: channels[0]?.origin_domain ?? 'chat.example',
    name,
    description: null,
    icon_hash: null,
    owner_id: '9',
    permission_generation: '1',
    unavailable: false,
    channels
  };
}

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: '10',
    origin_domain: 'chat.example',
    channel_id: '2',
    channel_domain: 'chat.example',
    author_id: user.id,
    author_domain: user.origin_domain,
    author: user,
    content: 'News',
    message_type: 0,
    flags: 0,
    client_nonce: null,
    referenced_message_id: null,
    referenced_message_domain: null,
    mention_user_refs: [],
    edited_at: null,
    deleted_at: null,
    created_at: '2026-08-27T12:00:00Z',
    ...overrides
  };
}

function follow() {
  return {
    id: '42',
    ref: '42@remote.example',
    source_channel_id: '2',
    source_channel_domain: 'chat.example',
    target_channel_id: '3',
    target_channel_domain: 'remote.example',
    creator_id: '7',
    creator_domain: 'chat.example',
    active: true,
    federated: true,
    generation: '1',
    lifecycle_state: 'active',
    name: null,
    avatar_hash: null,
    created_at: '2026-08-27T12:00:00Z',
    updated_at: '2026-08-27T12:00:00Z'
  };
}

describe('announcement client', () => {
  beforeEach(() => apiMock.mockReset());

  it('allows announcement-follow discovery with View Channel alone', () => {
    const news = channel('2', 5, Permission.VIEW_CHANNEL);

    expect(canReadAnnouncementChannel(news, guild('1', 'Local', [news]))).toBe(true);
    expect(
      canReadAnnouncementChannel(
        channel('3', 5, Permission.READ_MESSAGE_HISTORY),
        guild('1', 'Local', [news])
      )
    ).toBe(false);
  });

  it('uses canonical encoded routes for list, create, delete, and publish', async () => {
    apiMock
      .mockResolvedValueOnce([follow()])
      .mockResolvedValueOnce(follow())
      .mockResolvedValueOnce(undefined)
      .mockResolvedValueOnce({ ...message(), flags: MESSAGE_FLAG_CROSSPOSTED });
    const source = channel('2', 5, Permission.ADMINISTRATOR);
    const post = message();
    await listAnnouncementFollows('2@chat.example');
    await createAnnouncementFollow('2@chat.example', '3@remote.example');
    await deleteAnnouncementFollow('2@chat.example', '42@remote.example');
    await publishAnnouncementMessage(source, post);

    expect(apiMock.mock.calls).toEqual([
      ['/channels/2%40chat.example/followers', { signal: undefined }],
      [
        '/channels/2%40chat.example/followers',
        { method: 'POST', body: JSON.stringify({ target_channel_id: '3@remote.example' }) }
      ],
      ['/channels/2%40chat.example/followers/42%40remote.example', { method: 'DELETE' }],
      ['/channels/2%40chat.example/messages/10%40chat.example/crosspost', { method: 'POST' }]
    ]);
  });

  it('rejects substituted follower and publish response lineage', async () => {
    apiMock.mockResolvedValueOnce({ ...follow(), target_channel_id: '30' });
    await expect(createAnnouncementFollow('2@chat.example', '3@remote.example')).rejects.toThrow(
      /requested lineage/u
    );

    apiMock.mockResolvedValueOnce([{ ...follow(), id: '43', ref: '43@remote.example' }, follow()]);
    await expect(listAnnouncementFollows('2@chat.example')).rejects.toThrow(/unordered/u);

    apiMock.mockResolvedValueOnce({ ...message(), channel_id: '20' });
    await expect(
      publishAnnouncementMessage(channel('2', 5, Permission.ADMINISTRATOR), message())
    ).rejects.toThrow(/requested lineage/u);

    apiMock.mockResolvedValueOnce({ ...message(), flags: 0 });
    await expect(
      publishAnnouncementMessage(channel('2', 5, Permission.ADMINISTRATOR), message())
    ).rejects.toThrow(/requested lineage/u);
  });

  it('offers only plaintext regular text targets with Manage Webhooks', () => {
    const manage = Permission.MANAGE_WEBHOOKS;
    const allowedText = channel('3', 0, manage);
    const remoteText = channel('4', 0, Permission.ADMINISTRATOR, 'remote.example');
    const deniedNews = channel('8', 5, Permission.ADMINISTRATOR, 'remote.example');
    const denied = channel('5', 0, Permission.VIEW_CHANNEL);
    const voice = channel('6', 2, manage);
    const encrypted = { ...channel('7', 0, manage), encryption_mode: 'e2ee' as const };
    const e2eeRequired = { ...channel('9', 0, manage), e2ee_required: true };

    expect(
      announcementTargets([
        guild('1', 'Local', [denied, allowedText, voice, encrypted, e2eeRequired]),
        guild('2', 'Remote', [remoteText, deniedNews])
      ]).map((target) => target.ref)
    ).toEqual(['3@chat.example', '4@remote.example']);
  });

  it('matches publish ownership, moderation, authority, encryption, and flag rules', () => {
    const source = channel('2', 5, Permission.SEND_MESSAGES);
    expect(canPublishAnnouncementMessage(source, message(), user, true, false)).toBe(true);
    expect(
      canPublishAnnouncementMessage(source, message({ author_id: '8' }), user, true, false)
    ).toBe(false);
    expect(
      canPublishAnnouncementMessage(source, message({ author_id: '8' }), user, true, true)
    ).toBe(true);
    expect(
      canPublishAnnouncementMessage(
        { ...source, origin_domain: 'remote.example' },
        message({ channel_domain: 'remote.example' }),
        user,
        true,
        true
      )
    ).toBe(true);
    expect(canPublishAnnouncementMessage(source, message({ flags: 1 }), user, true, true)).toBe(
      false
    );
    expect(canPublishAnnouncementMessage(source, message({ e2ee: {} }), user, true, true)).toBe(
      false
    );
    expect(isPublishedAnnouncement(message({ flags: 3 }))).toBe(true);
  });

  it('renders Discord type-12 follow notices from known or federated channel references', () => {
    const follow = message({
      content: 'upstream-news',
      message_type: 12,
      message_reference: {
        type: 0,
        channel_id: '8',
        channel_domain: 'remote.example',
        guild_id: '9',
        guild_domain: 'remote.example'
      }
    });
    const known = channel('8', 5, Permission.VIEW_CHANNEL, 'remote.example');
    known.name = 'release-notes';

    expect(channelFollowSystemMessageText(follow, [known], 'Maple')).toBe(
      'Maple has added #release-notes to this channel. Its most important updates will show up here.'
    );
    expect(channelFollowSystemMessageText(follow, [], 'Maple')).toContain('#upstream-news');
    expect(
      channelFollowSystemMessageText(message({ ...follow, content: null }), [], 'Maple')
    ).toContain('#8@remote.example');
    expect(channelFollowSystemMessageText(message(), [], 'Maple')).toBeNull();
  });
});
