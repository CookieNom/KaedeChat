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
    if (!this.order.includes(key)) {
      this.order = options.append === false ? [key, ...this.order] : [...this.order, key];
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

  ingestCurrentUser(user: UserSummary): void {
    this.currentUser = user;
    this.users.upsert(user);
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

  clearSession(): void {
    this.guilds.clear();
    this.channels.clear();
    this.users.clear();
    this.messages.clear();
    this.readStates.clear();
    this.members.clear();
    this.relationships.clear();
    this.presences = {};
    this.currentUser = null;
  }
}

export const chatEntities = new ChatEntityStore();
