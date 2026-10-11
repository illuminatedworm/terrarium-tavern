<script setup lang="ts">
import { computed } from 'vue';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { openingProse } from '../../lib/storyText';

const props = defineProps<{ text: string; opening?: boolean }>();
const html = computed(() => {
  const source = props.opening ? openingProse(props.text) : props.text;
  // A reading preview is not the card runtime. Keep text/structure but prevent
  // card CSS, interactive elements and remote resources from affecting the editor.
  const readable = source.replace(/```html\s*\n([\s\S]*?)```/gi, '$1');
  return DOMPurify.sanitize(marked.parse(readable, { async: false, gfm: true }) as string, {
    ALLOWED_TAGS: [
      'p',
      'br',
      'strong',
      'em',
      'b',
      'i',
      's',
      'u',
      'blockquote',
      'h1',
      'h2',
      'h3',
      'h4',
      'h5',
      'h6',
      'ul',
      'ol',
      'li',
      'pre',
      'code',
      'table',
      'thead',
      'tbody',
      'tr',
      'td',
      'th',
      'hr',
      'details',
      'summary',
      'div',
      'span',
    ],
    ALLOWED_ATTR: [],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
});
</script>

<template><div class="card-preview-prose" v-html="html" /></template>

<style scoped>
.card-preview-prose {
  color: var(--text-story);
  font: 14px/1.85 var(--font-ui);
  overflow-wrap: anywhere;
}
.card-preview-prose :deep(:first-child) {
  margin-top: 0;
}
.card-preview-prose :deep(:last-child) {
  margin-bottom: 0;
}
.card-preview-prose :deep(p) {
  margin: 0 0 12px;
}
.card-preview-prose :deep(h1),
.card-preview-prose :deep(h2),
.card-preview-prose :deep(h3),
.card-preview-prose :deep(h4) {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 18px 0 8px;
}
.card-preview-prose :deep(ul),
.card-preview-prose :deep(ol) {
  padding-left: 22px;
}
.card-preview-prose :deep(pre) {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: 12px/1.75 monospace;
}
.card-preview-prose :deep(table) {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
}
.card-preview-prose :deep(td),
.card-preview-prose :deep(th) {
  padding: 6px;
  border: 1px solid var(--border-subtle);
}
.card-preview-prose :deep(blockquote) {
  margin: 12px 0;
  padding-left: 12px;
  border-left: 2px solid var(--border-control);
}
.card-preview-prose :deep(details) {
  margin: 10px 0;
  padding: 10px 0;
  border: 0;
  border-top: 1px solid var(--border-subtle);
}
</style>
