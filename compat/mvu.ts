import _ from 'lodash';
import JSON5 from 'json5';
import YAML from 'yaml';
import { parse } from 'acorn';
import patch from 'fast-json-patch';

export type Data = Record<string, any>;
export const events = {
  VARIABLE_INITIALIZED: 'mag_variable_initialized',
  VARIABLE_UPDATE_STARTED: 'mag_variable_update_started',
  COMMAND_PARSED: 'mag_command_parsed',
  VARIABLE_UPDATE_ENDED: 'mag_variable_update_ended',
  BEFORE_MESSAGE_UPDATE: 'mag_before_message_update',
  SINGLE_VARIABLE_UPDATED: 'mag_variable_updated',
};
export function safePath(path: string) {
  const parts = _.toPath(path);
  if (parts.some(p => ['__proto__', 'prototype', 'constructor'].includes(p))) throw Error('禁止的变量路径');
  return path;
}
function literal(node: any): any {
  if (node.type === 'Literal') return node.value;
  if (node.type === 'Identifier' && node.name === 'undefined') return null;
  if (node.type === 'ArrayExpression') return node.elements.map(literal);
  if (node.type === 'ObjectExpression') return Object.fromEntries(node.properties.map((p: any) => {
    if (p.type !== 'Property' || p.computed || p.method) throw Error('不支持的变量字面量');
    return [safePath(p.key.name ?? p.key.value), literal(p.value)];
  }));
  if (node.type === 'UnaryExpression' && ['+', '-'].includes(node.operator)) return node.operator === '-' ? -literal(node.argument) : +literal(node.argument);
  if (node.type === 'BinaryExpression' && ['+', '-', '*', '/', '%', '**'].includes(node.operator)) {
    const a = literal(node.left), b = literal(node.right);
    if (typeof a !== 'number' || typeof b !== 'number') throw Error('仅支持数值表达式');
    return ({'+': () => a+b, '-': () => a-b, '*': () => a*b, '/': () => a/b, '%': () => a%b, '**': () => a**b} as any)[node.operator]();
  }
  throw Error('变量命令只允许数据字面量和纯数值运算');
}

