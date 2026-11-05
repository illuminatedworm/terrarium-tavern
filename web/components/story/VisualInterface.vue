<script setup lang="ts">
import { defineComponent, h, ref, type PropType } from 'vue';
import { createStateStore, type UIElement } from '@json-render/core';
import { JSONUIProvider, Renderer, useStateValue, type ComponentRegistry } from '@json-render/vue';
import VisualScene from './VisualScene.vue';
import type { SceneProps, VisualSpec } from '../../lib/visuals';

const props = defineProps<{ spec: VisualSpec }>();
const emit = defineEmits<{ compose: [text: string] }>();
const state = createStateStore({ ...props.spec.state });
const notice = ref('');
const elementProp = { type: Object as PropType<UIElement>, required: true as const };
// Renderer accepts ordinary Vue components. The catalog/schema lives at the
// model/import boundary; do not ship prompt/schema generators into the reader.
const registry: ComponentRegistry = {
  Stack: defineComponent({
    props: { element: elementProp },
    setup(p, { slots }) {
      return () =>
        h(
          'div',
          { class: p.element.props.direction === 'horizontal' ? 'visual-controls' : 'visual-stack' },
          slots.default?.(),
        );
    },
  }),
  Text: defineComponent({
    props: { element: elementProp },
    setup(p) {
      return () => h('p', { class: 'visual-text', 'aria-live': 'polite' }, p.element.props.text as string);
    },
  }),
  Button: defineComponent({
    props: {
      element: elementProp,
      emit: { type: Function as PropType<(name: string) => void>, required: true },
    },
    setup(p) {
      const selection = useStateValue('/selection');
      return () => {
        const press = p.element.on?.press;
        const actions = Array.isArray(press) ? press : press ? [press] : [];
        const select = actions.find(
          (action) => action.action === 'setState' && action.params?.statePath === '/selection',
        );
        return h(
          'button',
          {
            type: 'button',
            'aria-pressed': select ? select.params?.value === selection.value : undefined,
            onClick: () => p.emit('press'),
          },
          p.element.props.label as string,
        );
      };
    },
  }),
  Scene: defineComponent({
    props: { element: elementProp },
    setup(p) {
      return () => h(VisualScene, p.element.props as unknown as SceneProps);
    },
  }),
};
const handlers = {
  composeDraft: async (params?: Record<string, unknown>) => {
    emit('compose', params!.text as string);
    notice.value = '已追加到主输入框，原有草稿保留；请检查后发送。';
  },
};
</script>
<template>
  <div class="visual-interface">
    <JSONUIProvider :registry="registry" :store="state" :handlers="handlers">
      <Renderer :spec="spec" :registry="registry" />
    </JSONUIProvider>
    <p v-if="notice" class="hint" role="status">{{ notice }}</p>
  </div>
</template>
