import { DiceRoller } from 'dice-roller-parser';

// The upstream grammar handles Roll20 notation. Evaluate its tree here so the
// receipt retains original faces even for replaced, penetrating and compounded dice.
const parser = new DiceRoller();
const arithmetic = {
  '+': (a, b) => a + b,
  '-': (a, b) => a - b,
  '*': (a, b) => a * b,
  '/': (a, b) => a / b,
  '%': (a, b) => a % b,
  '**': (a, b) => a ** b,
};

function compare(value, op, target) {
  if (op === '>') return value >= target;
  if (op === '<') return value <= target;
  return value === target;
}

function integer(value, min, max, name) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer in ${min}..${max} inclusive`);
  }
  return value;
}

function parseFormula(formula) {
  // The grammar only has integer atoms. Let it parse signs, subtraction and
  // precedence itself, then restore decimals once in the cached syntax tree.
  // Its implicit numeric literals (including d%) are at most 100.
  const decimals = new Map();
  let token = 101;
  const notation = formula.toLowerCase().replace(/([<>])=/g, '$1');
  const tree = parser.parse(
    notation.replace(/\d*\.\d+(?:\.\d+)*/g, (literal) => {
      const value = Number(literal);
      if (!Number.isFinite(value)) throw new Error(`Invalid decimal: ${literal}`);
      while (notation.includes(String(token))) token++;
      decimals.set(token, value);
      return String(token++);
    }),
  );
  if (tree.label) throw new Error(`Unrecognized dice suffix in: ${formula}`);
  function restore(node) {
    if (node.type === 'number') {
      const value = decimals.get(Math.abs(node.value));
      if (value !== undefined) node.value = Math.sign(node.value) * value;
    } else {
      for (const child of Object.values(node)) if (child && typeof child === 'object') restore(child);
    }
  }
  if (decimals.size) restore(tree);
  return tree;
}

function keepOrDrop(items, mod, count) {
  const keep = mod.type === 'keep';
  const high = mod.highlow ? mod.highlow === 'h' : keep;
  const ranked = items
    .filter((item) => !item.dropped)
    .sort((a, b) => (high ? b.value - a.value : a.value - b.value));
  for (const item of keep ? ranked.slice(count) : ranked.slice(0, count)) item.dropped = true;
}

function markMatches(items, min, accepts) {
  const frequencies = new Map();
  for (const { value, dropped } of items) {
    if (!dropped && accepts(value)) frequencies.set(value, (frequencies.get(value) || 0) + 1);
  }
  let matches = 0;
  for (const count of frequencies.values()) if (count >= min) matches++;
  for (const item of items) if (!item.dropped && frequencies.get(item.value) >= min) item.matched = true;
  return matches;
}

function isReroll(mod) {
  return mod.type === 'reroll' || mod.type === 'rerollOnce';
}

function rerollFace(face, item, rules, draw) {
  let usedOnce = false;
  while (rules.length) {
    const rule = rules.find((r) => (!r.once || !usedOnce) && compare(face, r.op, r.target));
    if (!rule) break;
    (item.rerolled ??= []).push(face);
    usedOnce ||= rule.once;
    face = draw();
  }
  return face;
}

function rerollItem(item, settle) {
  // All matching and history use physical faces. Apply the penetration penalty
  // to the retained face and rebuild a compounded die from its constituents.
  const first = settle(item.face ?? item.faces?.[0] ?? item.value, item);
  item.value = first;
  if (item.face !== undefined) {
    item.face = first;
    item.value--;
  }
  if (item.faces) {
    item.faces[0] = first;
    for (let i = 1; i < item.faces.length; i++) {
      item.faces[i] = settle(item.faces[i], item);
      item.value += item.faces[i];
    }
  }
}

function explode(items, mod, target, drawFace) {
  const extra = [];
  for (const item of items) {
    let face = item.face ?? item.value;
    while (compare(face, mod.target?.mod, target)) {
      const die = mod.type === 'compound' ? item : { extra: true };
      const next = drawFace(die);
      if (mod.type === 'compound') {
        (item.faces ??= [face]).push(next);
        item.value += next;
      } else {
        die.value = next;
        if (mod.type === 'penetrate') {
          die.face = next;
          die.value--;
        }
        extra.push(die);
      }
      // Penetration subtracts from the score, not the face used to trigger the next explosion.
      face = next;
    }
  }
  for (const item of extra) items.push(item);
}

export function diceSession(summary, random = Math.random) {
  const drawLimit = summary ? 20000 : 256;
  const formulas = new Map();
  let draws = 0;

  return function roll(formula) {
    if (typeof formula !== 'string' || !formula.trim() || formula.length > 256) {
      throw new Error('roll() needs a dice expression of 1..256 UTF-16 code units');
    }
    let tree = formulas.get(formula);
    if (!tree) {
      tree = parseFormula(formula);
      if (formulas.size < 64) formulas.set(formula, tree);
    }
    const pools = [];
    function draw(sides) {
      if (++draws > drawLimit) throw new Error(`Calculation exceeds ${drawLimit} individual dice`);
      return sides === 'F' ? Math.floor(random() * 3) - 1 : Math.floor(random() * sides) + 1;
    }
    function evaluate(node) {
      switch (node.type) {
        case 'number':
          return Number(node.value);
        case 'inline':
          return evaluate(node.expr);
        case 'mathfunction':
          return Math[node.op](evaluate(node.expr));
        case 'expression':
        case 'diceExpression':
          return node.ops.reduce(
            (n, operation) => arithmetic[operation.op](n, evaluate(operation.tail)),
            evaluate(node.head),
          );
        case 'die':
        case 'group':
          return pool(node);
        default:
          throw new Error(`Unsupported dice expression: ${node.type}`);
      }
    }
    // A single additive member applies group modifiers to the individual dice.
    // Comma-separated members each contribute one subtotal (Roll20 group rules).
    function collectMember(node, collectDie, sign = 1) {
      if (node.type === 'die') {
        collectDie(node, sign);
        return 0;
      }
      if (
        (node.type === 'expression' || node.type === 'diceExpression') &&
        node.ops.every((o) => o.op === '+' || o.op === '-')
      ) {
        let constant = collectMember(node.head, collectDie, sign);
        for (const op of node.ops)
          constant += collectMember(op.tail, collectDie, op.op === '-' ? -sign : sign);
        return constant;
      }
      return sign * evaluate(node);
    }
    function pool(node, collect) {
      const group = node.type === 'group';
      const items = [];
      let sides;
      let adjustment = 0;
      if (group) {
        if (node.rolls.length === 1 && node.mods?.length) {
          let hasDice = false;
          const constant = collectMember(node.rolls[0], (die, sign) => {
            hasDice = true;
            pool(die, (value) => items.push({ value: sign * value }));
          });
          if (!hasDice) items.push({ value: constant });
          else if (node.mods.some((mod) => mod.type === 'success')) {
            for (const item of items) item.value += constant;
          } else adjustment = constant;
        } else {
          for (const member of node.rolls) items.push({ value: evaluate(member) });
        }
      } else {
        const count = integer(Math.round(evaluate(node.count)), 0, drawLimit, 'Dice count');
        sides = node.die.type === 'fate' ? 'F' : integer(evaluate(node.die), 1, 1000000, 'Die sides');
        for (let i = 0; i < count; i++) items.push({ value: draw(sides) });
      }
      const targets = [];
      let rerolls = [];
      const drawRaw = () => draw(sides);
      const settle = (face, item) => rerollFace(face, item, rerolls, drawRaw);
      for (const mod of node.mods || []) {
        if (mod.type === 'keep' || mod.type === 'drop') {
          const count = integer(evaluate(mod.expr), 0, drawLimit, 'Keep/drop count');
          keepOrDrop(items, mod, count);
        } else if (mod.type === 'success' || mod.type === 'failure') {
          targets.push(mod);
        } else if (mod.type === 'crit' || mod.type === 'critfail') {
          const target = evaluate(mod.expr);
          for (const item of items) {
            if (compare(item.value, mod.mod, target)) item[mod.type] = true;
          }
        } else if (isReroll(mod)) {
          // One set of comparisons per physical die, including additional dice
          // from later explosions. A replacement face can match an earlier rule.
          if (rerolls.length) continue;
          rerolls = node.mods.filter(isReroll).map((r) => ({
            once: r.type === 'rerollOnce',
            op: r.target?.mod,
            target: r.target ? evaluate(r.target.value) : 1,
          }));
          for (const item of items) rerollItem(item, settle);
        } else {
          if (group) throw new Error(`Unsupported group modifier: ${mod.type}`);
          const target = mod.target ? evaluate(mod.target.value) : sides === 'F' ? 1 : sides;
          explode(items, mod, target, (die) => settle(drawRaw(), die));
        }
      }
      if (node.sort) items.sort((a, b) => (node.sort.asc ? a.value - b.value : b.value - a.value));
      let total = adjustment;
      const thresholds = [...targets, ...(node.targets || [])].map((t) => ({
        ...t,
        target: evaluate(t.expr),
      }));
      if (thresholds.length && !thresholds.some((t) => t.type === 'success')) {
        throw new Error('A failure threshold needs a success threshold, e.g. 8d10>8f1');
      }
      for (const item of items) {
        if (item.dropped) continue;
        if (thresholds.length) {
          item.score = thresholds.reduce(
            (score, t) =>
              score + (compare(item.value, t.mod, t.target) ? (t.type === 'failure' ? -1 : 1) : 0),
            0,
          );
        }
        total += item.score ?? item.value;
      }
      if (node.match) {
        const min = evaluate(node.match.min);
        const target = node.match.expr && evaluate(node.match.expr);
        const matches = markMatches(
          items,
          min,
          (value) => !node.match.expr || compare(value, node.match.mod, target),
        );
        if (node.match.count) total = matches;
      }
      if (collect) {
        if (node.match?.count) collect(total);
        else for (const item of items) if (!item.dropped) collect(item.score ?? item.value);
      }
      pools.push(group ? { members: items, total } : { sides, dice: items, total });
      return total;
    }
    const total = evaluate(tree);
    if (!Number.isFinite(total)) throw new Error('Dice result must be a finite number');
    return { formula, total, pools };
  };
}
