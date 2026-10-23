import fs from 'node:fs';

fs.mkdirSync('src/tavern/static/esbuild', { recursive: true });
for (const [source, name] of [
  ['node_modules/esbuild-wasm/lib/browser.min.js', 'browser.min.js'],
  ['node_modules/esbuild-wasm/esbuild.wasm', 'esbuild.wasm'],
  ['compat/dependency-bundler.js', 'bundle.js'],
  ['node_modules/esbuild-wasm/LICENSE.md', 'LICENSE.md'],
]) {
  fs.copyFileSync(source, `src/tavern/static/esbuild/${name}`);
}
