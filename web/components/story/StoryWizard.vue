<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import StorySetup from './StorySetup.vue';
import CharacterPortrait from '../character/CharacterPortrait.vue';
import { cardPortraitUrl } from '../../lib/portraits';
const props = defineProps<{ cards: any[] }>();
const emit = defineEmits<{ started: [session: any]; back: [] }>();
const selectedId = ref(''),
  search = ref('');
const selected = computed(() => props.cards.find((card) => card.id === selectedId.value));
const filtered = computed(() =>
  props.cards.filter((card) => card.name.toLowerCase().includes(search.value.trim().toLowerCase())),
);
const description = (card: any) => (card.document.data || card.document).description || '暂无角色描述';
watch(
  () => props.cards,
  () => {
    if (!selected.value) selectedId.value = '';
  },
);
</script>
<template>
  <StorySetup
    v-if="selected"
    :key="selected.id"
    :card="selected"
    @back="selectedId = ''"
    @started="emit('started', $event)"
  />
  <section v-else class="editor-page story-setup">
    <button class="quiet" @click="emit('back')">← 返回</button>
    <div class="story-steps"><span class="active">1 · 选择角色卡</span><span>2 · 用户与开场</span></div>
    <h1>开始新故事</h1>
    <p class="muted">先选择一张角色卡，再设置用户身份和开场。</p>
    <input v-model="search" class="story-card-search" aria-label="搜索角色卡" placeholder="搜索角色卡…" />
    <div class="story-card-list">
      <button
        v-for="card in filtered"
        :key="card.id"
        class="story-card-option"
        :data-card-id="card.id"
        :aria-label="`选择角色卡 ${card.name}`"
        @click="selectedId = card.id"
      >
        <CharacterPortrait :src="cardPortraitUrl(card)" :name="card.name" size="card" />
        <span class="story-card-description"
          ><strong>{{ card.name }}</strong>
          <p>{{ description(card) }}</p></span
        ><span aria-hidden="true">→</span>
      </button>
    </div>
    <p v-if="!cards.length" class="muted">书架中暂无角色卡。请先从左侧“角色卡”页导入。</p>
    <p v-else-if="!filtered.length" class="muted">未找到匹配的角色卡。</p>
  </section>
</template>
