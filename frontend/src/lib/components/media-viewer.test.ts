// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync, tick } from 'svelte';
import MediaViewer from './MediaViewer.svelte';
import type { Attachment } from '$lib/chat/types';

vi.mock('$lib/media/authenticated', () => ({
  attachmentMediaPath: () => '/media/1/original',
  authenticatedMedia: () => ({ destroy() {} }),
  downloadAuthenticatedMedia: vi.fn()
}));
let component: ReturnType<typeof mount> | undefined;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});

describe('media viewer zoom', () => {
  it('fits images by default and exposes mouse and keyboard zoom controls', async () => {
    const target = document.createElement('div');
    document.body.append(target);
    const closed = vi.fn();
    component = mount(MediaViewer, {
      target,
      props: {
        attachment: {
          id: '1',
          origin_domain: 'chat.example',
          filename: 'photo.png',
          content_type: 'image/png',
          size: 100,
          width: 640,
          height: 480
        } as Attachment,
        onClose: closed
      }
    });
    flushSync();
    const button = (label: string) =>
      document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!;
    const fit = button('Fit image to window');
    expect(fit.textContent?.trim()).toBe('Fit');
    expect(fit.disabled).toBe(true);
    expect(button('Zoom out').disabled).toBe(true);
    button('Zoom in').click();
    await tick();
    const zoomed = Number.parseFloat(fit.textContent!);
    expect(zoomed).toBeGreaterThan(100);
    expect(button('Zoom out').disabled).toBe(false);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: '+' }));
    await tick();
    expect(Number.parseFloat(fit.textContent!)).toBeGreaterThan(zoomed);
    const image = document.querySelector('img')!;
    const wheel = new WheelEvent('wheel', { deltaY: 1, bubbles: true, cancelable: true });
    // Happy DOM does not yet initialize WheelEvent modifier/position fields.
    Object.defineProperties(wheel, {
      ctrlKey: { value: true },
      clientX: { value: 0 },
      clientY: { value: 0 }
    });
    image.dispatchEvent(wheel);
    await tick();
    expect(Number.parseFloat(fit.textContent!)).toBe(zoomed);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: '0' }));
    await tick();
    expect(fit.disabled).toBe(true);
    expect(button('Zoom out').disabled).toBe(true);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(closed).toHaveBeenCalledOnce();
  });
});
