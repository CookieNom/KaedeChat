export interface LinkRule {
  host: string;
  parameter: string;
  value: string;
  replace: boolean;
}

export function shoppingDestination(raw: string, rules: readonly LinkRule[]): string {
  if (!rules.length || !/^https:\/\//i.test(raw)) return raw;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return raw;
  }
  if (url.username || url.password || url.port || /[\s\\]/.test(raw)) return raw;
  const rule = rules.find((item) => item.host === url.hostname);
  if (!rule) return raw;
  const hash = raw.indexOf('#');
  const base = hash < 0 ? raw : raw.slice(0, hash);
  const fragment = hash < 0 ? '' : raw.slice(hash);
  const question = base.indexOf('?');
  const path = question < 0 ? base : base.slice(0, question);
  const query = question < 0 ? '' : base.slice(question + 1);
  const fields = query ? query.split('&') : [];
  const pair = encodeURIComponent(rule.parameter) + '=' + encodeURIComponent(rule.value);
  const kept = fields.filter((field) => {
    try {
      return decodeURIComponent(field.split('=', 1)[0].replace(/\+/g, ' ')) !== rule.parameter;
    } catch {
      return true;
    }
  });
  if (fields.length !== kept.length && !rule.replace) return raw;
  return path + '?' + [...kept, pair].join('&') + fragment;
}
