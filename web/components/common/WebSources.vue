<script setup lang="ts">
import { computed } from 'vue';
import { webSources, type WebSource } from '../../lib/webSearch';
const props = defineProps<{ sources: WebSource[] }>();
const links = computed(() => webSources([{ web_sources: props.sources }]));
</script>
<template>
  <details v-if="links.length" class="web-sources">
    <summary>网页来源 · {{ links.length }}</summary>
    <ul>
      <li v-for="source in links" :key="source.url">
        <a :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.title || source.url }}</a>
      </li>
    </ul>
  </details>
</template>
<style scoped>
.web-sources {
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  margin-block: 8px;
  overflow-wrap: anywhere;
}
ul {
  padding-left: 20px;
  margin: 8px 0;
}
a {
  color: var(--accent);
}
</style>
