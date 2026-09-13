// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';
import { preparePrivateLinks, stripLinkTracking } from './link-privacy';

const cases = [
  [
    'https://open.spotify.com/track/7?si=abc&utm_source=copy-link',
    'https://open.spotify.com/track/7?utm_source=copy-link'
  ],
  ['https://music.youtube.com/watch?v=123&si=abc', 'https://music.youtube.com/watch?v=123'],
  [
    'See [this](https://youtu.be/123?si=abc&t=42#part).',
    'See [this](https://youtu.be/123?t=42#part).'
  ],
  [
    'https://www.youtube.com/watch?si=a&v=123&si=b&x=%20+%2f',
    'https://www.youtube.com/watch?v=123&x=%20+%2f'
  ],
  ['https://youtu.be/123?%73i=&t=4', 'https://youtu.be/123?t=4'],
  [
    'https://youtu.be/123?si=abc https://open.spotify.com/track/7?si=def',
    'https://youtu.be/123 https://open.spotify.com/track/7'
  ],
  [
    'https://youtube.com.evil.test/?si=a https://example.com/?si=b',
    'https://youtube.com.evil.test/?si=a https://example.com/?si=b'
  ],
  [
    'https://youtube.com/watch?v=1#fragment?si=keep',
    'https://youtube.com/watch?v=1#fragment?si=keep'
  ],
  ['https://youtu.be/123?SI=keep&list=abc&t=10', 'https://youtu.be/123?SI=keep&list=abc&t=10']
];

describe('link privacy', () => {
  it.each(cases)('strips only si: %s', (input, expected) => {
    expect(stripLinkTracking(input)).toBe(expected);
  });
  it('waits for a choice and remembers approval and decline', async () => {
    const input = 'https://youtu.be/123?si=abc&t=4';
    for (const enabled of [true, false]) {
      let choose!: (value: boolean) => void;
      const prompt = vi.fn(
        () =>
          new Promise<boolean>((resolve) => {
            choose = resolve;
          })
      );
      const sent = vi.fn();
      const pending = preparePrivateLinks(input, `test-${enabled}`, prompt).then(sent);
      await Promise.resolve();
      expect(sent).not.toHaveBeenCalled();
      choose(enabled);
      await pending;
      const expected = enabled ? 'https://youtu.be/123?t=4' : input;
      expect(sent).toHaveBeenCalledWith(expected);
      expect(await preparePrivateLinks(input, `test-${enabled}`, prompt)).toBe(expected);
      expect(prompt).toHaveBeenCalledTimes(1);
    }
  });
  it('does not ask for untracked links', async () => {
    const prompt = vi.fn();
    expect(await preparePrivateLinks('https://youtu.be/123?t=4', 'untracked', prompt)).toBe(
      'https://youtu.be/123?t=4'
    );
    expect(prompt).not.toHaveBeenCalled();
  });
});

it('uses saved choices after reload', async () => {
  const prompt = vi.fn();
  for (const enabled of [true, false]) {
    localStorage.setItem(`kaede:strip-link-si:restored-${enabled}`, String(enabled));
    expect(
      await preparePrivateLinks('https://youtu.be/123?si=a', `restored-${enabled}`, prompt)
    ).toBe(enabled ? 'https://youtu.be/123' : 'https://youtu.be/123?si=a');
  }
  expect(prompt).not.toHaveBeenCalled();
});

it('requires a button choice in the privacy dialog', async () => {
  const sent = vi.fn();
  const pending = preparePrivateLinks('https://youtu.be/123?si=a', 'dialog-test').then(sent);
  const dialog = document.querySelector('dialog')!;
  expect(dialog.open).toBe(true);
  const cancel = new Event('cancel', { cancelable: true });
  dialog.dispatchEvent(cancel);
  expect(cancel.defaultPrevented).toBe(true);
  await Promise.resolve();
  expect(sent).not.toHaveBeenCalled();
  dialog.querySelectorAll('button')[1].click();
  await pending;
  expect(sent).toHaveBeenCalledWith('https://youtu.be/123');
  expect(document.querySelector('dialog')).toBeNull();
});
