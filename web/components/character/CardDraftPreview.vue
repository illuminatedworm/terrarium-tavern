<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { cardName, isWorldbook, type EditorCard } from '../../lib/cardAuthor';
import { previewCardData, previewLore } from '../../lib/cardPreview';
import { cardPortraitUrl } from '../../lib/portraits';
import CharacterPortrait from './CharacterPortrait.vue';
import CardPreviewProse from './CardPreviewProse.vue';
import CardWorkspaceIcon from './CardWorkspaceIcon.vue';

const props = defineProps<{ card: EditorCard; conversationId: string; status: string; disabled?: boolean }>();
defineEmits<{ edit: [] }>();
const book = computed(() => isWorldbook(props.card));
const data = computed(() => previewCardData(props.card.document));
const lore = computed(() => previewLore(props.card.document));
const opening = ref(0);
const openingExpanded = ref(false);
const query = ref('');
const greetings = computed(() => [data.value.first_mes || '', ...(data.value.alternate_greetings || [])]);
const fields = computed(() =>
  [
    { key: 'description', label: '角色描述' },
    { key: 'personality', label: '性格' },
    { key: 'scenario', label: '场景' },
  ].filter((field) => data.value[field.key]?.trim()),
);
const additional = computed(() =>
  [
    { key: 'mes_example', label: '示例对话' },
    { key: 'system_prompt', label: '角色系统提示' },
    { key: 'post_history_instructions', label: '历史后提示' },
    { key: 'creator_notes', label: '作者备注' },
  ].filter((field) => data.value[field.key]?.trim()),
);
const matches = computed(() => {
  const terms = query.value.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return lore.value.filter((entry) =>
    terms.every((term) =>
      `${entry.title} ${entry.content} ${entry.keywords.join(' ')}`.toLocaleLowerCase().includes(term),
    ),
  );
});
// A PNG portrait may be set in the draft before it reaches the library, so the
// conversation serves the draft's own image; CHARX icons still come from the saved card.
const portrait = computed(() => {
  if (book.value) return '';
  const source = props.card.source || {};
  if (source.container === 'png' && source.original)
    return `/api/card-author/conversations/${encodeURIComponent(props.conversationId)}/portraits/${encodeURIComponent(source.original)}`;
  return props.card.source_exists === false
    ? ''
    : cardPortraitUrl({ ...source, document: props.card.document });
});
watch(greetings, (value) => {
  if (opening.value >= value.length) opening.value = 0;
});
watch(opening, () => {
  openingExpanded.value = false;
});
</script>

<template>
  <section class="draft-preview" aria-label="当前草稿阅读预览">
    <header class="draft-preview-heading">
      <CharacterPortrait :name="cardName(card)" :src="portrait" />
      <div>
        <h3>{{ cardName(card) }}</h3>
        <p>{{ status }}</p>
      </div>
      <button :disabled="disabled" title="打开表单编辑" aria-label="打开表单编辑" @click="$emit('edit')">
        <CardWorkspaceIcon name="pen" />
      </button>
    </header>
    <p class="draft-preview-note">阅读预览 · 不运行脚本，原始内容可在“数据”中查看。</p>
    <section v-for="field in book ? [] : fields" :key="field.key" class="draft-preview-section">
      <h4>{{ field.label }}</h4>
      <CardPreviewProse :text="data[field.key]" />
    </section>
    <p v-if="!book && !fields.length" class="draft-preview-empty">
      {{ lore.length ? '人物与世界设定请见下方世界书。' : '还没有填写人物或世界设定。' }}
    </p>
    <section v-if="!book" class="draft-preview-section" aria-label="开场预览">
      <div class="draft-section-heading">
        <h4>开场</h4>
        <select v-if="greetings.length > 1" v-model="opening" aria-label="预览开场">
          <option v-for="(_, index) in greetings" :key="index" :value="index">
            {{ index ? `开场 ${index + 1}` : '开场 1（默认）' }}
          </option></select
        ><small v-else>开场 1（默认）</small>
      </div>
      <div
        v-if="greetings[opening]"
        class="draft-opening-text"
        :class="{ limited: greetings[opening].length > 700 && !openingExpanded }"
      >
        <CardPreviewProse :key="opening" :text="greetings[opening]" opening />
      </div>
      <p v-else class="draft-preview-empty">尚未填写开场。</p>
      <button
        v-if="greetings[opening].length > 700"
        class="draft-opening-toggle"
        :aria-expanded="openingExpanded"
        @click="openingExpanded = !openingExpanded"
      >
        {{ openingExpanded ? '收起开场全文' : '展开开场全文'
        }}<CardWorkspaceIcon :name="openingExpanded ? 'up' : 'down'" />
      </button>
    </section>
    <section v-if="lore.length" class="draft-preview-section" aria-label="世界书预览">
      <div class="draft-section-heading">
        <h4>
          世界书 <span>{{ lore.length }}</span>
        </h4>
        <small>{{ lore.filter((entry) => entry.enabled).length }} 条启用</small>
      </div>
      <input
        v-if="lore.length > 4"
        v-model="query"
        type="search"
        aria-label="搜索预览世界书"
        placeholder="搜索条目名称或内容…"
      />
      <details
        v-for="entry in matches"
        :key="entry.key"
        class="draft-lore-entry"
        :class="{ disabled: entry.stateLabel === '未启用' }"
      >
        <summary>
          <CardWorkspaceIcon name="chevron" /><span>{{ entry.title }}</span
          ><small>{{
            entry.stateLabel === '仅初始化'
              ? '仅初始化'
              : `${entry.stateLabel ? `${entry.stateLabel} · ` : ''}${entry.purpose}${entry.audience === 'writer' ? ` · ${entry.trigger}` : ''}`
          }}</small>
        </summary>
        <div class="draft-lore-content">
          <p v-if="entry.keywords.length" class="draft-keywords">主触发词：{{ entry.keywords.join('、') }}</p>
          <CardPreviewProse :text="entry.content" />
        </div>
      </details>
      <p v-if="!matches.length" class="draft-preview-empty">没有匹配的条目。</p>
    </section>
    <p v-else-if="book" class="draft-preview-empty">这本世界书还没有条目。</p>
    <details v-for="field in book ? [] : additional" :key="field.key" class="draft-extra">
      <summary>{{ field.label }}</summary>
      <CardPreviewProse :text="data[field.key]" />
    </details>
  </section>
