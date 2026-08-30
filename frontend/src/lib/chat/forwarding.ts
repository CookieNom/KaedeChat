import { entityRef } from './refs';
import type { Channel, Message } from './types';
import { Permission } from '$lib/generated/permissions';

function permissions(channel: Channel): bigint {
  try {
    return BigInt(channel.permissions ?? '0');
  } catch {
    return 0n;
  }
}

function allows(channel: Channel, required: bigint): boolean {
  const effective = permissions(channel);
  return (
    (effective & Permission.ADMINISTRATOR) === Permission.ADMINISTRATOR ||
    (effective & required) === required
  );
}

export function forwardUnavailableReason(
  message: Pick<Message, 'message_type' | 'poll'> & Partial<Pick<Message, 'e2ee' | 'e2ee_verified'>>
): string | null {
  if (message.poll) return 'Poll messages cannot be forwarded.';
  if (message.message_type === 3) return 'Call messages cannot be forwarded.';
  if (![0, 19, 20, 23].includes(message.message_type)) {
    return 'System messages cannot be forwarded.';
  }
  if (
    message.e2ee &&
    (!message.e2ee_verified ||
      message.e2ee.forward_projection_version !== 2 ||
      typeof message.e2ee.forward_projection_digest !== 'string')
  ) {
    return 'This encrypted snapshot is not verified for forwarding.';
  }
  return null;
}

function canForwardToResolvedChannel(
  source: Channel,
  target: Channel,
  sourceNsfw: boolean,
  targetNsfw: boolean
): boolean {
  if (
    (sourceNsfw && !targetNsfw) ||
    target.archived ||
    (target.locked && !allows(target, Permission.MANAGE_THREADS))
  ) {
    return false;
  }
  if (target.guild_id === null) return [1, 3].includes(target.type);
  if ([10, 11, 12].includes(target.type)) {
    return allows(target, Permission.SEND_MESSAGES_IN_THREADS);
  }
  return [0, 2, 5, 13].includes(target.type) && allows(target, Permission.SEND_MESSAGES);
}

export function canForwardToChannel(source: Channel, target: Channel): boolean {
  return canForwardToResolvedChannel(source, target, source.nsfw ?? false, target.nsfw ?? false);
}

function effectiveNsfw(channel: Channel, channels: ReadonlyMap<string, Channel>): boolean | null {
  if (channel.guild_id === null) return false;
  if (![10, 11, 12].includes(channel.type)) return channel.nsfw ?? false;
  if (channel.parent_id === null || channel.parent_domain === null) return null;
  return channels.get(`${channel.parent_id}@${channel.parent_domain}`)?.nsfw ?? null;
}

export function forwardingDestinations(source: Channel, channels: Iterable<Channel>): Channel[] {
  const candidates = [...channels];
  const byRef = new Map(candidates.map((channel) => [entityRef(channel), channel]));
  byRef.set(entityRef(source), source);
  const sourceNsfw = effectiveNsfw(source, byRef);
  if (sourceNsfw === null) return [];
  const unique = new Map<string, Channel>();
  for (const channel of candidates) {
    const targetNsfw = effectiveNsfw(channel, byRef);
    if (
      targetNsfw !== null &&
      canForwardToResolvedChannel(source, channel, sourceNsfw, targetNsfw)
    ) {
      unique.set(entityRef(channel), channel);
    }
  }
  return [...unique.values()].sort((left, right) => {
    if (entityRef(left) === entityRef(source)) return -1;
    if (entityRef(right) === entityRef(source)) return 1;
    return (left.name ?? '').localeCompare(right.name ?? '');
  });
}
