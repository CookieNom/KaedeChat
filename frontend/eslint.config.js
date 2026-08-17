import js from '@eslint/js';
import globals from 'globals';
import svelte from 'eslint-plugin-svelte';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...svelte.configs['flat/recommended'],
  {
    languageOptions: { globals: { ...globals.browser, ...globals.node } }
  },
  {
    files: ['**/*.svelte'],
    languageOptions: {
      parserOptions: { parser: tseslint.parser }
    }
  },
  {
    files: ['**/*.svelte.ts'],
    languageOptions: { parser: tseslint.parser }
  },
  {
    ignores: [
      'build/',
      '.svelte-kit/',
      'src/lib/generated/',
      'src/lib/e2ee/wasm/kaede_e2ee.js',
      'src/lib/e2ee/wasm/kaede_e2ee_bg.wasm.d.ts'
    ]
  }
);
