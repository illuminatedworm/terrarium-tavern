<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { computed, onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import StoryProse from './StoryProse.vue';
import { openingProse } from '../../lib/storyText';

const props = defineProps<{ card: any }>();
const emit = defineEmits<{ started: [session: any]; back: [] }>();
const personas = ref<any[]>([]),
  personaId = ref(''),
  personaName = ref('旅人');
const greeting = ref(0),
  busy = ref(false),
  error = ref('');
const data = computed(() => props.card.document.data || props.card.document);
const greetings = computed(() => [data.value.first_mes || '', ...(data.value.alternate_greetings || [])]);
const opening = computed(() =>
  greetings.value[greeting.value]
    .replace(
      /\{\{user\}\}/g,
      () => personas.value.find((p) => p.id === personaId.value)?.name || personaName.value,
    )
    .replace(/\{\{char\}\}/g, () => data.value.name),
);
function moveGreeting(event: KeyboardEvent, index: number) {
  const next = (index + greetings.value.length) % greetings.value.length;
  greeting.value = next;
  (event.currentTarget as HTMLElement).parentElement
    ?.querySelectorAll<HTMLButtonElement>('button')
    [next]?.focus();
}
async function start() {
  error.value = '';
  busy.value = true;
  try {
    const session = await api('/sessions', {
      card_id: props.card.id,
      greeting: greeting.value,
      persona: personaName.value,
      persona_id: personaId.value || null,
    });
    emit('started', session);
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
onMounted(async () => {
  personas.value = await api('/personas');
});
useTimedError(error);
</script>

<template>
  <section class="editor-page story-setup">
    <button class="quiet" @click="emit('back')">← 重新选择角色卡</button>
    <div class="story-steps">
      <button @click="emit('back')">1 · 选择角色卡</button><span class="active">2 · 玩家与开场</span>
    </div>
    <div class="eyebrow">START A NEW STORY</div>
    <h1>进入 {{ data.name }}</h1>
    <p class="muted">每次进入都会创建独立会话</p>
    <AppNotification :message="error" />
    <div class="story-setup-grid">
      <div class="story-setup-options">
        <label
          >玩家角色<select v-model="personaId">
            <option value="">临时身份</option>
            <option v-for="p in personas" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select></label
        >
        <label v-if="!personaId">临时玩家名称<input v-model="personaName" /></label>
        <p v-else class="persona-preview">{{ personas.find((p) => p.id === personaId)?.description }}</p>
        <button class="primary" :disabled="busy || (!personaId && !personaName.trim())" @click="start">
          {{ busy ? '正在准备…' : '进入故事 →' }}
        </button>
      </div>
      <div class="story-opening">
        <h3>开场预览</h3>
        <div class="greeting-tabs" role="tablist" aria-label="开场白">
          <button
            v-for="(_, i) in greetings"
            :key="i"
            role="tab"
            :aria-selected="greeting === i"
            :tabindex="greeting === i ? 0 : -1"
            :class="{ active: greeting === i }"
            @click="greeting = i"
            @keydown.right.prevent="moveGreeting($event, i + 1)"
            @keydown.left.prevent="moveGreeting($event, i - 1)"
          >
            开场 {{ i + 1 }}
          </button>
        </div>
        <div role="tabpanel" :aria-label="`开场 ${greeting + 1} 预览`">
          <StoryProse :text="openingProse(opening)" />
        </div>
      </div>
    </div>
  </section>
</template>
