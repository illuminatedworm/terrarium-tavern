import { rawApi } from './api';
import type { Spec } from '@json-render/core';
import type { AgentActivity } from './agentActivity';

export function hasVisuals(items: readonly AgentActivity[] = [], kemini = false) {
  return !kemini && items.some((item) => item.name === 'present_ui' && item.status === 'returned');
}

/** Preserve the complete unsent draft, including its whitespace. */
export function appendVisualInput(current: string, text: string) {
  return current ? `${current}${current.endsWith('\n') ? '' : '\n'}${text}` : text;
}

export interface VisualSpec extends Spec {
  state: { selection: string; detail: string };
}
export type Paint = 'none' | 'surface' | 'muted' | 'ink' | 'accent' | 'secondary';
interface Shape {
  showFor?: string[] | null;
}
export type Primitive = Shape &
  (
    | { kind: 'rect'; x: number; y: number; width: number; height: number; fill: Paint; stroke: Paint }
    | { kind: 'ellipse'; x: number; y: number; rx: number; ry: number; fill: Paint; stroke: Paint }
    | { kind: 'line'; x1: number; y1: number; x2: number; y2: number; stroke: Paint; dashed?: boolean }
    | {
        kind: 'path';
        points: [number, number][];
        closed: boolean;
        fill: Paint;
        stroke: Paint;
        dashed?: boolean;
      }
    | { kind: 'label'; x: number; y: number; text: string; anchor: 'start' | 'middle' | 'end' }
  );
export interface SceneProps {
  title: string;
  height: number;
  selection: string;
  items: Primitive[];
}

interface Waiting {
  resolve: (specs: VisualSpec[]) => void;
  reject: (error: unknown) => void;
}
const batches = new Map<string, Map<string, Waiting[]>>();
const visible = new Map<Element, () => void>();
let observer: IntersectionObserver | undefined;

/** One observer delivers the viewport's graphics in a single callback. */
export function observeVisuals(element: Element, load: () => void) {
  if (!observer) {
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const callback = visible.get(entry.target);
          visible.delete(entry.target);
          observer?.unobserve(entry.target);
          callback?.();
        }
        if (!visible.size) {
          observer?.disconnect();
          observer = undefined;
        }
      },
      { rootMargin: '300px' },
    );
  }
  visible.set(element, load);
  observer.observe(element);
  return () => {
    visible.delete(element);
    observer?.unobserve(element);
    if (!visible.size) {
      observer?.disconnect();
      observer = undefined;
    }
  };
}

/** Visible message components share a request; no unbounded historical spec cache. */
export function loadVisuals(session: string, message: string): Promise<VisualSpec[]> {
  return new Promise((resolve, reject) => {
    let batch = batches.get(session);
    if (!batch) {
      batch = new Map();
      batches.set(session, batch);
      // Microtasks run between observer callbacks. Flush on the next frame so
      // separately delivered visibility notifications join the same request.
      requestAnimationFrame(() => void flush(session));
    }
    const waiting = batch.get(message) || [];
    waiting.push({ resolve, reject });
    batch.set(message, waiting);
  });
}
async function flush(session: string) {
  const batch = batches.get(session)!;
  batches.delete(session);
  const ids = [...batch.keys()];
  for (let offset = 0; offset < ids.length; offset += 100) {
    const part = ids.slice(offset, offset + 100);
    try {
      const result = await rawApi(`/sessions/${session}/full-agent/visuals`, { message_ids: part });
      for (const id of part) for (const waiting of batch.get(id)!) waiting.resolve(result.messages[id]);
    } catch (error) {
      for (const id of part) for (const waiting of batch.get(id)!) waiting.reject(error);
    }
  }
}
