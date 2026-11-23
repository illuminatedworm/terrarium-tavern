import variant from '@jitl/quickjs-singlefile-browser-release-sync';
import { newQuickJSWASMModuleFromVariant, Scope } from 'quickjs-emscripten-core';
import { diceSession } from './dice.js';

const outputLimit = 32768;
const utf8 = new TextEncoder();

function resultSize(data) {
  const depths = new WeakMap();
  const unpaired = /[\uD800-\uDFFF]/u;
  const json = JSON.stringify(data, function (key, item) {
    if (unpaired.test(key) || (typeof item === 'string' && unpaired.test(item)))
      throw new Error('Return valid Unicode text');
    if (item && typeof item === 'object') {
      const depth = (depths.get(this) || 0) + 1;
      if (depth > 64) throw new Error('JSON result exceeds 64 levels');
      depths.set(item, depth);
    }
    return item;
  });
  return utf8.encode(json).length;
}

// This function is installed inside QuickJS. Only JSON crosses the boundary;
// the parser, random source and authoritative dice receipts stay in the host.
function program(source, rollJSON) {
  const parse = JSON.parse;
  const stringify = JSON.stringify;
  const roll = (formula) => parse(rollJSON(formula));
  let evaluate;
  try {
    evaluate = new Function('roll', '"use strict"; return (\n' + source.trim().replace(/;$/, '') + '\n);');
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    evaluate = new Function('roll', '"use strict";\n' + source);
  }
  // Compile before execution so a user-thrown exception never repeats a roll.
  const value = evaluate(roll);
  return stringify(value, function (key, item) {
    if (item === null || typeof item === 'string' || typeof item === 'boolean') return item;
    if (typeof item === 'number' && Number.isFinite(item)) return item;
    if (
      typeof item !== 'object' ||
      (!Array.isArray(item) &&
        Object.getPrototypeOf(item) !== Object.prototype &&
        Object.getPrototypeOf(item) !== null)
    ) {
      throw new Error('Return a JSON value with finite numbers. Use return in a multi-statement program.');
    }
    return item;
  });
}

export async function calculate({ code, summary = false }) {
  const engine = await newQuickJSWASMModuleFromVariant(variant);
  const deadline = performance.now() + 1200;
  const expired = () => performance.now() >= deadline;
  const roll = diceSession(summary);
  const receipts = [];
  let receiptSize = 0;
  try {
    return Scope.withScope((scope) => {
      const vm = scope.manage(engine.newContext());
      vm.runtime.setMemoryLimit(16 * 1024 * 1024);
      vm.runtime.setMaxStackSize(512 * 1024);
      vm.runtime.setInterruptHandler(expired);
      const entry = scope.manage(vm.unwrapResult(vm.evalCode('(' + program.toString() + ')')));
      const script = scope.manage(vm.newString(code));
      const dice = scope.manage(
        vm.newFunction('roll', (handle) => {
          if (vm.typeof(handle) !== 'string') throw new Error('roll() needs a dice expression string');
          if (expired()) throw new Error('Calculation time limit exceeded');
          const result = roll(vm.getString(handle));
          const json = JSON.stringify(result);
          if (!summary) {
            receiptSize += utf8.encode(json).length;
            if (receiptSize > outputLimit)
              throw new Error('Dice details exceed 32 KiB. Select summary before a simulation.');
            receipts.push(result);
          }
          return vm.newString(json);
        }),
      );
      const returned = scope.manage(vm.unwrapResult(vm.callFunction(entry, vm.undefined, script, dice)));
      const json = vm.getString(returned);
      if (json.length > outputLimit) throw new Error('Calculation result exceeds 32 KiB');
      const value = JSON.parse(json);
      const data = { value };
      // A bare roll() already returns its full receipt. Intermediate rolls are
      // kept separately, even if the program discards or edits its local copy.
      if (receipts.length && !(receipts.length === 1 && JSON.stringify(receipts[0]) === json)) {
        data.rolls = receipts;
      }
      if (receiptSize > outputLimit || resultSize(data) > outputLimit) {
        throw new Error(
          'Calculation result exceeds 32 KiB. Return a smaller value or select summary before a simulation.',
        );
      }
      if (expired()) throw new Error('Calculation time limit exceeded');
      return { data };
    });
  } catch (error) {
    // Return the guest's diagnostic, never a browser/Node stack or installation path.
    const message = expired() ? 'Calculation exceeded 1200 ms' : String(error?.message ?? error);
    return {
      error:
        message.length <= 512
          ? message.replace(/[\uD800-\uDFFF]/gu, '\uFFFD')
          : 'Calculation failed with an oversized error message',
    };
  }
}

globalThis.tavernCalculate = calculate;
