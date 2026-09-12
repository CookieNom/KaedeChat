import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { IntlMessageFormat } from 'intl-messageformat';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const required = [
  'language_name',
  'language_suggestion',
  'language_accept',
  'language_decline',
  'language_save_error'
];
for (const [directory, extension, prefix] of [
  ['frontend/src/lib/locales', '.json', ''],
  ['mobile/lib/l10n', '.arb', 'app_']
]) {
  const folder = path.join(root, directory);
  const source = JSON.parse(fs.readFileSync(path.join(folder, `${prefix}en${extension}`), 'utf8'));
  for (const file of fs.readdirSync(folder).filter((name) => name.endsWith(extension))) {
    const locale = file.slice(prefix.length, -extension.length).replaceAll('_', '-');
    const messages = JSON.parse(fs.readFileSync(path.join(folder, file), 'utf8'));
    if (extension === '.arb' && messages['@@locale']?.replaceAll('_', '-') !== locale)
      throw Error(`${file}: @@locale must match filename`);
    for (const key of required) {
      if (typeof messages[key] !== 'string' || !messages[key].trim())
        throw Error(`${file}: translate ${key} before enabling this language`);
    }
    for (const [key, message] of Object.entries(messages)) {
      if (key.startsWith('@')) continue;
      if (!(key in source)) throw Error(`${file}: unknown source key ${key}`);
      if (typeof message !== 'string' || !message.trim())
        throw Error(`${file}:${key}: omit untranslated entries instead of storing empty text`);
      new IntlMessageFormat(message, locale, undefined, { ignoreTag: true });
      const expected = parameters(source[key]);
      const actual = parameters(message);
      if (expected.size !== actual.size || [...expected].some((name) => !actual.has(name)))
        throw Error(`${file}:${key}: placeholder names differ from English`);
      if (extension === '.arb' && locale === 'en') {
        const metadata = messages[`@${key}`]?.placeholders ?? {};
        for (const name of expected)
          if (!metadata[name])
            throw Error(`${file}:${key}: missing placeholder metadata for ${name}`);
      }
    }
    console.log(
      `${directory}/${file}: ${Object.keys(messages).filter((key) => !key.startsWith('@')).length} messages validated`
    );
  }
}
function parameters(message) {
  const names = new Set();
  const visit = (nodes) => {
    for (const node of nodes) {
      if ([1, 2, 3, 4, 5, 6].includes(node.type)) names.add(node.value);
      if (node.options) for (const option of Object.values(node.options)) visit(option.value);
      if (node.children) visit(node.children);
    }
  };
  visit(new IntlMessageFormat(message, 'en', undefined, { ignoreTag: true }).getAst());
  return names;
}
