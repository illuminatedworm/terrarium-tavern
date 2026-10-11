import { visibleLoreData } from './loreRecords';
import { parseStoryDisplay } from '../../compat/presentation';
import type { StoryPresentation } from '../../compat/presentation';
export type { StoryContent, StoryPresentation } from '../../compat/presentation';

/** Normalize imported display formats once, at the response-data boundary.
 * An extracted HTML document must never go through Markdown a second time.
 * raw/model/display remain lossless source fields for editing and export.
 */
export function createPresentationCache(limit = 16 * 1024 * 1024, entries = 128) {
  const cache = new Map<string, { value: StoryPresentation; size: number }>();
  let size = 0;
  return (text: string, role = 'assistant') => {
    const key = role + '\0' + text;
    const hit = cache.get(key);
    if (hit) {
      cache.delete(key);
      cache.set(key, hit);
      return hit.value;
    }
    const value = parseStoryDisplay(text, role);
    for (const part of [...value.body, ...value.panel]) Object.freeze(part);
    Object.freeze(value.body);
    Object.freeze(value.panel);
    Object.freeze(value);
    const cost =
      2 * (key.length + [...value.body, ...value.panel].reduce((n, part) => n + part.text.length, 0));
    if (cost <= limit) {
      cache.set(key, { value, size: cost });
      size += cost;
      while (size > limit || cache.size > entries) {
        const oldest = cache.keys().next().value!;
        size -= cache.get(oldest)!.size;
        cache.delete(oldest);
      }
    }
    return value;
  };
}
export const storyPresentation = createPresentationCache();

export function responseData<T>(value: T): T {
  const result = visibleLoreData(value);
  function visit(item: any): any {
    if (!item || typeof item !== 'object') return item;
    if (Array.isArray(item)) {
      const next = item.map(visit);
      return next.some((child, index) => child !== item[index]) ? next : item;
    }
    let result = item;
    if (typeof item.raw === 'string' && typeof item.display === 'string' && item.snapshot && item.role) {
      result = { ...item, presentation: storyPresentation(item.display, item.role) };
    }
    for (const [key, child] of Object.entries(item)) {
      // Card content and state are user data, not message envelopes.
      if (!['snapshot', 'document', 'card_data', 'state', 'scopes', 'presentation'].includes(key)) {
        const next = visit(child);
        if (next !== child) {
          if (result === item) result = { ...item };
          result[key] = next;
        }
      }
    }
    return result;
  }
  return visit(result);
}
