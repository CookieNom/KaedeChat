import { afterEach, describe, expect, it, vi } from 'vitest';

import { portal } from './portal';

describe('portal', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('removes a floating node when its owning block is destroyed', () => {
    const remove = vi.fn();
    const node = { remove } as unknown as HTMLElement;
    const appendChild = vi.fn();
    vi.stubGlobal('document', { body: { appendChild } });

    const action = portal(node);

    expect(appendChild).toHaveBeenCalledOnce();
    expect(appendChild).toHaveBeenCalledWith(node);

    action.destroy();

    expect(remove).toHaveBeenCalledOnce();
  });
});
