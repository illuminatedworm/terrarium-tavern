<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../lib/api';
import FieldHelp from '../common/FieldHelp.vue';
import Icon from '../common/Icon.vue';

const props = defineProps<{
  session: any;
  disabled?: boolean;
  updateVariables: boolean;
  allowContentEdit: boolean;
}>();
const emit = defineEmits<{ saved: [session: any]; navigate: [page: string] }>();
const tools = ref<any[]>([]);
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const resultLabels: Record<string, string> = {
  include_ref: '编号',
  include_title: '标题',
  include_preview: '摘要',
  include_metadata: '附加信息',
};
const metadataLabels: Record<string, string> = {
  read_history: '编号和角色',
  read_note: '类型和原文来源',
  read_state: '路径',
  read_character: '字段名',
};
function resultLabel(name: string, key: string) {
  return key === 'include_metadata' ? metadataLabels[name] || resultLabels[key] : resultLabels[key];
}
// Display names and groups are UI only; the model always sees the English definitions.
const groups: Array<{ label: string; tools: Record<string, string> }> = [
  {
    label: '世界书',
    tools: {
      find_lore_for_edit: '查找可编辑设定',
      read_lore_entry: '读取设定条目',
      recall_lore: '召回设定',
      revise_content: '修改设定',
    },
  },
  { label: '历史', tools: { search_history: '搜索历史', read_history: '读取历史' } },
  {
    label: '笔记与规划',
    tools: {
      search_notes: '搜索笔记',
      read_note: '读取笔记',
      write_note: '写入笔记',
      archive_note: '归档笔记',
      write_plot: '写剧情规划',
    },
  },
  { label: '窗口', tools: { write_handoff: '写交接笔记', new_context: '开新窗口' } },
  {
    label: '变量与角色',
    tools: { read_state: '读取变量', patch_state: '修改变量', read_character: '读取角色' },
  },
  { label: '检索', tools: { set_retrieval_query: '设置检索线索' } },
  {
    label: '原著',
    tools: {
      start_novel_research: '委派原著调查',
      list_novel_research: '查看调查列表',
      message_novel_research: '追问调查者',
      wait_novel_research: '等待调查结果',
      resume_novel_research: '恢复调查',
      destroy_novel_research: '销毁调查者',
    },
  },
  { label: '正文与图形', tools: { submit_story: '提交正文', present_ui: '交互图形' } },
];
const grouped = computed(() => {
  const byName = new Map(tools.value.map((tool) => [tool.name, tool]));
  const known = new Set(groups.flatMap((group) => Object.keys(group.tools)));
  const result = groups
    .map((group) => ({
      label: group.label,
      tools: Object.entries(group.tools).flatMap(([name, title]) =>
        byName.has(name) ? [{ tool: byName.get(name), title }] : [],
      ),
    }))
    .filter((group) => group.tools.length);
  // A tool added by the backend before this map learns its name still shows up.
  const other = tools.value.filter((tool) => !known.has(tool.name));
  if (other.length) result.push({ label: '其他', tools: other.map((tool) => ({ tool, title: tool.name })) });
  return result;
});
const enabledCount = computed(() => tools.value.filter((tool) => tool.enabled).length);
const unauthorized = computed(() => tools.value.filter((tool) => tool.enabled && !tool.available).length);
const controlSource: Record<string, { label: string; page?: string }> = {
  plot_enabled: { label: '由场景页“剧情规划”控制', page: 'scene' },
  novel_enabled: { label: '由原著页授权控制', page: 'novel' },
  story_protocol: { label: '始终启用' },
};
const open = ref(new Set<string>());
const definitions = ref(new Set<string>());
function flip(set: Set<string>, name: string) {
  if (!set.delete(name)) set.add(name);
}
let requestId = 0;
watch(
  () => [
    props.session.id,
    props.session.revision,
    props.session.novel_enabled,
    props.updateVariables,
    props.allowContentEdit,
  ],
  async () => {
    const current = ++requestId;
    loading.value = true;
    error.value = '';
    try {
      const query = new URLSearchParams({
        update_variables: String(props.updateVariables),
        allow_content_edit: String(props.allowContentEdit),
      });
      const result = await api(`/sessions/${props.session.id}/full-agent/tools?${query}`);
      if (current === requestId) tools.value = result.tools;
    } catch (e: any) {
      if (current === requestId) error.value = e.message;
    } finally {
      if (current === requestId) loading.value = false;
    }
  },
  { immediate: true },
);
async function toggle(name: string, enabled: boolean) {
  const tool = tools.value.find((item) => item.name === name);
  if (tool?.controlled_by) return;
  const sid = props.session.id;
  if (tool) tool.enabled = enabled;
  const disabled = new Set<string>(props.session.disabled_tools || []);
  if (enabled) disabled.delete(name);
  else disabled.add(name);
  saving.value = true;
  error.value = '';
  try {
    const updated = await api(
      `/sessions/${sid}/full-agent/tools`,
      {
        expected_revision: props.session.revision,
        disabled_tools: [...disabled],
        tool_result_options: props.session.tool_result_options || {},
      },
      'PUT',
    );
    if (props.session.id === sid) emit('saved', updated);
  } catch (e: any) {
    if (props.session.id === sid) {
      if (tool) tool.enabled = !enabled;
      error.value = e.message;
    }
  } finally {
    saving.value = false;
  }
}
async function toggleResultOption(name: string, key: string, enabled: boolean) {
  const sid = props.session.id;
  const tool = tools.value.find((item) => item.name === name);
  if (tool) tool.result_options[key] = enabled;
  const options = {
    ...(props.session.tool_result_options || {}),
    [name]: { ...(props.session.tool_result_options?.[name] || {}), [key]: enabled },
  };
  saving.value = true;
  error.value = '';
  try {
    const updated = await api(
      `/sessions/${sid}/full-agent/tools`,
      {
        expected_revision: props.session.revision,
        disabled_tools: props.session.disabled_tools || [],
        tool_result_options: options,
      },
      'PUT',
    );
    if (props.session.id === sid) emit('saved', updated);
  } catch (e: any) {
    if (props.session.id === sid) {
      if (tool) tool.result_options[key] = !enabled;
      error.value = e.message;
    }
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <section class="full-agent-tools" aria-label="Full Agent 工具">
    <header class="tools-head">
      <h2><Icon name="wrench" :size="20" />工具</h2>
      <p v-if="tools.length" class="tools-summary">
        <span
          >启用 <b>{{ enabledCount }} / {{ tools.length }}</b></span
        >
        <span v-if="unauthorized" class="tools-warn">· {{ unauthorized }} 个本轮未授权</span>
        <span>· 下回合生效</span>
        <FieldHelp
          tooltip="开关自动保存，仅影响当前会话，从下一回合生效。展开工具可设置附带返回的内容，并查看发送给模型的英文描述和参数。"
        />
      </p>
    </header>
    <p v-if="loading && !tools.length" role="status" class="state-empty">正在加载工具…</p>
    <p v-if="error" role="alert" class="tools-error">{{ error }}</p>
    <section v-for="group in grouped" :key="group.label" class="tool-group" :aria-label="group.label">
      <h3>
        {{ group.label }}<span>{{ group.tools.length }}</span>
      </h3>
      <div v-for="{ tool, title } in group.tools" :key="tool.name" class="tool-item">
        <div class="tool-row" :class="{ open: open.has(tool.name), off: !tool.enabled }">
          <button
            type="button"
            class="tool-name"
            :aria-expanded="open.has(tool.name)"
            @click="flip(open, tool.name)"
          >
            <Icon name="chevron-right" :size="14" class="tool-chevron" /><span>{{ title }}</span
            ><code>{{ tool.name }}</code>
          </button>
          <span v-if="!tool.enabled" class="tool-tag">已关闭</span>
          <span v-else-if="!tool.available" class="tool-tag warn">本轮未授权</span>
          <span v-else></span>
          <Icon
            v-if="tool.controlled_by"
            name="lock"
            :size="16"
            class="tool-lock"
            :title="controlSource[tool.controlled_by]?.label"
          />
          <input
            v-else
            type="checkbox"
            class="switch"
            role="switch"
            :checked="tool.enabled"
            :aria-label="`启用 ${title}`"
            :disabled="disabled || loading || saving"
            @change="toggle(tool.name, !tool.enabled)"
          />
          <p v-if="tool.controlled_by" class="tool-source">
            <button
              v-if="controlSource[tool.controlled_by]?.page"
              type="button"
              @click="emit('navigate', controlSource[tool.controlled_by].page!)"
            >
              {{ controlSource[tool.controlled_by].label }}<Icon name="arrow-right" :size="12" />
            </button>
            <span v-else>{{ controlSource[tool.controlled_by]?.label || tool.controlled_by }}</span>
          </p>
        </div>
        <div v-if="open.has(tool.name)" class="tool-detail">
          <p v-if="tool.name === 'present_ui'" class="hint">
            开启后允许模型按需生成地图、结构图和关系图。图形随本轮正文保存，旧图保留当时内容；交互仅改变显示或填写输入框，由你发送。
          </p>
          <div v-if="Object.keys(tool.result_options || {}).length" class="tool-options">
            <span>附带返回</span>
            <button
              v-for="(enabled, key) in tool.result_options"
              :key="key"
              type="button"
              class="tool-chip"
              :aria-pressed="enabled"
              :aria-label="`${tool.name} 返回${resultLabel(tool.name, String(key))}`"
              :disabled="disabled || loading || saving || !tool.enabled"
              @click="toggleResultOption(tool.name, String(key), !enabled)"
            >
              {{ resultLabel(tool.name, String(key)) }}
            </button>
          </div>
          <p v-if="tool.name === 'recall_lore'">默认直接返回命中条目的完整正文。</p>
          <p v-if="tool.controlled_by === 'story_protocol'">
            每轮可追加最多三段正文。普通文本回复仅记录为备注。
          </p>
          <p v-if="tool.controlled_by === 'novel_enabled'">主 agent 不能直接读取原文，只接收调查者的报告。</p>
          <p v-if="tool.name === 'patch_state' && tool.enabled && !tool.available">
            工具定义保持稳定；勾选本轮“更新变量”后才允许执行，收尾前须至少成功调用一次。
          </p>
          <p v-else-if="tool.name === 'revise_content' && tool.enabled && !tool.available">
            本轮允许修改设定后可用。
          </p>
          <p v-if="tool.name === 'revise_content' && tool.available">
            当前上下文已有目标的完整最新内容时可直接修改，否则需先读取。新增普通世界书条目无需读取。
          </p>
          <p v-if="tool.name === 'new_context' && !tool.enabled">
            上下文满时，需要在场景页手动换窗或重新启用此工具。
          </p>
          <button
            type="button"
            class="tool-definition-toggle"
            :aria-expanded="definitions.has(tool.name)"
            @click="flip(definitions, tool.name)"
          >
            {{ definitions.has(tool.name) ? '收起发送给模型的定义' : '查看发送给模型的定义' }}
          </button>
          <template v-if="definitions.has(tool.name)">
            <p class="tool-description" lang="en">{{ tool.description }}</p>
            <pre lang="en">{{ JSON.stringify(tool.parameters, null, 2) }}</pre>
          </template>
        </div>
      </div>
    </section>
  </section>
</template>

<style scoped>
.tools-head {
  margin-bottom: 6px;
}
.tools-head h2 {
  margin: 0;
  font-size: var(--fs-xl);
  display: flex;
  align-items: center;
  gap: 9px;
}
.tools-head h2 .icon {
  color: var(--accent);
}
.tools-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin: 6px 0 0 29px;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.tools-summary b {
  color: var(--text-secondary);
  font-weight: 500;
}
.tools-warn {
  color: var(--warning);
}
.tools-error {
  color: var(--danger);
  font-size: var(--fs-sm);
}
/* Group titles stay pinned while their switches scroll beneath them. */
.tool-group h3 {
  position: sticky;
  top: -18px;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  padding: 14px 4px 6px;
  background: linear-gradient(rgb(var(--glass-rgb)) 70%, rgb(var(--glass-rgb) / 0));
  color: var(--text-faint);
  font-size: var(--fs-xs);
  font-weight: 500;
  letter-spacing: 0.06em;
}
:root.no-glass .tool-group h3 {
  background: var(--surface-sidebar);
}
@media (max-width: 620px) {
  .tool-group h3 {
    top: -12px;
  }
}
.tool-group h3 span {
  font-family: var(--font-mono);
}
.tool-group h3::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border-subtle);
}
.tool-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto 34px;
  align-items: center;
  gap: 8px;
  min-height: 38px;
  padding: 2px 4px 2px 0;
  border-radius: var(--radius-xs);
}
.tool-row:hover {
  background: var(--hover-wash);
}
.tool-name {
  display: flex;
  justify-content: flex-start;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  padding: 7px 4px;
  overflow: hidden;
  border: 0;
  background: none;
  color: var(--text-primary);
  text-align: left;
}
.tool-name:hover:not(:disabled) {
  background: none;
}
.tool-name > span {
  font-size: var(--fs-md);
  white-space: nowrap;
}
.tool-name code {
  overflow: hidden;
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tool-row.off .tool-name > span {
  color: var(--text-muted);
}
.tool-chevron {
  flex: none;
  align-self: center;
  color: var(--text-faint);
  transition: transform var(--dur) var(--ease-out);
}
.tool-row.open .tool-chevron {
  transform: rotate(90deg);
  color: var(--accent);
}
.tool-tag {
  padding: 1px 7px;
  border-radius: var(--radius-2xs);
  background: rgb(255 255 255 / 0.06);
  color: var(--text-muted);
  font-size: var(--fs-2xs);
  white-space: nowrap;
}
.tool-tag.warn {
  background: rgb(var(--warning-rgb) / 0.12);
  color: var(--warning);
}
.tool-lock {
  justify-self: center;
  color: var(--text-faint);
}
.tool-source {
  grid-column: 1 / -1;
  margin: -6px 0 4px 26px;
  color: var(--text-faint);
  font-size: var(--fs-xs);
}
.tool-source button {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: var(--fs-xs);
}
.tool-source button:hover:not(:disabled) {
  background: none;
  color: var(--accent-hover);
}
.tool-detail {
  display: grid;
  gap: 8px;
  margin: 0 4px 8px 26px;
  padding: 10px 12px;
  border-left: 2px solid rgb(var(--accent-rgb) / 0.4);
  border-radius: 0 var(--radius-xs) var(--radius-xs) 0;
  background: rgb(255 255 255 / 0.025);
  min-width: 0;
}
.tool-detail p {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.tool-options {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.tool-options > span {
  margin-right: 4px;
}
.tool-chip {
  height: 24px;
  padding: 0 10px;
  border-radius: var(--radius-pill);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
}
.tool-chip[aria-pressed='true'] {
  border-color: rgb(var(--accent-rgb) / 0.4);
  background: var(--accent-soft);
  color: var(--text-strong);
}
.tool-definition-toggle {
  justify-self: start;
  padding: 0;
  border: 0;
  background: none;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
}
.tool-definition-toggle:hover:not(:disabled) {
  background: none;
  color: var(--accent);
}
.tool-detail .tool-description,
.tool-detail pre {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
