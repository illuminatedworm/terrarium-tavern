<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { computed, onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import StoryProse from './StoryProse.vue';
import StoryLoading from './StoryLoading.vue';
import { openingProse } from '../../lib/storyText';
import CharacterPortrait from '../character/CharacterPortrait.vue';
import { cardPortraitUrl } from '../../lib/portraits';
import ChannelSwitch from '../common/ChannelSwitch.vue';
import { channelLabels, type Channel } from '../../lib/channels';

const props = defineProps<{ card: any; settings?: any }>();
const emit = defineEmits<{ started: [session: any]; back: []; settings: [channel: Channel, mode: string] }>();
const channel = ref<Channel>('standard');
const settings = ref<any>(null);
const personas = ref<any[]>([]),
  personaId = ref(''),
  personaName = ref('用户');
const greeting = ref(0),
  busy = ref(false),
  error = ref('');
const agentMode = ref('half_agent');
const models = computed(() => {
  const scope = (props.settings || settings.value)?.[agentMode.value];
  const prefix = channel.value === 'chatgpt' ? 'official_' : '';
  return agentMode.value === 'full_agent'
    ? [scope?.[prefix + 'model']]
    : [scope?.[prefix + 'foreground'], scope?.[prefix + 'background']];
});
const modelLabel = computed(() => models.value.map((model) => model?.model || '待配置').join(' / '));
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
  if (busy.value) return;
  error.value = '';
  busy.value = true;
  try {
    const session = await api('/sessions', {
      card_id: props.card.id,
      greeting: greeting.value,
      persona: personaName.value,
      persona_id: personaId.value || null,
      agent_mode: agentMode.value,
      channel_origin: channel.value,
    });
    emit('started', session);
  } catch (e: any) {
    error.value = e.message;
    busy.value = false;
  }
}
onMounted(async () => {
  try {
    [personas.value, settings.value] = await Promise.all([api('/personas'), api('/settings')]);
    channel.value = settings.value?.[agentMode.value]?.default_channel || 'standard';
  } catch (e: any) {
    error.value = e.message;
  }
});
useTimedError(error);
</script>

<template>
  <StoryLoading v-if="busy" creating :name="data.name" />
  <section v-else class="editor-page story-setup">
    <button class="quiet" @click="emit('back')">← 重新选择角色卡</button>
    <div class="story-steps">
      <button @click="emit('back')">1 · 选择角色卡</button><span class="active">2 · 用户与开场</span>
    </div>
    <div class="character-heading">
      <CharacterPortrait :src="cardPortraitUrl(card)" :name="data.name" size="detail" fit="contain" />
      <div>
        <h1>进入 {{ data.name }}</h1>
        <p class="muted">每次进入都会创建独立会话</p>
      </div>
    </div>
    <AppNotification :message="error" />
    <div class="story-setup-grid">
      <div class="story-setup-options">
        <h3>对话渠道</h3>
        <ChannelSwitch v-model="channel" label="对话渠道" />
        <p class="muted">创建后渠道固定；可在同一渠道内更换模型。</p>
        <div class="story-channel-model">
          <span>当前模型</span><strong>{{ modelLabel }}</strong>
          <button class="quiet" @click="emit('settings', channel, agentMode)">
            配置{{ channelLabels[channel] }}模型
          </button>
        </div>
        <label
          >运行模式<select v-model="agentMode" aria-label="运行模式">
            <option value="half_agent">Half Agent · Writer 与 Tracker</option>
            <option value="full_agent">Full Agent · 单 Agent 与工具</option>
          </select></label
        >
        <p class="muted">
          {{
            agentMode === 'full_agent'
              ? '由 Agent 自行检索、维护笔记和切换工作窗口。需要支持原生工具调用的模型。'
              : 'Writer 负责正文，Tracker 负责后台整理，运行时管理上下文。'
          }}
        </p>
        <p v-if="agentMode === 'full_agent'" class="muted">
          上下文方式：本地换窗{{ channel === 'chatgpt' ? '（创建后可切换云端 compact）' : '' }}
        </p>
        <label
          >用户角色<select v-model="personaId">
            <option value="">临时身份</option>
            <option v-for="p in personas" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select></label
        >
        <label v-if="!personaId">临时用户名称<input v-model="personaName" /></label>
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