</template>

<style scoped>
.draft-preview {
  min-width: 0;
}
.draft-opening-text.limited {
  max-height: 260px;
  overflow: hidden;
}
.draft-opening-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  background: transparent;
  color: var(--accent-hover);
  font-size: var(--fs-sm);
  padding: 8px 0 0;
  margin-top: 6px;
}
.draft-opening-toggle svg {
  width: 13px;
  height: 13px;
}
.draft-preview-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  height: auto;
  padding: 0;
  border: 0;
}
.draft-preview-heading :deep(.character-portrait) {
  width: 48px;
  height: 58px;
  font-size: var(--fs-title);
}
.draft-preview-heading > div {
  flex: 1;
  min-width: 0;
}
.draft-preview-heading h3 {
  margin: 0 0 4px;
  font: 600 var(--fs-lg) / 1.5 var(--font-ui);
  overflow-wrap: anywhere;
}
.draft-preview-heading p {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.draft-preview-heading button {
  padding: 8px;
  background: transparent;
  flex: 0 0 auto;
}
.draft-preview-note {
  margin: 14px 0 22px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.7;
}
.draft-preview-section {
  padding: 20px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.draft-preview-section:first-of-type {
  padding-top: 0;
}
.draft-preview-section h4 {
  margin: 0 0 12px;
  font: 600 var(--fs-base) / 1.5 var(--font-ui);
  color: var(--text-primary);
}
.draft-section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.draft-section-heading h4 {
  margin: 0;
}
.draft-section-heading h4 span {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  font-weight: 400;
  margin-left: 5px;
}
.draft-section-heading small {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.draft-section-heading select {
  width: auto;
  max-width: 65%;
  font: var(--fs-sm) / 1.5 var(--font-ui);
  min-height: 30px;
  padding: 4px 8px;
  background: transparent;
}
.draft-preview-empty {
  color: var(--text-muted);
  font-size: var(--fs-md);
  margin: 10px 0 0;
}
.draft-preview-section input {
  width: 100%;
  font-size: var(--fs-sm);
  margin: 0 0 12px;
}
.draft-lore-entry {
  margin: 0 0 8px;
  padding: 0;
  background: transparent;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}
.draft-lore-entry summary {
  display: flex;
  gap: 8px;
  align-items: center;
  list-style: none;
  padding: 10px;
}
.draft-lore-entry summary::-webkit-details-marker {
  display: none;
}
.draft-lore-entry summary svg {
  width: 13px;
  height: 13px;
  color: var(--text-muted);
}
.draft-lore-entry[open] > summary svg {
  transform: rotate(90deg);
}
.draft-lore-entry summary > span {
  min-width: 0;
  flex: 1;
  overflow-wrap: anywhere;
  font-size: var(--fs-md);
}
.draft-lore-entry summary small {
  color: var(--text-muted);
  font-size: var(--fs-2xs);
  text-align: right;
  max-width: 44%;
}
.draft-lore-entry.disabled > summary {
  color: var(--text-muted);
}
.draft-lore-content {
  border-top: 1px solid var(--border-subtle);
  padding: 12px;
}
.draft-keywords {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  margin: 0 0 12px;
  overflow-wrap: anywhere;
}
.draft-extra {
  border: 0;
  border-bottom: 1px solid var(--border-subtle);
  border-radius: 0;
  padding: 16px 0;
  margin: 0;
}
.draft-extra summary {
  color: var(--text-secondary);
  font-size: var(--fs-md);
}
.draft-extra[open] summary {
  margin-bottom: 12px;
}
</style>
