import { api, userErrorMessage } from '$lib/api/client';
import {
  parseGuildNavigation,
  type GuildNavigation,
  type GuildNavigationItem
} from '$lib/guild-navigation';

class GuildNavigationStore {
  navigation = $state<GuildNavigation>({ items: [] });
  loaded = $state(false);
  saving = $state(false);
  error = $state('');
  #generation = 0;
  #load: Promise<void> | null = null;

  apply(value: unknown): void {
    this.navigation = parseGuildNavigation(value);
    this.loaded = true;
    this.error = '';
  }

  load(force = false): Promise<void> {
    if (!force && (this.loaded || this.#load)) return this.#load ?? Promise.resolve();
    const generation = this.#generation;
    const request = api<GuildNavigation>('/users/@me/guild-navigation')
      .then((value) => {
        if (generation === this.#generation) this.apply(value);
      })
      .catch((caught: unknown) => {
        if (generation !== this.#generation) return;
        this.error = userErrorMessage(
          caught,
          'Could not load your guild order. Kaede is showing the default order; retry to restore your groups.'
        );
      })
      .finally(() => {
        if (this.#load === request) this.#load = null;
      });
    this.#load = request;
    return request;
  }

  async save(items: GuildNavigationItem[]): Promise<void> {
    if (this.saving) return;
    const previous = this.navigation;
    this.navigation = { items };
    this.saving = true;
    this.error = '';
    try {
      this.apply(
        await api<GuildNavigation>('/users/@me/guild-navigation', {
          method: 'PUT',
          body: JSON.stringify({ items })
        })
      );
    } catch (caught) {
      this.navigation = previous;
      this.error = userErrorMessage(
        caught,
        'Could not save your guild order. Your previous layout was restored; try again.'
      );
    } finally {
      this.saving = false;
    }
  }

  reset(): void {
    this.#generation += 1;
    this.#load = null;
    this.navigation = { items: [] };
    this.loaded = false;
    this.saving = false;
    this.error = '';
  }
}

export const guildNavigation = new GuildNavigationStore();
