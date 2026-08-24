import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Channel } from './types';

const apiMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: apiMock }));

import {
  activeThreadsForParent,
  createThread,
  filterForumPosts,
  forumDefaultLayout,
  forumDefaultReactionPayload,
  forumDefaultSort,
  forumRequiresTag,
  isForumChannel,
  isPinnedForumPost,
  isThreadChannel,
  ordinaryGuildChannels,
  parseNativeThreadCommand,
  parseCreatedThread,
  parseThreadPage,
  fetchThreads,
  setThreadMembership,
  threadParentAllowsChildCreation,
  threadMembersUpdateRemovesUser,
  threadRequiresE2EEActivation,
  createThreadFromMessage,
  updateThread
} from './threads';

function channel(id: string, type: number, overrides: Partial<Channel> = {}): Channel {
  return {
    id,
    origin_domain: 'guild.test',
    guild_id: '1',
    guild_domain: 'guild.test',
    type,
    name: `channel-${id}`,
    topic: null,
    position: Number(id),
    parent_id: null,
    parent_domain: null,
    rate_limit_per_user: 0,
    last_message_id: null,
    last_message_domain: null,
    ...overrides
  };
}

describe('thread and forum helpers', () => {
  beforeEach(() => apiMock.mockReset());

  it('identifies Discord thread/forum types and keeps threads out of ordinary channel order', () => {
    const channels = [
      channel('1', 0),
      channel('2', 10),
      channel('3', 11),
      channel('4', 12),
      channel('5', 15)
    ];
    expect(channels.map(isThreadChannel)).toEqual([false, true, true, true, false]);
    expect(isForumChannel(channels[4])).toBe(true);
    expect(ordinaryGuildChannels(channels).map((item) => item.id)).toEqual(['1', '5']);
  });

  it('fails closed from the child thread E2EE requirement without relying on its parent', () => {
    expect(
      threadRequiresE2EEActivation(
        channel('2', 11, { e2ee_required: true, encryption_state: 'plaintext' })
      )
    ).toBe(true);
    expect(
      threadRequiresE2EEActivation(
        channel('3', 11, {
          e2ee_required: true,
          encryption_mode: 'e2ee',
          encryption_state: 'active'
        })
      )
    ).toBe(false);
    expect(
      threadRequiresE2EEActivation(
        channel('4', 11, {
          e2ee_required: true,
          encryption_mode: 'plaintext',
          encryption_state: 'active'
        })
      )
    ).toBe(true);
    expect(
      threadRequiresE2EEActivation(
        channel('5', 11, { e2ee_required: false, encryption_state: 'plaintext' })
      )
    ).toBe(false);
    expect(
      threadRequiresE2EEActivation(
        channel('6', 15, { e2ee_required: true, encryption_state: 'plaintext' })
      )
    ).toBe(false);
  });

  it('allows starterless child creation only after an encrypted parent is active', () => {
    expect(
      threadParentAllowsChildCreation(
        channel('1', 0, { encryption_mode: 'e2ee', encryption_state: 'active' })
      )
    ).toBe(true);
    expect(
      threadParentAllowsChildCreation(
        channel('2', 5, { encryption_mode: 'e2ee', encryption_state: 'rekeying' })
      )
    ).toBe(false);
    expect(threadParentAllowsChildCreation(channel('3', 0))).toBe(true);
    expect(threadParentAllowsChildCreation(channel('4', 15))).toBe(false);
  });

  it('nests only active threads under their exact composite parent', () => {
    const parent = channel('1', 0);
    const threads = [
      channel('2', 11, { parent_id: '1', parent_domain: 'guild.test' }),
      channel('3', 11, { parent_id: '1', parent_domain: 'guild.test', archived: true }),
      channel('4', 11, { parent_id: '1', parent_domain: 'remote.test' })
    ];
    expect(activeThreadsForParent(threads, parent).map((item) => item.id)).toEqual(['2']);
  });

  it('matches removed thread members by exact federated identity', () => {
    const user = {
      id: '42',
      origin_domain: 'users.test'
    };
    expect(
      threadMembersUpdateRemovesUser(
        { removed_member_refs: [{ id: '42', origin_domain: 'users.test' }] },
        user
      )
    ).toBe(true);
    expect(
      threadMembersUpdateRemovesUser(
        {
          removed_member_ids: ['42'],
          removed_member_refs: [{ id: '42', origin_domain: 'other.test' }]
        },
        user
      )
    ).toBe(false);
    expect(threadMembersUpdateRemovesUser({ removed_member_ids: ['42'] }, user)).toBe(true);
  });

  it('matches any selected tag without disturbing the backend activity order', () => {
    const posts = [
      channel('2', 11, {
        name: 'Older pinned bug',
        pinned: true,
        applied_tag_ids: ['bug', 'web'],
        starter_message: { created_at: '2026-01-01T00:00:00Z' } as never
      }),
      channel('5', 11, {
        name: 'Recently active bug with an old starter',
        applied_tag_ids: ['bug'],
        starter_message: { created_at: '2025-05-01T00:00:00Z' } as never
      }),
      channel('3', 11, {
        name: 'Newest web bug',
        applied_tag_ids: ['bug', 'web'],
        starter_message: { created_at: '2026-03-01T00:00:00Z' } as never
      }),
      channel('4', 11, {
        name: 'Mobile request',
        applied_tag_ids: ['mobile'],
        starter_message: { created_at: '2026-04-01T00:00:00Z' } as never
      })
    ];
    expect(
      filterForumPosts(posts, {
        query: 'bug',
        selectedTagIds: new Set(['bug', 'web']),
        sort: 'recent_activity'
      }).map((item) => item.id)
    ).toEqual(['2', '5', '3']);
  });

  it('normalizes Discord numeric defaults and canonical response envelopes', () => {
    expect(forumDefaultSort({ default_sort_order: 1 })).toBe('creation_date');
    expect(forumDefaultLayout({ default_forum_layout: 2 })).toBe('gallery');
    expect(
      parseThreadPage({
        threads: [channel('2', 11)],
        members: [{}],
        has_more: true,
        next_cursor: 'opaque.page-token'
      })
    ).toMatchObject({
      has_more: true,
      next_cursor: 'opaque.page-token',
      threads: [{ id: '2' }]
    });
    expect(
      parseCreatedThread({ channel: channel('2', 11), starter_message: { id: '9' } }).channel.id
    ).toBe('2');
  });

  it('preserves a custom forum default reaction until the field is edited or cleared', () => {
    expect(forumDefaultReactionPayload('', '99')).toEqual({ emoji_id: '99' });
    expect(forumDefaultReactionPayload('👍', null)).toEqual({ emoji_name: '👍' });
    expect(forumDefaultReactionPayload('', null)).toBeNull();
  });

  it('decodes forum flags and never presents an archived post as pinned', () => {
    expect(forumRequiresTag(channel('1', 15, { flags: '16' }))).toBe(true);
    expect(isPinnedForumPost(channel('2', 11, { flags: '2' }))).toBe(true);
    expect(isPinnedForumPost(channel('3', 11, { flags: '2', archived: true }))).toBe(false);
  });

  it('parses the native thread command without treating ordinary bot commands as native', () => {
    expect(parseNativeThreadCommand('/thread name:"Release notes" message:Ready to ship')).toEqual({
      name: 'Release notes',
      message: 'Ready to ship'
    });
    expect(parseNativeThreadCommand('/thread name:test')).toBeNull();
    expect(parseNativeThreadCommand('/poll name:test message:hello')).toBeNull();
  });

  it('sends notification membership state without changing the thread membership route', async () => {
    apiMock.mockResolvedValue(undefined);
    await setThreadMembership(channel('2', 11), true, 'mentions');
    expect(apiMock).toHaveBeenCalledWith('/channels/2%40guild.test/thread-members/@me', {
      method: 'PUT',
      body: JSON.stringify({ flags: 0, notification_level: 'mentions' })
    });
  });

  it('uses server-backed title and repeated OR tag filters', async () => {
    apiMock.mockResolvedValue({
      threads: [],
      members: [],
      has_more: true,
      next_cursor: 'opaque+/=cursor'
    });
    await fetchThreads(channel('1', 15), {
      query: 'release notes',
      tagIds: ['7', '8'],
      sort: 'creation_date',
      includeArchived: true,
      cursor: 'opaque+/=cursor'
    });
    const [url] = apiMock.mock.calls[0] as [string];
    const params = new URL(`https://example.test${url}`).searchParams;
    expect(params.get('query')).toBe('release notes');
    expect(params.getAll('tag_id')).toEqual(['7', '8']);
    expect(params.get('sort_order')).toBe('1');
    expect(params.get('include_archived')).toBe('true');
    expect(params.has('archived')).toBe(false);
    expect(params.get('cursor')).toBe('opaque+/=cursor');
    expect(params.has('before')).toBe(false);
  });

  it('keeps timestamp-only pagination as an explicit legacy option', async () => {
    apiMock.mockResolvedValue({ threads: [], members: [], has_more: false, next_cursor: null });
    await fetchThreads(channel('1', 0), { before: '2026-08-24T12:00:00Z' });
    const [url] = apiMock.mock.calls[0] as [string];
    const params = new URL(`https://example.test${url}`).searchParams;
    expect(params.get('before')).toBe('2026-08-24T12:00:00Z');
    expect(params.has('cursor')).toBe(false);
  });

  it('creates an existing-message thread through the scoped endpoint without a fake starter id', async () => {
    apiMock.mockResolvedValue(channel('9', 11));
    await createThreadFromMessage(
      channel('1', 0, { default_thread_rate_limit_per_user: 30 }),
      { id: '7', origin_domain: 'guild.test' } as never,
      'Review'
    );
    expect(apiMock).toHaveBeenCalledWith(
      '/channels/1%40guild.test/messages/7%40guild.test/threads',
      {
        method: 'POST',
        body: JSON.stringify({ name: 'Review', auto_archive_duration: 1440 })
      }
    );
  });

  it('creates an attachment-only forum starter without inventing plaintext content', async () => {
    apiMock.mockResolvedValue({
      channel: channel('9', 11),
      starter_message: { id: '9', origin_domain: 'guild.test' }
    });
    await createThread(channel('1', 15), {
      name: 'Screenshots',
      content: '',
      attachmentIds: ['77'],
      clientNonce: 'forum-attachment-only'
    });
    expect(apiMock).toHaveBeenCalledWith('/channels/1%40guild.test/threads', {
      method: 'POST',
      body: JSON.stringify({
        name: 'Screenshots',
        applied_tag_ids: [],
        auto_archive_duration: 1440,
        message: {
          attachment_ids: ['77'],
          client_nonce: 'forum-attachment-only'
        }
      })
    });
  });

  it('creates a starterless child for an active encrypted parent', async () => {
    apiMock.mockResolvedValue({
      channel: channel('9', 11, {
        e2ee_required: true,
        encryption_state: 'plaintext'
      })
    });
    await createThread(channel('1', 0, { encryption_mode: 'e2ee', encryption_state: 'active' }), {
      name: 'Secure follow-up',
      type: 11
    });
    expect(apiMock).toHaveBeenCalledWith('/channels/1%40guild.test/threads', {
      method: 'POST',
      body: JSON.stringify({
        name: 'Secure follow-up',
        type: 11,
        applied_tag_ids: [],
        auto_archive_duration: 1440
      })
    });
  });

  it('renames a thread through the versioned thread update endpoint', async () => {
    apiMock.mockResolvedValue(channel('9', 11, { name: 'Renamed' }));
    await updateThread(channel('9', 11, { version: 'v3' }), { name: 'Renamed' });
    expect(apiMock).toHaveBeenCalledWith('/channels/9%40guild.test', {
      method: 'PATCH',
      body: JSON.stringify({ name: 'Renamed' }),
      headers: { 'If-Match': 'v3' }
    });
  });
});
