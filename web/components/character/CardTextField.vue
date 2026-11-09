<script setup lang="ts">
import { computed } from 'vue';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import CardPreviewProse from './CardPreviewProse.vue';

const props = defineProps<{ label: string; plain?: boolean; spacious?: boolean; disabled?: boolean }>();
const model = defineModel<string | null>();
const source = computed(() => String(model.value ?? ''));
const filled = computed(() => source.value.trim().length > 0);
const excerpt = computed(() => {
  const head = source.value.slice(0, 2048);
  // The plain excerpt keeps line breaks but clamps to two lines; a blank line
  // would spend one of them, leaving only an ellipsis visible.
  return props.plain ? head.trim().replace(/\n(?:[ \t]*\n)+/g, '\n') : head;
});
</script>

<template>
  <div class="card-text-field" :class="{ 'is-plain': plain, 'is-spacious': spacious, 'is-empty': !filled }">
    <div class="card-text-label">
      <strong>{{ label }}</strong>
      <span v-if="!filled">未填写</span>
    </div>
    <div class="card-text-action">
      <ExpandableTextarea
        :model-value="source"
        :label="label"
        :disabled="disabled"
        :dialog-action="filled ? '编辑' : '填写'"
        dialog-only
        @update:model-value="model = $event"
      />
    </div>
    <div v-if="filled" class="card-text-excerpt">
      <div v-if="plain" class="card-text-source">{{ excerpt }}</div>
      <CardPreviewProse v-else :text="excerpt" excerpt />
    </div>
  </div>
</template>

<style scoped>
.card-text-field {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  column-gap: 14px;
  padding-block: 10px;
}
.card-text-field + .card-text-field {
  border-top: 1px solid var(--border-subtle);
}
.card-text-label {
  grid-column: 1;
  grid-row: 1;
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 8px;
  color: var(--text-primary);
  font-size: var(--fs-md);
}
.card-text-label span {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.card-text-action {
  grid-column: -2;
  grid-row: 1;
}
.card-text-action :deep(.dialog-editor-trigger) {
  padding: 4px 9px;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.card-text-action :deep(.dialog-editor-trigger:hover:not(:disabled)) {
  color: var(--text-primary);
}
.card-text-excerpt {
  --card-excerpt-lines: 3;
  grid-column: 1 / -1;
  margin-top: 6px;
  /* Same measure as the story column, so the preview wraps like play does. */
  max-inline-size: calc(var(--reading-width, 1120px) - 120px);
  overflow: clip;
  font: var(--story-font-size, var(--fs-base)) / var(--story-line-height, 1.85)
    var(--story-font-family, var(--font-ui));
}
.is-spacious {
  padding-block: 14px;
}
.is-spacious .card-text-excerpt {
  --card-excerpt-lines: 5;
}
.card-text-excerpt :deep(.card-preview-prose) {
  font: inherit;
}
.card-text-excerpt :deep(.card-preview-prose p) {
  margin-bottom: 0.35lh;
}
.card-text-excerpt :deep(.card-preview-prose p:last-child) {
  margin-bottom: 0;
}
.is-plain {
  grid-template-columns: minmax(110px, 15%) minmax(0, 1fr) auto;
}
.is-plain.is-empty .card-text-label {
  grid-column: 1 / -2;
}
.is-plain .card-text-excerpt {
  --card-excerpt-lines: 2;
  grid-column: 2;
  grid-row: 1;
  margin-top: 0;
  max-inline-size: none;
  color: var(--text-secondary);
  font: var(--fs-md) / 1.65 var(--font-ui);
}
.card-text-excerpt :deep(.card-preview-prose),
.card-text-source {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: var(--card-excerpt-lines);
  overflow: hidden;
}
.card-text-source {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 620px) {
  .is-plain {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .is-plain .card-text-excerpt {
    grid-column: 1 / -1;
    grid-row: auto;
    margin-top: 6px;
  }
}
</style>
