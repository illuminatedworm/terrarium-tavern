// Only this trusted runner executes in Node. Model code executes inside QuickJS/WASM.
import { readFileSync } from 'node:fs';
import '../static/calculation/engine.js';

const input = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(await globalThis.tavernCalculate(input)));
