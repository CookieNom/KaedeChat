// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import AttachmentSpoiler from './AttachmentSpoiler.svelte';
import UploadPreviewTray from './UploadPreviewTray.svelte';

it('does not render spoiler media before explicit reveal, and can hide it again', async () => {
  const render = vi.fn(() => '<img alt="secret image" src="/secret.png" />');
  const component = mount(AttachmentSpoiler, {
    target: document.body,
    props: {
      filename: 'SPOILER_secret.png',
      identity: '123',
      children: createRawSnippet(() => ({ render }))
    }
  });
  flushSync();
  expect(render).not.toHaveBeenCalled();
  expect(document.querySelector('img')).toBeNull();
  flushSync(() =>
    document.querySelector<HTMLButtonElement>('[aria-label="Reveal spoiler attachment"]')!.click()
  );
  expect(document.querySelector('img')).not.toBeNull();
  flushSync(() => document.querySelector<HTMLButtonElement>('.spoiler-hide')!.click());
  expect(document.querySelector('img')).toBeNull();
  await unmount(component);
});

it('offers a queued attachment spoiler toggle without removing or sending the file', async () => {
  const onSpoiler = vi.fn();
  const onRemove = vi.fn();
  const component = mount(UploadPreviewTray, {
    target: document.body,
    props: {
      uploads: [
        {
          key: 'a',
          file: new File(['file'], 'photo.png', { type: 'image/png' }),
          progress: 100,
          status: 'ready'
        }
      ],
      onSpoiler,
      onRemove
    }
  });
  flushSync();
  flushSync(() => document.querySelector<HTMLButtonElement>('[aria-pressed]')!.click());
  expect(onSpoiler).toHaveBeenCalledWith('a', true);
  expect(onRemove).not.toHaveBeenCalled();
  flushSync(() =>
    document.querySelector<HTMLButtonElement>('[aria-label="Edit attachment photo.png"]')!.click()
  );
  expect(document.querySelector('dialog')?.open).toBe(true);
  const checkbox = document.querySelector<HTMLInputElement>('dialog input[type="checkbox"]')!;
  flushSync(() => {
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
  });
  expect(onSpoiler).toHaveBeenCalledTimes(2);
  flushSync(() => document.querySelector<HTMLButtonElement>('dialog button')!.click());
  expect(document.querySelector('dialog')).toBeNull();
  await unmount(component);
});
