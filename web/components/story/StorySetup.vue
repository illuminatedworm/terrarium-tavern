<script setup lang="ts">
import Icon from '../common/Icon.vue';
import { useTimedError } from '../../composables/useTimedError';
import { computed, onMounted, ref, watch } from 'vue';
import { api } from '../../lib/api';
import StoryProse from './StoryProse.vue';
import StoryLoading from './StoryLoading.vue';
import { openingProse } from '../../lib/storyText';
import CharacterPortrait from '../character/CharacterPortrait.vue';
import { cardPortraitUrl } from '../../lib/portraits';
import ChannelSwitch from '../common/ChannelSwitch.vue';
import { channelLabels, type Channel } from '../../lib/channels';
import type { CardDefaults, WorldbookSummary } from '../../lib/types';

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
// Step 2 sets the user and opening; step 3 picks library worldbooks to mount.
const step = ref<2 | 3>(2);
const worldbooks = ref<WorldbookSummary[]>([]),
  chosen = ref(new Set<string>()),
  defaults = ref<CardDefaults | null>(null);
// Kemini conversations are fixed at creation and exist only on the standard channel.
const kemini = ref(false);
const keminiAvailable = computed(() => !!(props.settings || settings.value)?.shared?.kemini?.enabled);
watch(channel, (value) => {
  if (value !== 'standard') kemini.value = false;
});
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
function toggleWorldbook(id: string, on: boolean) {
  const next = new Set(chosen.value);
  if (on) next.add(id);
  else next.delete(id);
  chosen.value = next;
}
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
      kemini: kemini.value,
      // Mount order follows the library list, as shown in step 3.
      worldbook_ids: worldbooks.value.filter((book) => chosen.value.has(book.id)).map((book) => book.id),
    });
    emit('started', session);
  } catch (e: any) {
    error.value = e.message;
    busy.value = false;
  }
}
onMounted(async () => {
  try {
    [personas.value, settings.value, worldbooks.value, defaults.value] = await Promise.all([
      api('/personas'),
      api('/settings'),
      api('/worldbooks'),
      api(`/cards/${props.card.id}/defaults`),
    ]);
    channel.value = settings.value?.[agentMode.value]?.default_channel || 'standard';
    // Card defaults only preselect; deleted targets were already dropped with them.
    personaId.value = defaults.value?.persona_id || '';
    chosen.value = new Set(defaults.value?.worldbook_ids);
  } catch (e: any) {
    error.value = e.message;
  }
});
useTimedError(error);
</script>

<template>
  <StoryLoading v-if="busy" creating :name="data.name" />
  <section v-else class="editor-page story-setup" :class="{ kemini }">
    <button class="quiet editor-back" @click="emit('back')">
      <Icon name="arrow-left" :size="16" />重新选择角色卡
    </button>
    <div class="story-steps">
      <button @click="emit('back')">1 · 选择角色卡</button
      ><button v-if="step === 3" @click="step = 2">2 · 用户与开场</button
      ><span v-else class="active">2 · 用户与开场</span
      ><span :class="{ active: step === 3 }">3 · 挂载世界书</span>
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
      <div v-if="step === 3" class="story-setup-options">
        <h3>挂载世界书</h3>
        <p class="muted">
          所选世界书的条目复制进本对话的角色卡世界书，与角色卡自带条目一起参与检索。之后修改书库不会影响这个对话；可在“场景
          → 编辑对话世界书 → 挂载世界书”中重新载入或卸载。
        </p>
        <p v-if="defaults?.worldbook_ids.length" class="muted">已按角色卡默认项预选。</p>
        <p v-if="!worldbooks.length" class="empty-small">
          世界书库为空，可直接进入故事，之后也能在对话中挂载。
        </p>
        <div v-else class="worldbook-choice-list">
          <label v-for="book in worldbooks" :key="book.id" class="check worldbook-choice">
            <input
              type="checkbox"
              :checked="chosen.has(book.id)"
              :aria-label="`挂载世界书 ${book.name}`"
              @change="toggleWorldbook(book.id, ($event.target as HTMLInputElement).checked)"
            /><span>{{ book.name }}</span
            ><small>{{ book.entries }} 条</small>
          </label>
        </div>
        <button class="primary" :disabled="busy || (!personaId && !personaName.trim())" @click="start">
          {{ busy ? '正在准备…' : '进入故事'
          }}<Icon v-if="!busy" name="arrow-right" :size="16" class="enter-icon" />
        </button>
        <button class="quiet story-setup-back" @click="step = 2">
          <Icon name="arrow-left" :size="16" />上一步：用户与开场
        </button>
      </div>
      <div v-else class="story-setup-options">
        <h3>对话渠道</h3>
        <ChannelSwitch v-model="channel" label="对话渠道" />
        <p class="muted">创建后渠道固定；可在同一渠道内更换模型。</p>
        <template v-if="keminiAvailable">
          <label class="check"
            ><input v-model="kemini" type="checkbox" :disabled="channel !== 'standard'" />Kemini
            实验对话</label
          >
          <p class="muted">
            仅此类对话应用 Kemini 式实验规则，头像以梅紫色描边区分；创建后不能与普通对话互相转换，ChatGPT
            官方渠道不可用。
          </p>
        </template>
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
        <p class="muted">
          上下文方式：{{ agentMode === 'full_agent' ? '本地换窗' : '本地滚动窗口'
          }}{{ channel === 'chatgpt' ? '（创建后可切换云端 compact）' : '' }}
        </p>
        <label
          >用户角色<select v-model="personaId">
            <option value="">临时身份</option>
            <option v-for="p in personas" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select></label
        >
        <p v-if="defaults?.persona_id && personaId === defaults.persona_id" class="muted">
          已按角色卡默认项预选。
        </p>
        <label v-if="!personaId">临时用户名称<input v-model="personaName" /></label>
        <p v-else class="persona-preview">{{ personas.find((p) => p.id === personaId)?.description }}</p>
        <button class="primary" :disabled="!personaId && !personaName.trim()" @click="step = 3">
          下一步：挂载世界书<Icon name="arrow-right" :size="16" class="enter-icon" />
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
