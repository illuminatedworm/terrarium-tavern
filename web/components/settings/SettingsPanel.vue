<script setup lang="ts">
import { computed, nextTick, onMounted, provide, ref } from 'vue';
import { api } from '../../lib/api';
import { useSettingsEditor } from '../../composables/useSettingsEditor';
import type {
  SettingsDocument,
  SettingsPage,
  SettingsScope,
  SettingsObject,
} from '../../lib/settingsDocument';
import {
  loreFields,
  retrievalFields,
  retrievalAdvanced,
  storyToolFields,
  networkFields,
  requestFields,
  proxyFields,
  miscFields,
  shortcutFields,
  debugFields,
  experimentFields,
} from '../../lib/settingsFields';
import SettingsFields from './SettingsFields.vue';
import PolicySelect from './PolicySelect.vue';
import ModelProfileEditor from './ModelProfileEditor.vue';
import ProviderSettings from './ProviderSettings.vue';
import LocalResourceSettings from './LocalResourceSettings.vue';
import StorageSettings from './StorageSettings.vue';
import FailedRequestLogs from './FailedRequestLogs.vue';
import KeminiSettings from './KeminiSettings.vue';
import DataTransferSettings from './DataTransferSettings.vue';
import type { TransferResult } from '../../lib/dataTransfer';
import PromptSettingsEditor from './PromptSettingsEditor.vue';
import SaveStatus from './SaveStatus.vue';
import Icon from '../common/Icon.vue';
import PreferencePresetSettings from './PreferencePresetSettings.vue';
import AdvisorSettings from './AdvisorSettings.vue';
import NovelSettings from './NovelSettings.vue';
import CardAuthorSettings from './CardAuthorSettings.vue';
import WebSettings from './WebSettings.vue';
import GlobalMediaSettings from './GlobalMediaSettings.vue';
import ChannelSwitch from '../common/ChannelSwitch.vue';
import { blankChannelProfile, channelProviders, storyProfileKey, type Channel } from '../../lib/channels';
import type { ModelWarmupState } from '../../composables/useModelWarmup';
const props = defineProps<{
  initialPage?: string;
  initialChannel?: Channel;
  effortOptions: Array<{ value: string; label: string }>;
  warmup: ModelWarmupState;
}>();
const emit = defineEmits<{
  updated: [settings: SettingsDocument];
  close: [];
  imported: [result: TransferResult];
  openSession: [id: string];
}>();
const transferBusy = ref(false);
const editor = useSettingsEditor((settings) => emit('updated', settings));
const { saved, draft, error, busy, textBusy, textDirty, textErrors, pending } = editor;
const pageId = ref('');
const channel = ref<Channel>(props.initialChannel || 'standard');
const storyProfile = computed(() => draft.value?.story[storyProfileKey(channel.value)]);
const providersForChannel = computed(() =>
  channelProviders(draft.value?.shared.providers || [], channel.value),
);
function createChannelProfile() {
  if (!draft.value) return;
  const { story, shared } = draft.value;
  story[storyProfileKey(channel.value)] = blankChannelProfile(story.model, shared.providers, channel.value);
}
const localErrors = ref<Record<string, string>>({});
provide('settings-local-errors', localErrors);
const query = ref('');
const promptPath = ref('system_prompt');
const pendingAction = ref<null | (() => void)>(null);
const discardAll = ref(false);
const continueButton = ref<HTMLButtonElement>();
let returnFocus: HTMLElement | null = null;
const page = computed(() => draft.value?.pages.find((item) => item.id === pageId.value));
const scope = computed<SettingsScope>(() => page.value?.scope || 'story');
provide('settings-scope', scope);
provide(
  'settings-field-errors',
  computed(() =>
    Object.fromEntries(
      error.value
        .replace(/^Error: /, '')
        .split('；')
        .map((part) => {
          const [path, ...message] = part.split('：');
          return [path, message.join('：')];
        }),
    ),
  ),
);
const values = computed<SettingsObject>(() => draft.value?.[scope.value] || {});
const groups = [
  { scope: 'story', label: '游玩' },
  { scope: 'shared', label: '通用' },
];
const groupIcon = (s: string) => (s === 'story' ? 'sparkle' : 'sliders');
const groupLabel = (s: string) => groups.find((g) => g.scope === s)?.label || s;
const prompts = computed(() => draft.value?.prompt_definitions.filter((p) => p.scope === scope.value) || []);
const prompt = computed(() => prompts.value.find((p) => p.path === promptPath.value) || prompts.value[0]);
const advisorPrompt = computed(() =>
  draft.value?.prompt_definitions.find((p) => p.scope === 'shared' && p.path === 'advisor.system_prompt'),
);
const cardAuthorPrompt = computed(() =>
  draft.value?.prompt_definitions.find(
    (p) => p.scope === 'shared' && p.path === 'card_author_config.system_prompt',
  ),
);
const status = computed(() =>
  !saved.value
    ? '正在读取…'
    : Object.keys(localErrors.value).length
      ? '有参数格式错误'
      : error.value
        ? '保存失败'
        : busy.value
          ? '正在保存…'
          : textDirty.value.length
            ? '有文本未保存'
            : pending.value
              ? '待保存'
              : '已保存',
);
function guard(action: () => void, all = false) {
  const blurSave = editor.pendingBlurSave();
  if (blurSave) {
    // The click can reach the guard before the name's blur save is acknowledged.
    void blurSave.then(() => guard(action, all));
    return;
  }
  if (
    Object.keys(localErrors.value).length ||
    textDirty.value.length ||
    (all && error.value && pending.value)
  ) {
    returnFocus = document.activeElement as HTMLElement;
    discardAll.value = all;
    pendingAction.value = action;
    void nextTick(() => continueButton.value?.focus());
  } else action();
}
function continueEditing() {
  pendingAction.value = null;
  void nextTick(() => returnFocus?.focus());
}
function discardAndContinue() {
  const action = pendingAction.value;
  editor.discard(discardAll.value ? undefined : page.value);
  pendingAction.value = null;
  action?.();
}
function trap(event: KeyboardEvent) {
  const buttons = (event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('button');
  if (event.shiftKey && document.activeElement === buttons[0]) {
    event.preventDefault();
    buttons[1]?.focus();
  }
  if (!event.shiftKey && document.activeElement === buttons[1]) {
    event.preventDefault();
    buttons[0]?.focus();
  }
}
function navigate(id: string) {
  if (transferBusy.value) return;
  guard(() => {
    pageId.value = id;
    query.value = '';
    promptPath.value = 'system_prompt';
    localStorage.setItem('tavern-settings-page', id);
  });
}
async function requestClose() {
  if (transferBusy.value) return;
  try {
    await editor.flush();
  } catch {
    /* Guard offers the two agreed choices. */
  }
  guard(() => emit('close'), true);
}
defineExpose({ requestClose });
function changed(event: Event) {
  const element = event.target as HTMLInputElement;
  if (element.tagName === 'SELECT' || element.type === 'checkbox') editor.schedule(true);
}
async function refreshAfterProbe() {
  try {
    const latest: SettingsDocument = await api('/settings');
    editor.acceptRefresh(latest);
  } catch (e) {
    error.value = String(e);
  }
}
async function afterImport(result: TransferResult) {
  if (result.settings_restored) await editor.initialize();
  emit('imported', result);
}
async function savePresets() {
  try {
    await editor.flush();
    await editor.saveValue('shared', 'preference_presets');
  } catch {
    /* Keep the error and draft. */
  }
}
const searchable = computed(() => {
  if (!draft.value) return [];
  const entries: Array<{ page: SettingsPage; label: string; path: string }> = [];
  for (const p of draft.value.pages) {
    entries.push({ page: p, label: p.label, path: '' });
    const fields =
      p.key === 'memory'
        ? storyToolFields
        : p.key === 'retrieval'
          ? [...loreFields, ...retrievalFields, ...retrievalAdvanced]
          : p.key === 'web'
            ? networkFields
            : p.key === 'misc'
              ? miscFields
              : [];
    for (const f of fields) entries.push({ page: p, label: f.label, path: f.path });
    if (p.key === 'media') {
      entries.push({ page: p, label: '全局配音渠道、语音模型与额度', path: 'media.speech' });
      entries.push({ page: p, label: '全局生图渠道、插图模型与额度', path: 'media.image' });
    }
    if (p.key === 'model' || p.key === 'editor')
      for (const [path, label] of [
        ['model', '模型名称'],
        ['context_tokens', '上下文窗口'],
        ['reply_tokens', '输出上限'],
        ['temperature', '温度'],
        ['top_p', '核采样'],
        ['reasoning_effort', '思考强度'],
        ['cache', '提示缓存'],
        ['tokenizer', '计数分词器'],
      ])
        entries.push({ page: p, label, path });
    if (p.key === 'prompts')
      for (const f of draft.value.prompt_definitions.filter((f) => f.scope === p.scope))
        entries.push({ page: p, label: f.label, path: f.path });
  }
  const term = query.value.trim().toLowerCase();
  return term
    ? entries
        .filter((e) => `${groupLabel(e.page.scope)} ${e.label} ${e.path}`.toLowerCase().includes(term))
        .slice(0, 16)
    : [];
});
function selectSearch(result: { page: SettingsPage; label: string; path: string }) {
  if (transferBusy.value) return;
  guard(() => {
    pageId.value = result.page.id;
    promptPath.value = result.path;
    query.value = '';
    void nextTick(() => {
      const field = Array.from(document.querySelectorAll<HTMLElement>('[data-setting-path]')).find(
        (e) => e.dataset.settingPath === result.path,
      );
      if (field) {
        let parent = field.parentElement;
        while (parent) {
          if (parent.tagName === 'DETAILS') (parent as HTMLDetailsElement).open = true;
          parent = parent.parentElement;
        }
        field.scrollIntoView({ block: 'nearest' });
        field.querySelector<HTMLElement>('input,select,textarea')?.focus();
      }
    });
  });
}
onMounted(async () => {
  await editor.initialize();
  const previous = props.initialPage || localStorage.getItem('tavern-settings-page');
  const pages = draft.value?.pages;
  const page = pages?.find((p) => p.id === previous);
  const savedKey = previous?.split('.').at(-1);
  const restored = page || pages?.find((p) => p.scope === 'story' && p.key === savedKey);
  pageId.value = restored?.id || 'story.model';
  if (previous && !page && restored && !props.initialPage)
    localStorage.setItem('tavern-settings-page', restored.id);
});
</script>
<template>
  <div class="settings-shell" :inert="pendingAction !== null">
    <header class="settings-header">
      <span class="settings-mark" aria-hidden="true"><Icon name="cog" :size="19" /></span>
      <h2>设置</h2>
      <div class="settings-search">
        <Icon name="search" :size="16" />
        <input
          v-model="query"
          type="search"
          placeholder="搜索设置…"
          aria-label="搜索设置"
          :disabled="transferBusy"
        />
        <div v-if="query" class="settings-search-results">
          <button v-for="(result, i) in searchable" :key="i" @click="selectSearch(result)">
            <span>{{ result.label }}</span
            ><small>{{ groupLabel(result.page.scope) }} · {{ result.page.label }}</small>
          </button>
          <p v-if="!searchable.length">没有匹配的设置</p>
        </div>
      </div>
      <SaveStatus :state="status" :revision="JSON.stringify(saved?.revisions || {})" />
    </header>
    <p v-if="!draft" class="settings-loading">
      {{ error || '正在读取设置…' }}<button v-if="error" @click="editor.initialize">重试</button>
    </p>
    <div v-else class="settings-layout">
      <nav class="settings-pages" aria-label="设置页面">
        <div v-for="group in groups" :key="group.scope" class="settings-nav-group">
          <h3><Icon :name="groupIcon(group.scope)" :size="14" />{{ group.label }}</h3>
          <button
            v-for="item in draft.pages.filter((p) => p.scope === group.scope)"
            :key="item.id"
            :disabled="transferBusy"
            :class="{ active: pageId === item.id }"
            :aria-current="pageId === item.id ? 'page' : undefined"
            :aria-label="group.label + ' ' + item.label"
            @click="navigate(item.id)"
          >
            {{ item.label }}
          </button>
        </div>
      </nav>
      <div class="settings-mobile-nav">
        <div class="settings-mode-buttons">
          <button
            v-for="group in groups"
            :key="group.scope"
            :disabled="transferBusy"
            :class="{ active: scope === group.scope }"
            @click="navigate(group.scope + '.' + (group.scope === 'shared' ? 'providers' : 'model'))"
          >
            {{ group.label }}
          </button>
        </div>
        <select
          :value="pageId"
          :disabled="transferBusy"
          aria-label="设置页面"
          @change="navigate(($event.target as HTMLSelectElement).value)"
        >
          <option
            v-for="item in draft.pages.filter((p) => p.scope === scope)"
            :key="item.id"
            :value="item.id"
          >
            {{ item.label }}
          </option>
        </select>
      </div>
      <div class="settings-detail" @change="changed">
        <div class="settings-detail-heading">
          <div>
            <div class="settings-crumb">{{ groupLabel(scope) }}</div>
            <h3>{{ page?.label }}</h3>
          </div>
          <button
            v-if="page"
            class="quiet"
            :disabled="busy || transferBusy || !editor.pageChanged(page)"
            @click="editor.undo(page)"
          >
            <Icon name="refresh" :size="15" />撤回本页修改
          </button>
        </div>
        <p class="settings-scope-note">
          {{
            page?.key === 'novel'
              ? '调查模型与额度用于新建调查者；汇报消息适配用于新建对话。'
              : scope === 'shared'
                ? '游玩和辅助对话使用这些通用配置。'
                : '参数独立保存，成功保存后用于下一轮。'
          }}
        </p>
        <section
          v-if="page"
          :key="page.id"
          class="settings-page-content"
          :aria-label="groupLabel(scope) + ' ' + page.label"
        >
          <template v-if="page.key === 'model'">
            <ChannelSwitch v-model="channel" />
            <p class="muted">两类渠道的模型独立保存；已有对话始终使用创建时的渠道。</p>
            <label class="settings-check"
              ><span>新建对话默认使用此渠道</span
              ><input
                type="checkbox"
                :checked="values.default_channel === channel"
                @change="values.default_channel = channel"
            /></label>
            <div class="settings-profiles">
              <ModelProfileEditor
                v-if="storyProfile"
                :key="channel"
                :profile="storyProfile"
                :providers="providersForChannel"
                :channel="channel"
                role="story"
                label="模型连接"
                :effort-options="effortOptions"
                :flush="editor.flush"
                @checked="refreshAfterProbe"
              />
              <div v-else class="channel-empty">
                <p>此渠道的剧情模型尚未配置。</p>
                <button :disabled="!providersForChannel.length" @click="createChannelProfile()">
                  配置模型连接
                </button>
                <button class="quiet" @click="navigate('shared.providers')">管理本渠道供应商</button>
              </div>
            </div>
            <div class="settings-policy" data-setting-path="policy">
              <PolicySelect v-model="values.policy" global compact />
            </div>
          </template>
          <template v-else-if="page.key === 'retrieval'">
            <h4>世界书注入</h4>
            <SettingsFields :value="values" :fields="loreFields" />
            <h4>召回通道与权重</h4>
            <div class="settings-channel-grid">
              <label
                v-for="[key, label] in [
                  ['keyword', '关键词'],
                  ['bm25', '关键词排序'],
                  ['embedding', '向量检索'],
                ]"
                :key="key"
                class="settings-channel"
                ><span
                  ><input v-model="values.retrieval.channels" type="checkbox" :value="key" />{{ label }}</span
                ><input
                  v-model.number="values.retrieval.weights[key]"
                  :aria-label="label + '权重'"
                  type="number"
                  min="0.01"
                  max="100"
                  step="0.1"
              /></label>
            </div>
            <SettingsFields :value="values" :fields="retrievalFields" />
            <details class="settings-advanced">
              <summary>更多检索参数</summary>
              <SettingsFields :value="values" :fields="retrievalAdvanced" />
              <p class="muted">
                切片在神经通道按本地模型分词器计数，在关键词排序通道按词元计数；字符限额单独计算。
              </p>
            </details>
          </template>
          <template v-else-if="page.key === 'memory'">
            <SettingsFields class="story-tool-fields" :value="values" :fields="storyToolFields" />
            <p class="muted">模型通过工具查阅资料、保存笔记并主动换窗。</p>
          </template>
          <template v-else-if="page.key === 'prompts' && prompt">
            <div class="settings-prompt-layout">
              <div class="settings-prompt-list">
                <button
                  v-for="item in prompts"
                  :key="item.path"
                  :class="{ active: prompt.path === item.path }"
                  @click="guard(() => (promptPath = item.path))"
                >
                  {{ item.label }}
                </button>
              </div>
              <PromptSettingsEditor
                :key="scope + '.' + prompt.path"
                :definition="prompt"
                :editor="editor"
                :guard="guard"
              />
            </div>
          </template>
          <GlobalMediaSettings
            v-else-if="page.key === 'media'"
            :value="draft.story.media"
            :providers="draft.shared.providers"
            :flush="editor.flush"
            @providers="navigate('shared.providers')"
          />
          <ProviderSettings
            v-else-if="page.key === 'providers'"
            v-model:providers="draft.shared.providers"
            :initial-channel="channel"
            @channel="channel = $event"
            :flush="editor.flush"
          />
          <NovelSettings
            v-else-if="page.key === 'novel'"
            :value="draft.story.novel_research"
            :main="draft.story.model"
            :official-main="draft.story.official_model"
            :providers="draft.shared.providers"
            :initial-channel="channel"
            :effort-options="effortOptions"
            :flush="editor.flush"
            @checked="refreshAfterProbe"
          />
          <AdvisorSettings
            v-else-if="page.key === 'advisor'"
            :value="draft.shared.advisor"
            :services="draft.shared.web"
            @services="navigate('shared.web')"
            :providers="draft.shared.providers"
            :initial-channel="channel"
            :effort-options="effortOptions"
            :flush="editor.flush"
            @checked="refreshAfterProbe"
          >
            <PromptSettingsEditor
              v-if="advisorPrompt"
              :definition="advisorPrompt"
              :editor="editor"
              :guard="guard"
            />
          </AdvisorSettings>
          <CardAuthorSettings
            v-else-if="page.key === 'editor'"
            :value="draft.shared.card_author_config"
            :profile="draft.shared.card_author"
            :providers="draft.shared.providers"
            :services="draft.shared.web"
            :effort-options="effortOptions"
            :flush="editor.flush"
            @checked="refreshAfterProbe"
            @services="navigate('shared.web')"
          >
            <PromptSettingsEditor
              v-if="cardAuthorPrompt"
              :definition="cardAuthorPrompt"
              :editor="editor"
              :guard="guard"
            />
          </CardAuthorSettings>
          <template v-else-if="page.key === 'web'">
            <h4>模型请求方式</h4>
            <SettingsFields :value="draft.shared" :fields="requestFields" layout="rows" />
            <h4>代理</h4>
            <SettingsFields :value="draft.shared" :fields="proxyFields" layout="rows" />
            <h4>联网服务</h4>
            <WebSettings :value="draft.shared.web" />
          </template>
          <LocalResourceSettings
            v-else-if="page.key === 'local'"
            :settings="draft.shared"
            :flush="editor.flush"
            :warmup="warmup"
          />
          <template v-else-if="page.key === 'storage'">
            <DataTransferSettings
              :flush="editor.flush"
              @busy="transferBusy = $event"
              @imported="afterImport"
              @open-session="emit('openSession', $event)"
            />
            <h4>存储与清理</h4>
            <StorageSettings :settings="draft.shared" />
          </template>
          <template v-else-if="page.key === 'misc'">
            <h4>输入</h4>
            <SettingsFields :value="draft.shared" :fields="shortcutFields" layout="rows" />
            <h4>实验性功能</h4>
            <SettingsFields :value="draft.shared" :fields="experimentFields" layout="rows" />
            <KeminiSettings
              :value="draft.shared"
              :saved="saved!.shared"
              :text-value="(path) => editor.textValue('shared', path)"
              :edit-text="(path, text) => editor.editText('shared', path, text)"
              :save-text="(path) => editor.saveText('shared', path)"
              :text-busy="textBusy"
              :text-errors="textErrors"
              :guard="guard"
            />
            <h4>调试</h4>
            <SettingsFields :value="draft.shared" :fields="debugFields" layout="rows" />
            <FailedRequestLogs />
          </template>
          <PreferencePresetSettings
            v-else-if="page.key === 'preference_presets'"
            :presets="editor.draftValue('shared', 'preference_presets')"
            :dirty="textDirty.includes('shared.preference_presets')"
            :saving="textBusy !== ''"
            :error="textErrors['shared.preference_presets']"
            @update:presets="editor.editValue('shared', 'preference_presets', $event)"
            @save="savePresets"
          />
        </section>
        <div v-if="error" class="settings-error" role="alert">
          <span>{{ error }}</span
          ><button :disabled="busy" @click="editor.flush().catch(() => undefined)">重试保存</button>
        </div>
      </div>
    </div>
  </div>
  <Teleport to="body"
    ><div v-if="pendingAction" class="settings-confirm-backdrop">
      <section
        class="settings-confirm"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="unsaved-title"
        @keydown.esc.prevent.stop="continueEditing"
        @keydown.tab="trap"
      >
        <h2 id="unsaved-title">有修改尚未保存</h2>
        <p>离开会放弃尚未保存的修改。</p>
        <div>
          <button ref="continueButton" class="primary" @click="continueEditing">继续编辑</button
          ><button @click="discardAndContinue">放弃修改</button>
        </div>
      </section>
    </div></Teleport
  >
</template>
