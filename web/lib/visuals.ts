import { rawApi } from './api';
import type { Spec } from '@json-render/core';
import type { AgentActivity } from './agentActivity';
import type { StoryContent } from './storyPresentation';

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
export interface VisualOutput {
  specs: VisualSpec[];
  order: Array<'story' | 'visual'>;
}
export type StoryOutputItem =
  | { key: string; kind: 'content'; content: StoryContent }
  | { key: string; kind: 'visual'; spec: VisualSpec };

/** Accepted calls place graphics; the current, possibly unfinished prose stays intact. */
export function draftStoryContents(segments: readonly string[], activities: readonly AgentActivity[]) {
  const items: Array<
    | { key: string; kind: 'story'; text: string; divider: boolean }
    | { key: string; kind: 'visual'; spec: VisualSpec }
  > = [];
  let story = 0;
  function appendStory() {
    if (story < segments.length) {
      items.push({ key: `story-${story}`, kind: 'story', text: segments[story], divider: story > 0 });
      story++;
    }
  }
  for (const item of activities) {
    if (item.status !== 'returned') continue;
    if (item.name === 'submit_story') appendStory();
    else if (item.visual) items.push({ key: item.id, kind: 'visual', spec: item.visual });
  }
  while (story < segments.length) appendStory();
  return items;
}

/** Keep body node identities stable when lazy graphics arrive between them. */
export function orderedStoryContents(contents: readonly StoryContent[], output: VisualOutput | null) {
  const body: StoryOutputItem[] = contents.map((content, index) => ({
    key: `content-${index}`,
    kind: 'content',
    content,
  }));
  if (!output?.specs.length) return { items: body, unaligned: false };
  const groups: StoryOutputItem[][] = [[]];
  for (const item of body) {
    if (item.kind === 'content' && item.content.segmentBreak) groups.push([]);
    groups[groups.length - 1].push(item);
  }
  const visuals: StoryOutputItem[] = output.specs.map((spec, index) => ({
    key: `visual-${index}`,
    kind: 'visual',
    spec,
  }));
  if (output.order.filter((kind) => kind === 'story').length !== groups.length) {
    // Whole-message regex/scripts can retire the original boundaries. Keep
    // their complete result; do not guess offsets or replay the raw tool text.
    return { items: [...body, ...visuals], unaligned: true };
  }
  let story = 0,
    visual = 0;
  return {
    items: output.order.flatMap((kind) => (kind === 'story' ? groups[story++] : [visuals[visual++]])),
    unaligned: false,
  };
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
  resolve: (output: VisualOutput) => void;
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
export function loadVisuals(session: string, message: string): Promise<VisualOutput> {
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
