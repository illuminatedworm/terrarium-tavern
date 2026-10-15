<script setup lang="ts">
import { computed, nextTick, onMounted, provide, ref } from 'vue';
import { api } from '../../lib/api';
import { useSettingsEditor } from '../../composables/useSettingsEditor';
import {
  getPath,
  type SettingsDocument,
  type SettingsPage,
  type SettingsScope,
  type SettingsObject,
} from '../../lib/settingsDocument';
import {
  loreFields,
  retrievalFields,
  retrievalAdvanced,
  fullFields,
  historyFields,
  memoryFields,
  networkFields,
} from '../../lib/settingsFields';
import SettingsFields from './SettingsFields.vue';
import PolicySelect from './PolicySelect.vue';
import ModelProfileEditor from './ModelProfileEditor.vue';
import ProviderSettings from './ProviderSettings.vue';
import LocalResourceSettings from './LocalResourceSettings.vue';
import StorageSettings from './StorageSettings.vue';
import DataTransferSettings from './DataTransferSettings.vue';
import type { TransferResult } from '../../lib/dataTransfer';
import SavedText from './SavedText.vue';
import SaveStatus from './SaveStatus.vue';
import RecipeSettings from './RecipeSettings.vue';
import PreferencePresetSettings from './PreferencePresetSettings.vue';
import AdvisorSettings from './AdvisorSettings.vue';
import WebSettings from './WebSettings.vue';
import WebModeSettings from './WebModeSettings.vue';
import ChannelSwitch from '../common/ChannelSwitch.vue';
import { blankChannelProfile, channelProviders, profileKey, type Channel } from '../../lib/channels';
const props = defineProps<{
  initialMode?: string;
  initialPage?: string;
  initialChannel?: Channel;
  effortOptions: Array<{ value: string; label: string }>;
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
const providersForChannel = computed(() =>
  channelProviders(draft.value?.shared.providers || [], channel.value),
);
function createChannelProfile(role: string) {
  if (!draft.value) return;
  const owner = role === 'full_agent' ? draft.value.full_agent : draft.value.half_agent;
  const base = role === 'full_agent' ? owner.model : owner[role];
  owner[profileKey(role, channel.value)] = blankChannelProfile(
    base,
    draft.value.shared.providers,
    channel.value,
  );
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
const scope = computed<SettingsScope>(() => page.value?.scope || 'full_agent');
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
  { scope: 'full_agent', label: 'full agent' },
  { scope: 'half_agent', label: 'half agent' },
  { scope: 'shared', label: '通用' },
];
const groupLabel = (s: string) => groups.find((g) => g.scope === s)?.label || s;
const prompts = computed(() => draft.value?.prompt_definitions.filter((p) => p.scope === scope.value) || []);
const prompt = computed(() => prompts.value.find((p) => p.path === promptPath.value) || prompts.value[0]);
const advisorPrompt = computed(() =>
  draft.value?.prompt_definitions.find((p) => p.scope === 'shared' && p.path === 'advisor.system_prompt'),
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
async function saveRecipeText(path: string) {
  try {
    await editor.flush();
    await editor.saveText('half_agent', path);
  } catch {
    /* Keep the error and text draft. */
  }
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
        ? p.scope === 'full_agent'
          ? fullFields
          : [...historyFields, ...memoryFields]
        : p.key === 'retrieval'
          ? [...loreFields, ...retrievalFields, ...retrievalAdvanced]
          : p.key === 'network'
            ? networkFields
            : [];
    for (const f of fields) entries.push({ page: p, label: f.label, path: f.path });
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
  pageId.value = draft.value?.pages.some((p) => p.id === previous)
    ? previous!
    : `${props.initialMode === 'half_agent' ? 'half_agent' : 'full_agent'}.model`;
});
</script>
<template>
  <div class="settings-shell" :inert="pendingAction !== null">
    <header class="settings-header">
      <h2>设置</h2>
      <div class="settings-search">
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
          <h3>{{ group.label }}</h3>
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
          <h3>{{ groupLabel(scope) }} · {{ page?.label }}</h3>
          <button
            v-if="page && page.key !== 'data_transfer'"
            :disabled="busy || !editor.pageChanged(page)"
            @click="editor.undo(page)"
          >
            撤回本页修改
          </button>
        </div>
        <p class="settings-scope-note">
          {{
            page?.key === 'data_transfer'
              ? '备份个人数据，或导入后继续故事。'
              : scope === 'shared'
                ? '这些资源由两种模式共用。'
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
            <div :class="['settings-profiles', { 'dual-profiles': scope === 'half_agent' }]">
              <ModelProfileEditor
                v-if="scope === 'full_agent' && draft.full_agent[profileKey('full_agent', channel)]"
                :key="'full-' + channel"
                :profile="draft.full_agent[profileKey('full_agent', channel)]"
                :providers="providersForChannel"
                :channel="channel"
                role="full_agent"
                label="模型连接"
                :effort-options="effortOptions"
                :flush="editor.flush"
                @checked="refreshAfterProbe"
              />
              <div v-else-if="scope === 'full_agent'" class="channel-empty">
                <p>此渠道的 Full Agent 模型尚未配置。</p>
                <button :disabled="!providersForChannel.length" @click="createChannelProfile('full_agent')">
                  配置模型连接
                </button>
                <button class="quiet" @click="navigate('shared.providers')">管理本渠道供应商</button>
              </div>
              <template v-else>
                <template v-for="role in ['foreground', 'background'] as const" :key="role + channel">
                  <ModelProfileEditor
                    v-if="draft.half_agent[profileKey(role, channel)]"
                    :profile="draft.half_agent[profileKey(role, channel)]"
                    :providers="providersForChannel"
                    :channel="channel"
                    :role="role"
                    :label="role === 'foreground' ? '正文生成' : '后台整理'"
                    :effort-options="effortOptions"
                    :flush="editor.flush"
                    @checked="refreshAfterProbe"
                  />
                  <div v-else class="channel-empty">
                    <p>{{ role === 'foreground' ? '正文生成' : '后台整理' }}尚未配置。</p>
                    <button :disabled="!providersForChannel.length" @click="createChannelProfile(role)">
                      配置模型连接
                    </button>
                    <button class="quiet" @click="navigate('shared.providers')">管理本渠道供应商</button>
                  </div>
                </template>
              </template>
            </div>
            <div class="settings-policy" data-setting-path="policy">
              <PolicySelect v-model="values.policy" global compact />
            </div>
          </template>
          <template v-else-if="page.key === 'retrieval'">
            <h4>世界书注入</h4>
            <SettingsFields :value="values" :fields="loreFields" />
            <SettingsFields
              v-if="scope === 'half_agent'"
              :value="values"
              :fields="[
                {
                  path: 'lore_injection_mode',
                  label: '新对话的注入方案',
                  options: [
                    { value: 'conversation', label: '随对话滚动' },
                    { value: 'st_top', label: '按角色卡位置注入' },
                  ],
                },
              ]"
            />
            <p class="muted">
              数量和字符额度是上限，不以弱匹配补满。常驻条目仍受总上下文窗口限制。{{
                scope === 'full_agent' ? '此模式固定随对话注入。' : '注入方案仅影响新对话。'
              }}
            </p>
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
            <SettingsFields
              v-if="scope === 'full_agent'"
              class="full-agent-fields"
              :value="values"
              :fields="fullFields"
            />
            <template v-else
              ><h4>历史窗口</h4>
              <p class="muted">
                以下裁剪设置仅适用于本地滚动窗口；云端 compact 按 Writer 的上下文 token 上限触发。
              </p>
              <SettingsFields :value="values" :fields="historyFields" />
              <h4>Tracker 与记忆整理</h4>
              <SettingsFields :value="values" :fields="memoryFields"
            /></template>
            <p class="muted">
              {{
                scope === 'full_agent'
                  ? '模型通过工具查阅资料、保存笔记并主动换窗。'
                  : '周期记忆在正文后整理，可与本轮变量维护合并。消息退出前仍须补齐记忆覆盖；关闭裁剪仍执行周期整理。'
              }}取消或拒绝审核不会提交本轮变更。
            </p>
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
              <div class="settings-prompt-editor">
                <p v-if="prompt.path === 'system_prompt'" class="settings-prompt-warning">
                  除非你知道自己在做什么，否则不要修改。
                </p>
                <SavedText
                  :key="prompt.path"
                  :label="prompt.label"
                  :model-value="editor.textValue(scope, prompt.path)"
                  :saved-value="getPath(saved![scope], prompt.path)"
                  :saving="textBusy === scope + '.' + prompt.path"
                  :error="textErrors[scope + '.' + prompt.path]"
                  :guard="guard"
                  @update:model-value="editor.editText(scope, prompt.path, $event)"
                  @save="editor.saveText(scope, prompt.path)"
                />
                <p class="muted">{{ prompt.description }}</p>
                <p
                  v-if="
                    scope === 'full_agent' &&
                    prompt.path === 'system_prompt' &&
                    /handoff|new_context|tavern_memory/.test(editor.textValue(scope, prompt.path))
                  "
                  class="muted"
                >
                  此自定义主规则包含本地换窗要求；使用云端 compact
                  前请移除其中的换窗命令，或恢复通用默认文本。
                </p>
                <button @click="editor.editText(scope, prompt.path, prompt.default)">恢复默认文本</button>
              </div>
            </div>
          </template>
          <RecipeSettings
            v-else-if="page.key === 'recipe'"
            :recipe="draft.half_agent.context_recipe"
            :saved="saved!.half_agent.context_recipe"
            :guard="guard"
            :text-value="(path) => editor.textValue('half_agent', path)"
            :edit-text="(path, value) => editor.editText('half_agent', path, value)"
            :save-text="saveRecipeText"
            :text-busy="textBusy"
            :text-errors="textErrors"
          />
          <ProviderSettings
            v-else-if="page.key === 'providers'"
            v-model:providers="draft.shared.providers"
            :initial-channel="channel"
            @channel="channel = $event"
            :flush="editor.flush"
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
            <SavedText
              :model-value="editor.textValue('shared', 'advisor.system_prompt')"
              :saved-value="saved!.shared.advisor.system_prompt"
              label="参谋 agent 系统提示词"
              :guard="guard"
              :saving="textBusy === 'shared.advisor.system_prompt'"
              :error="textErrors['shared.advisor.system_prompt']"
              @update:model-value="editor.editText('shared', 'advisor.system_prompt', $event)"
              @save="editor.saveText('shared', 'advisor.system_prompt')"
            />
            <button
              v-if="advisorPrompt"
              @click="editor.editText('shared', 'advisor.system_prompt', advisorPrompt.default)"
            >
              恢复默认文本
            </button>
          </AdvisorSettings>
          <template v-else-if="page.key === 'editor'">
            <WebModeSettings
              :value="draft.shared.card_editor_config.web"
              :services="draft.shared.web"
              @services="navigate('shared.web')"
            />
            <ModelProfileEditor
              :profile="draft.shared.card_editor"
              :providers="draft.shared.providers"
              role="card_editor"
              label="角色卡编辑 agent 模型"
              :effort-options="effortOptions"
              :flush="editor.flush"
              @checked="refreshAfterProbe"
            />
            <fieldset>
              <legend>制卡运行</legend>
              <label
                >每次发送或继续的模型请求上限<input
                  v-model.number="draft.shared.card_editor_config.max_steps"
                  type="number"
                  min="2"
                  max="100"
              /></label>
              <label
                >上下文提醒比例<input
                  v-model.number="draft.shared.card_editor_config.pressure_ratio"
                  type="number"
                  min="0.25"
                  max="0.9"
                  step="0.05"
              /></label>
              <label
                >创作进度上限（tokens）<input
                  v-model.number="draft.shared.card_editor_config.handoff_tokens"
                  type="number"
                  min="128"
                  max="16384"
              /></label>
            </fieldset>
          </template>
          <WebSettings v-else-if="page.key === 'web'" :value="draft.shared.web" />
          <LocalResourceSettings
            v-else-if="page.key === 'local'"
            :settings="draft.shared"
            :flush="editor.flush"
          />
          <SettingsFields v-else-if="page.key === 'network'" :value="draft.shared" :fields="networkFields" />
          <StorageSettings v-else-if="page.key === 'storage'" :settings="draft.shared" />
          <DataTransferSettings
            v-else-if="page.key === 'data_transfer'"
            :flush="editor.flush"
            @busy="transferBusy = $event"
            @imported="afterImport"
            @open-session="emit('openSession', $event)"
          />
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
