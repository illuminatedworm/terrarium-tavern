<script setup lang="ts">
import Icon from '../common/Icon.vue';
import { computed, ref, watch } from 'vue';
import StorySetup from './StorySetup.vue';
import CharacterPortrait from '../character/CharacterPortrait.vue';
import { cardPortraitUrl } from '../../lib/portraits';
import { api } from '../../lib/api';
import type { CardRecord, CardSummary } from '../../lib/types';
import type { Channel } from '../../lib/channels';
const props = defineProps<{ cards: CardSummary[]; settings?: any }>();
const emit = defineEmits<{
  started: [session: any];
  back: [];
  settings: [channel: Channel];
  cardChanged: [card: CardRecord];
}>();
const selected = ref<CardRecord | null>(null),
  loading = ref(''),
  error = ref(''),
  search = ref('');
const filtered = computed(() =>
  props.cards.filter((card) => card.name.toLowerCase().includes(search.value.trim().toLowerCase())),
);
let request = 0;
async function selectCard(card: CardSummary) {
  const ticket = ++request;
  loading.value = card.id;
  error.value = '';
  try {
    const detail = await api(`/cards/${card.id}`);
    if (ticket === request && props.cards.some((item) => item.id === card.id)) selected.value = detail;
  } catch (cause) {
    if (ticket === request) error.value = String(cause);
  } finally {
    if (ticket === request) loading.value = '';
  }
}
function cardChanged(card: CardRecord) {
  selected.value = card;
  emit('cardChanged', card);
}
watch(
  () => props.cards,
  (cards) => {
    if (selected.value && !cards.some((item) => item.id === selected.value?.id)) selected.value = null;
  },
);
</script>
<template>
  <StorySetup
    v-if="selected"
    :key="selected.id"
    :card="selected"
    :settings="settings"
    @back="selected = null"
    @started="emit('started', $event)"
    @settings="emit('settings', $event)"
    @card-changed="cardChanged"
  />
  <section v-else class="editor-page story-setup">
    <button class="quiet editor-back" @click="emit('back')"><Icon name="arrow-left" :size="16" />返回</button>
    <div class="story-steps">
      <span class="active">1 · 选择角色卡</span><span>2 · 用户与开场</span><span>3 · 挂载世界书</span>
    </div>
    <h1>开始新故事</h1>
    <p class="muted">先选择一张角色卡，再设置用户身份、开场和要挂载的世界书。</p>
    <div class="story-card-search">
      <Icon name="search" :size="16" />
      <input v-model="search" aria-label="搜索角色卡" placeholder="搜索角色卡…" />
    </div>
    <div class="story-card-list">
      <button
        v-for="card in filtered"
        :key="card.id"
        class="story-card-option"
        :data-card-id="card.id"
        :aria-label="`选择角色卡 ${card.name}`"
        :aria-busy="loading === card.id"
        @click="selectCard(card)"
      >
        <CharacterPortrait :src="cardPortraitUrl(card)" :name="card.name" size="card" />
        <span class="story-card-description"
          ><strong>{{ card.name }}</strong>
          <p>{{ card.description || '暂无角色描述' }}</p></span
        >
      </button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">正在读取角色卡…</p>
    <p v-if="!cards.length" class="muted">书架中暂无角色卡。请先从左侧“角色卡”页导入。</p>
    <p v-else-if="!filtered.length" class="muted">未找到匹配的角色卡。</p>
  </section>
</template>
