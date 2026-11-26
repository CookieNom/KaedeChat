import { api } from '$lib/api/client';
import { isNativeDesktop } from '$lib/platform/native';
import { shoppingDestination, type LinkRule } from './links';

interface State {
  available: boolean;
  enabled: boolean;
  prompt: boolean;
  rules: LinkRule[];
}
const empty = (): State => ({ available: false, enabled: false, prompt: false, rules: [] });
class ShoppingLinks {
  state = $state<State>(empty());
  busy = $state(false);
  error = $state('');
  private generation = 0;
  private request = 0;
  private loading = false;
  reset() {
    this.generation++;
    this.request++;
    this.state = empty();
    this.loading = false;
    this.busy = false;
    this.error = '';
  }
  private endpoint(open = false) {
    return (
      '/users/@me/shopping-links' +
      (open ? '/open' : '') +
      '?platform=' +
      (isNativeDesktop() ? 'desktop' : 'web')
    );
  }
  async load() {
    if (this.busy || this.loading || this.state.prompt) return;
    this.loading = true;
    const id = ++this.request;
    try {
      const state = await api<State>(this.endpoint(true), { method: 'POST' });
      if (id === this.request) this.state = state;
    } catch {
      if (id === this.request) this.state = empty();
    } finally {
      if (id === this.request) this.loading = false;
    }
  }
  async choose(enabled: boolean) {
    if (this.busy) return;
    const generation = this.generation;
    this.request++;
    this.loading = false;
    this.busy = true;
    this.error = '';
    try {
      const state = await api<State>(this.endpoint(), {
        method: 'PUT',
        body: JSON.stringify({ enabled })
      });
      if (generation === this.generation) this.state = state;
    } catch {
      if (generation === this.generation)
        this.error = 'Could not save your choice. Please try again.';
    } finally {
      if (generation === this.generation) this.busy = false;
    }
  }
  activate = (event: MouseEvent) => {
    if (event.button > 1 || event.defaultPrevented || !(event.target instanceof Element)) return;
    const anchor = event.target.closest<HTMLAnchorElement>('a[data-shopping-link]');
    const original = anchor?.getAttribute('href');
    if (!anchor || !original) return;
    const destination = shoppingDestination(original, this.state.rules);
    if (destination === original) return;
    // Native navigation handles keyboard activation, middle-click and modifier keys.
    // Keep the shared destination intact for copying and the normal context menu.
    anchor.setAttribute('href', destination);
    setTimeout(() => {
      if (anchor.getAttribute('href') === destination) anchor.setAttribute('href', original);
    }, 0);
  };
}
export const shopping = new ShoppingLinks();
