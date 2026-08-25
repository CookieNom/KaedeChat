import { assetUrl } from '$lib/media/assets';
import type { GuildSticker } from './types';

const DOMAIN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const SNOWFLAKE = /^[1-9][0-9]{0,18}$/;
const NAME = /^[A-Za-z0-9_]{2,32}$/;
const TOKEN = /^<sticker:([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]{1,253})>$/;

export interface StickerOption extends GuildSticker {
  url: string;
  value: string;
}

function validSnowflake(value: string): boolean {
  return SNOWFLAKE.test(value) && BigInt(value) <= 9223372036854775807n;
}

export function stickerToken(sticker: Pick<GuildSticker, 'id' | 'origin_domain' | 'name'>): string {
  if (
    !validSnowflake(sticker.id) ||
    !DOMAIN.test(sticker.origin_domain) ||
    !NAME.test(sticker.name)
  ) {
    return '';
  }
  return `<sticker:${sticker.name}:${sticker.id}@${sticker.origin_domain.toLowerCase()}>`;
}

export function stickerFromToken(
  value: string
): { name: string; id: string; domain: string } | null {
  const match = TOKEN.exec(value.trim());
  if (!match || !validSnowflake(match[2]) || !DOMAIN.test(match[3])) return null;
  return { name: match[1], id: match[2], domain: match[3].toLowerCase() };
}

export function stickerUrl(id: string, domain: string): string {
  if (!validSnowflake(id) || !DOMAIN.test(domain)) return '';
  const normalized = domain.toLowerCase();
  const localDomain = typeof window === 'undefined' ? '' : window.location.hostname.toLowerCase();
  const path = `/media/stickers/${id}/thumbnail_512`;
  return normalized === localDomain ? path : `https://${normalized}${path}`;
}

export function stickerOptions(
  stickers: GuildSticker[],
  activeGuild?: { id: string; origin_domain: string }
): StickerOption[] {
  return stickers
    .filter((sticker) => sticker.media_hash)
    .map((sticker) => ({
      ...sticker,
      value: stickerToken(sticker),
      url: assetUrl(sticker.media_hash ?? '', 'thumbnail_512', sticker.origin_domain)
    }))
    .filter((sticker) => sticker.value && sticker.url)
    .sort((left, right) => {
      const leftActive =
        activeGuild &&
        left.guild_id === activeGuild.id &&
        left.guild_domain === activeGuild.origin_domain
          ? 0
          : 1;
      const rightActive =
        activeGuild &&
        right.guild_id === activeGuild.id &&
        right.guild_domain === activeGuild.origin_domain
          ? 0
          : 1;
      return (
        leftActive - rightActive ||
        (left.guild_name ?? '').localeCompare(right.guild_name ?? '') ||
        left.name.localeCompare(right.name)
      );
    });
}
