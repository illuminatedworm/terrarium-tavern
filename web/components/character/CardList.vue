<script setup lang="ts">
import { ref } from 'vue';
import Icon from '../common/Icon.vue';
import CharacterPortrait from './CharacterPortrait.vue';
import { cardPortraitUrl } from '../../lib/portraits';
import type { CardRecord, OrphanDraft } from '../../lib/types';

/** Left sidebar list of library cards and drafts whose card was deleted. */
defineProps<{ cards: CardRecord[]; orphanDrafts: OrphanDraft[]; activeId?: string }>();
const emit = defineEmits<{
  import: [file: File];
  show: [card: CardRecord];
  copy: [card: CardRecord];
  delete: [card: CardRecord];
  'open-draft': [id: string];
}>();
const fileInput = ref<HTMLInputElement>();
function chosen(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  // Clear first so choosing the same file again still fires change.
  input.value = '';
  if (file) emit('import', file);
}
</script>

<template>
  <header class="panel-head">
    <h2>角色卡</h2>
    <span class="panel-count">{{ cards.length }}</span>
  </header>
  <button class="new-story import-only" @click="fileInput?.click()">
    <Icon name="import" :size="17" />导入角色卡</button
  ><input ref="fileInput" type="file" accept=".json,.png,.charx" hidden @change="chosen" />
  <nav class="session-list card-side-list" aria-label="角色卡列表">
    <div v-for="card in cards" :key="card.id" class="card-side-row" :data-card-id="card.id">
      <button class="card-side-item" :class="{ active: activeId === card.id }" @click="emit('show', card)">
        <CharacterPortrait :src="cardPortraitUrl(card)" :name="card.name" />
        <span
          >{{ card.name
          }}<small>{{ card.report.variables === 'mvu_detected' ? 'MVU' : '普通角色卡' }}</small></span
        >
      </button>
      <div class="card-row-actions" role="group" :aria-label="`${card.name} 操作`">
        <button
          class="card-copy-action"
          title="复制"
          :aria-label="`复制角色卡 ${card.name}`"
          @click="emit('copy', card)"
        >
          <Icon name="copy" :size="15" /></button
        ><button
          class="card-delete-action"
          title="删除"
          :aria-label="`删除角色卡 ${card.name}`"
          @click="emit('delete', card)"
        >
          <Icon name="trash" :size="15" />
        </button>
      </div>
    </div>
    <p v-if="!cards.length" class="empty-small">从上方导入 JSON、PNG 或 CHARX 角色卡。</p>
    <p v-if="orphanDrafts.length" class="panel-subhead">原卡已删除的编辑草稿</p>
    <button
      v-for="draft in orphanDrafts"
      :key="draft.id"
      class="orphan-draft"
      @click="emit('open-draft', draft.id)"
    >
      <span class="session-glyph"><Icon name="pencil" :size="16" /></span>继续编辑 {{ draft.name }}
    </button>
  </nav>
</template>
