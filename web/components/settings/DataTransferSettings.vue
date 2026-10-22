<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { api } from '../../lib/api';
import {
  downloadData,
  readBrowserPreferences,
  type TransferCatalog,
  type TransferPreview,
  type TransferResult,
} from '../../lib/dataTransfer';

const props = defineProps<{ flush: () => Promise<unknown> }>();
const emit = defineEmits<{
  imported: [result: TransferResult];
  openSession: [id: string];
  busy: [value: boolean];
}>();
type Group = keyof TransferCatalog;
const groups: { key: Group; label: string; unit: string }[] = [
  { key: 'personas', label: '用户角色', unit: '个' },
  { key: 'worldbooks', label: '世界书', unit: '本' },
  { key: 'cards', label: '角色卡', unit: '张' },
  { key: 'sessions', label: '对话', unit: '个' },
];
const catalog = ref<TransferCatalog>({ personas: [], worldbooks: [], cards: [], sessions: [] });
const selected = ref<Record<Group, string[]>>({ personas: [], worldbooks: [], cards: [], sessions: [] });
const enabled = ref<Record<Group, boolean>>({
  personas: true,
  worldbooks: true,
  cards: true,
  sessions: true,
});
const settings = ref(true),
  includeKeys = ref(false),
  restoreSettings = ref(false);
const loading = ref(true),
  loaded = ref(false),
  busy = ref(''),
  error = ref(''),
  notice = ref('');
const expanded = ref<Group | null>(null),
  query = ref('');
const fileInput = ref<HTMLInputElement>(),
  file = ref<File | null>(null);
const preview = ref<TransferPreview | null>(null),
  imported = ref<TransferResult | null>(null);
const exportable = computed(
  () => settings.value || groups.some(({ key }) => enabled.value[key] && selected.value[key].length),
);
// Card defaults travel only with the personas and worldbooks exported beside them.
const partialDefaults = computed(() => {
  if (!enabled.value.cards) return [];
  const picked = (key: Group) => new Set(enabled.value[key] ? selected.value[key] : []);
  const personas = picked('personas'),
    worldbooks = picked('worldbooks'),
    cards = picked('cards');
  return catalog.value.cards
    .filter(
      (card) =>
        cards.has(card.id) &&
        card.defaults &&
        ((card.defaults.persona_id && !personas.has(card.defaults.persona_id)) ||
          card.defaults.worldbook_ids.some((id) => !worldbooks.has(id))),
    )
    .map((card) => card.name);
});
const importable = computed(
  () => preview.value && (restoreSettings.value || groups.some(({ key }) => preview.value![key].length)),
);
function setBusy(value: string) {
  busy.value = value;
  emit('busy', !!value);
}
async function refresh() {
  loading.value = true;
  try {
    catalog.value = await api('/data-transfer');
    for (const { key } of groups) selected.value[key] = catalog.value[key].map((item) => item.id);
    loaded.value = true;
  } catch (e) {
    error.value = String(e).replace(/^Error: /, '');
  } finally {
    loading.value = false;
  }
}
function count(key: Group, unit: string) {
  const total = catalog.value[key].length,
    picked = selected.value[key].length;
  return `${picked === total ? '全部' : '已选'} ${picked} ${unit}${picked !== total ? ` / 共 ${total} ${unit}` : ''}`;
}
function expand(key: Group) {
  expanded.value = expanded.value === key ? null : key;
  query.value = '';
}
function items(key: Group) {
  return catalog.value[key].filter((item) =>
    item.name.toLowerCase().includes(query.value.trim().toLowerCase()),
  );
}
async function download() {
  error.value = notice.value = '';
  setBusy('export');
  try {
    await props.flush();
    await downloadData({
      settings: settings.value,
      include_api_keys: settings.value && includeKeys.value,
      browser_preferences: settings.value ? readBrowserPreferences() : {},
      ...Object.fromEntries(groups.map(({ key }) => [key, enabled.value[key] ? selected.value[key] : []])),
    });
    notice.value = '数据包已下载，可在本页重新导入。';
  } catch (e) {
    error.value = String(e).replace(/^Error: /, '');
  } finally {
    setBusy('');
  }
}
async function chooseFile(event: Event) {
  const chosen = (event.target as HTMLInputElement).files?.[0];
  if (!chosen) return;
  file.value = chosen;
  preview.value = imported.value = null;
  restoreSettings.value = false;
  error.value = notice.value = '';
  if (chosen.size > 256 * 1024 * 1024) {
    error.value = '数据包超过 256 MiB，请分批导入。';
    return;
  }
  setBusy('preview');
  try {
    await props.flush();
    const form = new FormData();
    form.append('file', chosen);
    preview.value = await api('/data-transfer/preview', form);
  } catch (e) {
    error.value = String(e).replace(/^Error: /, '');
  } finally {
    setBusy('');
  }
}
async function importData() {
  if (!file.value || !preview.value) return;
  error.value = notice.value = '';
  setBusy('import');
  try {
    await props.flush();
    const form = new FormData();
    form.append('file', file.value);
    form.append(
      'options',
      JSON.stringify({
        restore_settings: restoreSettings.value,
        sha256: preview.value.sha256,
        settings_revisions: preview.value.settings_revisions,
      }),
    );
    const result: TransferResult = await api('/data-transfer/import', form);
    imported.value = result;
    preview.value = null;
    notice.value = `导入完成：${result.personas.length} 个用户角色、${result.worldbooks.length} 本世界书、${result.cards.length} 张角色卡、${result.sessions.length} 个对话。${result.settings_restored ? '个人设置已恢复。' : ''}`;
    emit('imported', result);
    await refresh();
  } catch (e) {
    error.value = String(e).replace(/^Error: /, '');
  } finally {
    setBusy('');
  }
}
onMounted(refresh);
</script>