export function commands(text: string) {
  const result: any[] = [];
  // Parse only recognized update calls, never evaluate model-generated JavaScript.
  const start = /_\.(set|insert|delete|add|move)\s*\(/g;
  for (const match of text.matchAll(start)) {
    const offset = match.index!;
    let depth = 1, quote = '', escape = false, end = offset + match[0].length;
    for (; end < text.length && depth; end++) {
      const c = text[end];
      if (quote) { if (escape) escape = false; else if (c === '\\') escape = true; else if (c === quote) quote = ''; }
      else if ('\'"`'.includes(c)) quote = c;
      else if (c === '(') depth++;
      else if (c === ')') depth--;
    }
    if (depth) throw Error('不完整的 MVU 命令');
    const raw = text.slice(offset, end);
    const ast: any = parse(raw, {ecmaVersion: 2022});
    const call = ast.body[0]?.expression;
    if (call?.type !== 'CallExpression') throw Error('无效 MVU 命令');
    const args = call.arguments.map((a: any) => raw.slice(a.start, a.end));
    result.push({type: match[1], full_match: raw, args, reason: ''});
  }
  return result;
}
function value(raw: any) {
  if (typeof raw !== 'string') return raw;
  try { return JSON5.parse(raw); } catch { /* numeric expressions are parsed, never evaluated */ }
  const ast: any = parse(`(${raw})`, {ecmaVersion: 2022});
  return literal(ast.body[0].expression);
}
function validate(node: any, val: any, path = ''): any {
  if (!node) return val;
  if (node.type === 'object') {
    if (!_.isPlainObject(val)) throw Error(`变量 ${path} 需要对象`);
    for (const [k, v] of Object.entries(val)) {
      safePath(k);
      if (!(k in (node.properties || {})) && node.extensible === false) throw Error(`变量 ${path}.${k} 不允许新增`);
      val[k] = validate(node.properties?.[k], v, `${path}.${k}`);
    }
    for (const [k, child] of Object.entries<any>(node.properties || {})) if (child.required && !(k in val)) throw Error(`缺少变量 ${path}.${k}`);
  } else if (node.type === 'array') {
    if (!Array.isArray(val)) throw Error(`变量 ${path} 需要数组`);
    // Upstream represents legacy [value, description] with the value's element
    // schema. Validate the value but preserve the description as a string;
    // checking it as a number incorrectly rejects every JSON Patch to that card.
    val = val.length === 2 && typeof val[1] === 'string'
      ? [validate(node.elementType, val[0], `${path}.0`), val[1]]
      : val.map((v, i) => validate(node.elementType, v, `${path}.${i}`));
  } else if (node.type !== 'any' && typeof val !== node.type) {
    if (node.type === 'number' && typeof val === 'string' && val.trim() && Number.isFinite(Number(val))) return Number(val);
    throw Error(`变量 ${path} 类型不匹配`);
  }
  return val;
}
function schemaFor(data: any, inherited = true): any {
  if (Array.isArray(data)) return {type: 'array', elementType: data.length ? schemaFor(data[0], inherited) : {type: 'any'}, extensible: inherited};
  if (_.isPlainObject(data)) {
    const meta = data.$meta || {};
    const extensible = meta.extensible ?? meta.recursiveExtensible ?? inherited;
    return {type: 'object', extensible, template: meta.template, properties: Object.fromEntries(Object.entries(data).filter(([k]) => !k.startsWith('$')).map(([k,v]) => [k, {...schemaFor(v, meta.recursiveExtensible ?? inherited), required: meta.required?.includes(k) || false}]))};
  }
  return {type: data === null ? 'any' : typeof data};
}
function clean(data: any): any {
  if (Array.isArray(data)) return data.filter(v => v !== '$__META_EXTENSIBLE__$').map(clean);
  if (_.isPlainObject(data)) return Object.fromEntries(Object.entries(data).filter(([k]) => !['$meta','$arrayMeta'].includes(k)).map(([k,v]) => [safePath(k), clean(v)]));
  return data;
}
function parseInitialData(content: string) {
  const wrapped = content.trim().match(/^<initvar\b[^>]*>([\s\S]*?)<\/initvar\s*>$/i);
  const text = (wrapped?.[1] ?? content).trim();
  const codeblock = text.match(/^```[^\r\n]*\r?\n([\s\S]*?)\r?\n```$/);
  const initial = YAML.parse(codeblock?.[1] ?? text);
  if (!_.isPlainObject(initial)) throw Error('InitVar 必须为对象');
  safePath(JSON.stringify(initial).includes('__proto__') ? '__proto__' : 'ok');
  return initial;
}
function mergeInitialData(target: Data, initial: Data) {
  // Match upstream correctlyMerge for fragments in the same worldbook/opening.
  return _.mergeWith(target, initial, (_old, value) => Array.isArray(value) ? value : undefined);
}
export async function initialize(entries: any[], old: Data, emit: (name: string, ...args: any[]) => Promise<void>, greeting = '') {
  const data = _.cloneDeep(old);
  data.stat_data ||= {};
  data.initialized_lorebooks ||= {};
  let changed = false;
  // The InitVar marker controls initialization; injection switches and audiences
  // only control prompt delivery, so Tracker-marked defaults remain readable.
  entries = entries.filter(e => /\[initvar\]/i.test(e.comment || e.name || ''));
  const opening = Array.from(greeting.matchAll(/<initvar\b[^>]*>([\s\S]*?)<\/initvar\s*>/gi));
  const openingInitialized = _.has(data.initialized_lorebooks, 'card:opening:0');
  if (opening.length || openingInitialized) {
    if (!openingInitialized) {
      // A complete opening replaces this card's defaults; keep the marker on
      // later runtime opens so omitted fields never return from the worldbook.
      data.stat_data = opening.reduce((initial, match) => mergeInitialData(initial, parseInitialData(match[1])), {});
      opening.forEach((match, index) => {data.initialized_lorebooks[`card:opening:${index}`] = [match[1]];});
      for (const entry of entries) data.initialized_lorebooks[`card:${entry.id}`] = [entry.content];
      changed = true;
    }
  } else {
    const initial: Data = {};
    for (const entry of entries) {
      const key = `card:${entry.id}`;
      if (data.initialized_lorebooks[key]) continue;
      mergeInitialData(initial, parseInitialData(entry.content));
      data.initialized_lorebooks[key] = [entry.content];
      changed = true;
    }
    // Upstream preserves existing root values as complete objects/arrays.
    if (changed) data.stat_data = {...initial, ...data.stat_data};
  }
  const core=(globalThis as any).MVUReference;
  if (changed || !data.schema) data.schema = core ? core.generateSchema(_.cloneDeep(data.stat_data),data.schema) : schemaFor(data.stat_data);
  if(core)core.cleanUpMetadata(data.stat_data);
  else data.stat_data = clean(data.stat_data);
  if (changed) await emit(events.VARIABLE_INITIALIZED, data, 0);
  return data;
}

export async function apply(text: string, old: Data, emit: (name: string, ...args: any[]) => Promise<void>, extra: any[] = []) {
  const data = _.cloneDeep(old);
  data.stat_data ||= {};
  await emit(events.VARIABLE_UPDATE_STARTED, data);
  const parsed = commands(text);
  await emit(events.COMMAND_PARSED, data, parsed, text);
  const stat = data.stat_data;
  for (const command of parsed) {
    const args = command.args.map(value);
    const path = safePath(String(args[0]));
    const previous = _.get(stat, path);
    const vwd = Array.isArray(previous) && previous.length === 2 && typeof previous[1] === 'string' && !Array.isArray(args.at(-1));
    const actual = vwd ? `${path}[0]` : path;
    if (command.type === 'set') {
      if (args.length === 3 && !_.isEqual(_.get(stat, actual), args[1])) throw Error(`MVU 旧值不匹配：${path}`);
      _.set(stat, actual, args.at(-1));
    } else if (command.type === 'add') {
      const prev = _.get(stat, actual);
      if (typeof prev === 'boolean') _.set(stat, actual, !prev);
      else if (typeof prev === 'number' && Number.isFinite(Number(args[1]))) _.set(stat, actual, prev + Number(args[1]));
      else throw Error(`MVU add 需要数值：${path}`);
    } else if (command.type === 'delete') {
      if (args.length === 1) _.unset(stat, path);
      else if (Array.isArray(previous)) previous.splice(typeof args[1] === 'number' ? args[1] : previous.findIndex(v => _.isEqual(v,args[1])), 1);
      else _.unset(previous, safePath(String(args[1])));
    } else if (command.type === 'insert') {
      if (Array.isArray(previous)) previous.splice(args.length === 3 ? Number(args[1]) : previous.length, 0, args.at(-1));
      else if (_.isPlainObject(previous) && args.length === 3) _.set(previous, safePath(String(args[1])), args[2]);
      else if (args.length === 2 && previous === undefined) _.set(stat, path, args[1]);
      else throw Error(`MVU insert 目标无效：${path}`);
    } else if (command.type === 'move') {
      const to = safePath(String(args[1]));
      _.set(stat, to, _.cloneDeep(previous)); _.unset(stat, path);
    }
  }
  const tagged = text.match(/<JSONPatch>\s*([\s\S]*?)\s*<\/JSONPatch>/i);
  const operations = [...(tagged ? JSON5.parse(tagged[1]) : []), ...extra];
  if (operations.length) data.stat_data = patch.applyPatch(stat, operations, true, false, true).newDocument;
  await emit(events.VARIABLE_UPDATE_ENDED, data, _.cloneDeep(old));
  const context = {variables: data, message_content: text};
  await emit(events.BEFORE_MESSAGE_UPDATE, context);
  context.variables.stat_data = validate(context.variables.schema, context.variables.stat_data);
  if (!JSON.stringify(context.variables)) throw Error('MVU 状态不可序列化');
  return {data: context.variables, text: context.message_content};
}
