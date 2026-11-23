import fs from 'node:fs';
import { build } from 'esbuild';

fs.mkdirSync('src/tavern/static/esbuild', { recursive: true });
for (const [source, name] of [
  ['node_modules/esbuild-wasm/lib/browser.min.js', 'browser.min.js'],
  ['node_modules/esbuild-wasm/esbuild.wasm', 'esbuild.wasm'],
  ['compat/dependency-bundler.js', 'bundle.js'],
  ['node_modules/esbuild-wasm/LICENSE.md', 'LICENSE.md'],
]) {
  fs.copyFileSync(source, `src/tavern/static/esbuild/${name}`);
}

const calculation = 'src/tavern/static/calculation';
await build({
  entryPoints: ['compat/calculation/engine.js'],
  outfile: `${calculation}/engine.js`,
  bundle: true,
  minify: true,
  format: 'iife',
  platform: 'browser',
  target: 'es2022',
  charset: 'ascii',
});
for (const [pkg, file] of [
  ['@jitl/quickjs-singlefile-browser-release-sync', 'QuickJS'],
  ['quickjs-emscripten-core', 'QuickJSEmscripten'],
  ['dice-roller-parser', 'DiceRollerParser'],
]) {
  fs.copyFileSync(`node_modules/${pkg}/LICENSE`, `${calculation}/${file}-LICENSE.md`);
}
