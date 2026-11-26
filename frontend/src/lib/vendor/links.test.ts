import { describe, expect, it } from 'vitest';
import { shoppingDestination } from './links';

describe('shopping link destinations', () => {
  const rule = { host: 'www.shop.test', parameter: 'tag', value: 'store-20', replace: false };
  const original = 'https://www.shop.test/item?a=%20&color=red&color=blue#details';
  it('adds only the shared parameter, preserving query spelling and the fragment', () => {
    expect(shoppingDestination(original, [rule])).toBe(original.replace('#', '&tag=store-20#'));
  });
  it('preserves existing attribution by default, including encoded and duplicate keys', () => {
    const tagged = original.replace('a=%20', 't%61g=alice&tag=bob&a=%20');
    expect(shoppingDestination(tagged, [rule])).toBe(tagged);
    expect(shoppingDestination(tagged, [{ ...rule, replace: true }])).toBe(
      original.replace('#', '&tag=store-20#')
    );
  });
  it('requires an enabled rule for the exact HTTPS host', () => {
    expect(shoppingDestination(original, [])).toBe(original);
    for (const url of [
      'http://www.shop.test/x',
      'https://www.shop.test.evil.test/x',
      'https://www.shop.test@evil.test',
      'https://user@www.shop.test/x',
      'https://www.shop.test:8888/x',
      '/product'
    ]) {
      expect(shoppingDestination(url, [rule])).toBe(url);
    }
  });
});
