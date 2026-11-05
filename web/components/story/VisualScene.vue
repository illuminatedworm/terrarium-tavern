<script setup lang="ts">
import { h } from 'vue';
import type { Paint, Primitive, SceneProps } from '../../lib/visuals';

const props = defineProps<SceneProps>();
// A spatial drawing cannot reflow like prose. The tool description promises this
// fixed width, so percentages keep one aspect ratio inline and in the expanded
// view; narrower viewports scroll instead of squeezing or stretching geometry.
const WIDTH = 768;
const colors: Record<Paint, string> = {
  none: 'none',
  surface: 'var(--surface-base)',
  muted: 'var(--surface-active)',
  ink: 'var(--text-secondary)',
  accent: 'var(--accent)',
  secondary: 'var(--moss)',
};
function shape(item: Primitive) {
  const x = (value: number) => (value * WIDTH) / 100;
  const y = (value: number) => (value * props.height) / 100;
  const attrs = {
    fill: 'fill' in item ? colors[item.fill] : 'none',
    stroke: 'stroke' in item ? colors[item.stroke] : 'none',
    'stroke-width': 1.5,
    ...('dashed' in item && item.dashed ? { 'stroke-dasharray': '6 5' } : {}),
  };
  switch (item.kind) {
    case 'rect':
      return h('rect', {
        ...attrs,
        x: x(item.x),
        y: y(item.y),
        width: x(item.width),
        height: y(item.height),
      });
    case 'ellipse':
      return h('ellipse', { ...attrs, cx: x(item.x), cy: y(item.y), rx: x(item.rx), ry: y(item.ry) });
    case 'line':
      return h('line', { ...attrs, x1: x(item.x1), y1: y(item.y1), x2: x(item.x2), y2: y(item.y2) });
    case 'path':
      return h(item.closed ? 'polygon' : 'polyline', {
        ...attrs,
        points: item.points.map(([a, b]) => `${x(a)},${y(b)}`).join(' '),
      });
    case 'label':
      return h('text', { x: x(item.x), y: y(item.y), 'text-anchor': item.anchor }, item.text);
  }
}
const drawing = () =>
  h(
    'svg',
    {
      width: WIDTH,
      height: props.height,
      viewBox: `0 0 ${WIDTH} ${props.height}`,
      role: 'img',
      'aria-label': props.title,
    },
    [
      h('title', props.title),
      ...props.items.filter((item) => !item.showFor || item.showFor.includes(props.selection)).map(shape),
    ],
  );
</script>
<template>
  <div class="visual-scene" tabindex="0" :aria-label="`${title}，可横向滚动`">
    <component :is="drawing" />
  </div>
</template>
