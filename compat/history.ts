/** Immutable history transport, local copy-on-read records and isolated writes.
 * Providers are private to the sandbox; no host object is exposed to card code.
 */
export type HistorySource = {
  length: number;
  read: (index: number) => any;
  variables: (index: number) => Record<string, any>;
};

export function lazyHistory(source: HistorySource) {
  const aggregates = new Map<number, Record<string, any>>();
  const cachedSource = {
    ...source,
    variables(index: number) {
      if (!aggregates.has(index)) aggregates.set(index, source.variables(index));
      return aggregates.get(index)!;
    },
  };
  const loaded = new Map<number, any>();
  const target = new Array(source.length);
  const messages = new Proxy(target, {
    get(array, key, receiver) {
      if (typeof key === 'string' && /^(0|[1-9]\d*)$/.test(key)) {
        const index = Number(key);
        if (index >= source.length) return undefined;
        if (!loaded.has(index)) loaded.set(index, source.read(index));
        return loaded.get(index);
      }
      return Reflect.get(array, key, receiver);
    },
    has(array, key) {
      if (typeof key === 'string' && /^(0|[1-9]\d*)$/.test(key)) return Number(key) < source.length;
      return Reflect.has(array, key);
    },
  });
  return { messages, loaded, source: cachedSource, dirty: false };
}
