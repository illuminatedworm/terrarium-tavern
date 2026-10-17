<script setup lang="ts">
/** Request-order rows shared by the Writer/Agent and Advisor context previews. */
export interface ContextRow {
  key: string | number;
  /** Story round or other short locator, shown in the category colour. */
  round?: string;
  title?: string;
  /** Leading wrapper tag of the message, without brackets. */
  tag?: string;
  excerpt: string;
  role: string;
  position?: string | number;
  tokens?: string;
  content: string;
}
export interface ContextRowGroup {
  id: string;
  kind: string;
  label: string;
  /** Raw source key, kept as a tooltip for debugging. */
  hint?: string;
  meta?: string;
  tokens?: string;
  anchored?: boolean;
  rows: ContextRow[];
}
defineProps<{ groups: ContextRowGroup[] }>();
</script>

<template>
  <div class="context-groups" aria-label="上下文结构">
    <component
      :is="group.rows.length > 1 ? 'details' : 'section'"
      v-for="group in groups"
      :key="group.id"
      class="context-group"
      :class="[`context-${group.kind}`, { single: group.rows.length === 1, anchored: group.anchored }]"
      :open="group.rows.length > 1 ? true : undefined"
    >
      <summary v-if="group.rows.length > 1" :title="group.hint">
        <span class="context-dot"></span><strong>{{ group.label }}</strong
        ><small v-if="group.meta">{{ group.meta }}</small
        ><small v-if="group.tokens" class="context-tokens">{{ group.tokens }}</small>
      </summary>
      <div class="context-items">
        <details v-for="row in group.rows" :key="row.key" class="context-entry">
          <summary>
            <span v-if="group.rows.length === 1" class="context-dot"></span
            ><strong v-if="group.rows.length === 1" class="context-entry-label" :title="group.hint">{{
              group.label
            }}</strong
            ><span v-if="row.round" class="context-round">{{ row.round }}</span
            ><strong
              v-if="row.title && (group.rows.length > 1 || row.title !== group.label)"
              class="context-entry-title"
              :title="row.title"
              >{{ row.title }}</strong
            ><code v-if="row.tag" class="context-tag">&lt;{{ row.tag }}&gt;</code
            ><span class="context-excerpt">{{ row.excerpt }}</span
            ><span class="badge">{{ row.role }}</span
            ><span v-if="row.position != null" class="context-entry-position">位置 {{ row.position }}</span
            ><small v-if="row.tokens" class="context-tokens">{{ row.tokens }}</small>
          </summary>
          <pre>{{ row.content }}</pre>
        </details>
      </div>
    </component>
  </div>
</template>
