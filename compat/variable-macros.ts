import isPlainObject from 'lodash/isPlainObject';
import toPath from 'lodash/toPath';
import unescape from 'lodash/unescape';
import { stringify } from 'yaml';

export type VariableScope = 'message' | 'chat' | 'character' | 'preset' | 'global';
export type VariableReader = (scope: VariableScope) => Record<string, any>;

function publicValue(value: any): any {
  if (Array.isArray(value)) return value.map(publicValue);
  if (isPlainObject(value))
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !key.startsWith('$'))
        .map(([key, item]) => [key, publicValue(item)]),
    );
  return value ?? null;
}

/** Helper macros read complete values. A two-element array is never implicitly unwrapped. */
export function variableValue(read: VariableReader, scope: VariableScope, path: string): any {
  const keys = toPath(unescape(path));
  if (!keys.length) return null;
  if (keys.some((key) => key.startsWith('$') || ['__proto__', 'prototype', 'constructor'].includes(key)))
    return null;
  let value: any = read(scope);
  for (const key of keys) {
    if (value === null || typeof value !== 'object' || !Object.hasOwn(value, key)) return null;
    value = value[key];
  }
  return publicValue(value);
}

export function replaceVariableMacros(text: string, read: VariableReader): string {
  const regex = /\{\{(get|format)_(message|chat|character|preset|global)_variable(_quoted)?::(.*?)\}\}/gi;
  let result = '',
    cursor = 0;
  for (const match of text.matchAll(regex)) {
    result += text.slice(cursor, match.index);
    const value = variableValue(read, match[2].toLowerCase() as VariableScope, match[4]);
    const format = match[1].toLowerCase() === 'format';
    let replacement =
      typeof value === 'string'
        ? value
        : format
          ? stringify(value, {
              blockQuote: 'literal',
              ...(match[3] ? ({ defaultKeyType: 'PLAIN', defaultStringType: 'QUOTE_DOUBLE' } as const) : {}),
            }).trimEnd()
          : JSON.stringify(value);
    if (format)
      replacement = replacement.replaceAll(
        '\n',
        '\n' + ' '.repeat(result.length - result.lastIndexOf('\n') - 1),
      );
    result += replacement;
    cursor = match.index! + match[0].length;
  }
  return result + text.slice(cursor);
}

export function snapshotMacros(text: string, state: any, scopes: any = {}): string {
  return replaceVariableMacros(text, (type) =>
    type === 'message' || type === 'chat' ? state || {} : scopes[type] || {},
  );
}