<template>
  <div class="data-transfer" :aria-busy="!!busy">
    <section aria-labelledby="data-export-heading">
      <h4 id="data-export-heading">导出数据</h4>
      <p class="transfer-description">选择需要备份的内容，对话会自动包含继续游玩所需的角色资源。</p>
      <p v-if="loading">正在读取数据…</p>
      <div v-else-if="loaded" class="transfer-rows">
        <div class="transfer-row">
          <label class="transfer-choice"
            ><input v-model="settings" type="checkbox" :disabled="!!busy" />个人设置</label
          >
          <span class="transfer-row-note">模型连接、提示词、常用偏好与阅读偏好</span>
        </div>
        <div v-for="group in groups" :key="group.key" class="transfer-group">
          <div class="transfer-row">
            <label class="transfer-choice"
              ><input
                v-model="enabled[group.key]"
                type="checkbox"
                :disabled="!!busy || !catalog[group.key].length"
              />{{ group.label }}</label
            >
            <span class="transfer-row-note"
              >{{ count(group.key, group.unit)
              }}{{ group.key === 'sessions' ? ' · 含消息、记忆、变量与分支' : '' }}</span
            >
            <button
              :disabled="!!busy || !enabled[group.key] || !catalog[group.key].length"
              :aria-label="`选择${group.label}条目`"
              :aria-expanded="expanded === group.key"
              @click="expand(group.key)"
            >
              选择条目
            </button>
          </div>
          <div v-if="expanded === group.key" class="transfer-picker">
            <div class="transfer-picker-tools">
              <input
                v-model="query"
                type="search"
                :placeholder="`搜索${group.label}…`"
                :aria-label="`搜索${group.label}`"
                :disabled="!!busy"
              />
              <button
                :disabled="!!busy"
                @click="selected[group.key] = catalog[group.key].map((item) => item.id)"
              >
                全选
              </button>
              <button :disabled="!!busy" @click="selected[group.key] = []">全不选</button>
            </div>
            <div class="transfer-picker-list">
              <label v-for="item in items(group.key)" :key="item.id" class="transfer-choice"
                ><input
                  v-model="selected[group.key]"
                  type="checkbox"
                  :value="item.id"
                  :disabled="!!busy"
                /><span>{{ item.name }}</span
                ><small v-if="item.agent_mode">{{
                  item.agent_mode === 'full_agent' ? 'Full Agent' : 'Half Agent'
                }}</small></label
              >
              <p v-if="!items(group.key).length" class="muted">没有匹配的条目</p>
            </div>
          </div>
        </div>
      </div>
      <p v-if="partialDefaults.length" class="transfer-description">
        {{
          partialDefaults.join('、')
        }}的新建对话默认项引用了未选中的用户角色或世界书，导出时省略这部分默认项。
      </p>
      <label class="transfer-choice transfer-secrets"
        ><input v-model="includeKeys" type="checkbox" :disabled="!!busy || !settings" />包含 API
        Key（文件中将以明文保存）</label
      >
      <div class="transfer-actions">
        <button class="primary" :disabled="!!busy || !loaded || !exportable" @click="download">
          {{ busy === 'export' ? '正在导出…' : '导出所选数据' }}</button
        ><span class="muted">下载 ZIP 数据包</span>
      </div>
    </section>
    <section class="transfer-import" aria-labelledby="data-import-heading">
      <h4 id="data-import-heading">导入数据</h4>
      <p class="transfer-description">选择本软件导出的 ZIP 数据包，预览后导入。</p>
      <div class="transfer-actions">
        <input
          ref="fileInput"
          class="transfer-file-input"
          type="file"
          accept=".zip,application/zip"
          aria-label="导入数据包"
          :disabled="!!busy"
          @change="chooseFile"
        />
        <button :disabled="!!busy" @click="fileInput?.click()">选择数据包</button
        ><span class="transfer-filename">{{ file?.name || '未选择文件' }}</span>
      </div>
      <p class="transfer-description">
        用户角色、世界书、角色卡和对话将作为新副本导入。恢复个人设置前需确认。
      </p>
      <p v-if="busy === 'preview'" role="status">正在校验数据包…</p>
      <div v-if="preview" class="transfer-preview">
        <h4>数据包预览</h4>
        <p class="muted">
          个人设置：{{ preview.settings ? '已包含' : '未包含' }} ·
          {{ preview.includes_api_keys ? '包含 API Key' : '不含 API Key' }}
        </p>
        <details v-for="group in groups" :key="group.key">
          <summary>{{ group.label }} · {{ preview[group.key].length }} {{ group.unit }}</summary>
          <ul v-if="preview[group.key].length">
            <li v-for="item in preview[group.key]" :key="item.id">
              {{ item.name
              }}<span v-if="item.messages !== undefined" class="muted"> · {{ item.messages }} 条消息</span>
            </li>
          </ul>
        </details>
        <label v-if="preview.settings" class="transfer-choice transfer-restore"
          ><input
            v-model="restoreSettings"
            type="checkbox"
            :disabled="!!busy"
          />恢复包内个人设置，替换当前设置与阅读偏好</label
        >
        <p v-if="preview.settings && !preview.includes_api_keys" class="muted">
          同一提供商的本地 API Key 会保留；其他连接需重新填写。
        </p>
        <button class="primary" :disabled="!!busy || !importable" @click="importData">
          {{ busy === 'import' ? '正在导入…' : '确认导入' }}
        </button>
      </div>
    </section>
    <p v-if="error" class="transfer-error" role="alert">
      {{ error }} <button v-if="!loaded" :disabled="loading" @click="refresh">重新读取</button>
    </p>
    <div v-if="notice" class="transfer-success" role="status">
      <p>{{ notice }}</p>
      <button
        v-if="imported?.sessions.length"
        :disabled="!!busy"
        @click="emit('openSession', imported.sessions[0]!)"
      >
        打开导入的对话，继续故事
      </button>
    </div>
  </div>
</template>
