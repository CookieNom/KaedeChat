import { entityKey } from '$lib/chat/refs';
import type {
  Channel,
  Guild,
  GuildMemberSummary,
  Message,
  ReadStateStatus,
  Relationship,
  PresenceStatus,
  UserSummary
} from '$lib/chat/types';

type Keyed = { id: string; origin_domain: string };

export class NormalizedCollection<T> {
  records = $state<Record<string, T>>({});
  order = $state<string[]>([]);

  constructor(private readonly keyOf: (item: T) => string) {}

  get values(): T[] {
    return this.order.flatMap((key) => (this.records[key] ? [this.records[key]] : []));
  }

  get(key: string): T | undefined {
    return this.records[key];
  }

  replace(items: T[]): void {
    const records: Record<string, T> = {};
    const order: string[] = [];
    for (const item of items) {
      const key = this.keyOf(item);
      if (!(key in records)) order.push(key);
      records[key] = item;
    }
    this.records = records;
    this.order = order;
  }

  replaceWhere(items: T[], predicate: (item: T) => boolean): void {
    const retained = this.values.filter((item) => !predicate(item));
    this.replace([...retained, ...items]);
  }

  upsert(item: T, options: { append?: boolean } = {}): void {
    const key = this.keyOf(item);
    const existing = this.records[key];
    this.records = { ...this.records, [key]: existing ? { ...existing, ...item } : item };
    if (options.append === false) {
      this.order = [key, ...this.order.filter((itemKey) => itemKey !== key)];
    } else if (!this.order.includes(key)) {
      this.order = [...this.order, key];
    }
  }

  upsertMany(items: T[]): void {
    for (const item of items) this.upsert(item);
  }

  update(key: string, transform: (item: T) => T): void {
    const current = this.records[key];
    if (current) this.records = { ...this.records, [key]: transform(current) };
  }

  remove(key: string): void {
    if (!(key in this.records)) return;
    const records = { ...this.records };
    delete records[key];
    this.records = records;
    this.order = this.order.filter((item) => item !== key);
  }

  clear(): void {
    this.records = {};
    this.order = [];
  }
}

const keyed = (item: Keyed) => entityKey(item);
const readStateKey = (item: ReadStateStatus) => `${item.channel_id}@${item.channel_domain}`;
const memberKey = (item: GuildMemberSummary) =>
  `${item.guild_id}@${item.guild_domain}:${entityKey(item.user)}`;
const relationshipKey = (item: Relationship) => entityKey(item.user);

export class ChatEntityStore {
  guilds = new NormalizedCollection<Guild>(keyed);
  channels = new NormalizedCollection<Channel>(keyed);
  users = new NormalizedCollection<UserSummary>(keyed);
  messages = new NormalizedCollection<Message>(keyed);
  readStates = new NormalizedCollection<ReadStateStatus>(readStateKey);
  members = new NormalizedCollection<GuildMemberSummary>(memberKey);
  relationships = new NormalizedCollection<Relationship>(relationshipKey);
  presences = $state<Record<string, PresenceStatus>>({});
  currentUser = $state<UserSummary | null>(null);

  ingestGuilds(guilds: Guild[]): void {
    this.guilds.upsertMany(guilds);
    this.channels.upsertMany(guilds.flatMap((guild) => guild.channels ?? []));
  }

  ingestDirectMessages(channels: Channel[]): void {
    this.channels.replaceWhere(channels, (channel) => channel.guild_id === null);
    this.users.upsertMany(channels.flatMap((channel) => channel.recipients ?? []));
  }

  ingestCurrentUser(user: UserSummary): void {
    this.currentUser = user;
    this.users.upsert(user);
  }

  applyUserProfile(user: UserSummary): void {
    const key = entityKey(user);
    this.users.upsert(user);
    this.messages.replace(
      this.messages.values.map((message) =>
        message.author_id === user.id && message.author_domain === user.origin_domain
          ? { ...message, author: { ...(message.author ?? user), ...user } }
          : message
      )
    );
    this.members.replace(
      this.members.values.map((member) =>
        entityKey(member.user) === key ? { ...member, user: { ...member.user, ...user } } : member
      )
    );
    this.channels.replace(
      this.channels.values.map((channel) => ({
        ...channel,
        recipients: channel.recipients?.map((recipient) =>
          entityKey(recipient) === key ? { ...recipient, ...user } : recipient
        )
      }))
    );
    this.relationships.replace(
      this.relationships.values.map((relationship) =>
        entityKey(relationship.user) === key
          ? { ...relationship, user: { ...relationship.user, ...user } }
          : relationship
      )
    );
    if (this.currentUser && entityKey(this.currentUser) === key) {
      this.currentUser = { ...this.currentUser, ...user };
    }
  }

  setPresence(user: Pick<UserSummary, 'id' | 'origin_domain'>, status: PresenceStatus): void {
    this.presences = { ...this.presences, [entityKey(user)]: status };
  }

  presenceFor(user: Pick<UserSummary, 'id' | 'origin_domain'>): PresenceStatus {
    return this.presences[entityKey(user)] ?? 'offline';
  }

  ingestMembers(members: GuildMemberSummary[]): void {
    this.members.replace(members);
    for (const member of members) {
      this.users.upsert(member.user);
      if (member.presence) this.setPresence(member.user, member.presence);
    }
  }

  messagesFor(channelId: string, channelDomain: string): Message[] {
    return this.messages.values.filter(
      (message) => message.channel_id === channelId && message.channel_domain === channelDomain
    );
  }

  beginGatewaySession(user: UserSummary): void {
    const preserveMembers = Boolean(
      this.currentUser && entityKey(this.currentUser) === entityKey(user)
    );
    this.clearSession({ preserveMembers });
    this.ingestCurrentUser(user);
  }

  clearSession(options: { preserveMembers?: boolean } = {}): void {
    this.guilds.clear();
    this.channels.clear();
    this.users.clear();
    this.messages.clear();
    this.readStates.clear();
    if (!options.preserveMembers) this.members.clear();
    this.relationships.clear();
    this.presences = {};
    this.currentUser = null;
  }
}

export const chatEntities = new ChatEntityStore();
