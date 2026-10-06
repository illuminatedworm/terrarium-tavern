<script setup lang="ts">
import { ref, watch } from 'vue';
import { api } from '../../lib/api';

const props = defineProps<{
  session: any;
  disabled?: boolean;
  updateVariables: boolean;
  allowContentEdit: boolean;
}>();
const emit = defineEmits<{ saved: [session: any] }>();
const tools = ref<any[]>([]);
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const resultLabels: Record<string, string> = {
  include_ref: '返回编号',
  include_title: '返回标题',
  include_preview: '返回摘要',
  include_metadata: '返回附加信息',
};
const metadataLabels: Record<string, string> = {
  read_history: '返回编号和角色',
  read_note: '返回类型、来源和过时状态',
  read_state: '返回路径',
  read_character: '返回字段名',
};
const readDependencies: Record<string, string> = {
  patch_state: 'read_state',
};
function missingRead(name: string) {
  const read = readDependencies[name];
  return read && props.session.disabled_tools?.includes(read) ? read : '';
}
function resultLabel(name: string, key: string) {
  return key === 'include_metadata' ? metadataLabels[name] || resultLabels[key] : resultLabels[key];
}
let requestId = 0;
watch(
  () => [props.session.id, props.session.revision, props.updateVariables, props.allowContentEdit],
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
  const sid = props.session.id;
  const tool = tools.value.find((item) => item.name === name);
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
    <h2>工具</h2>
    <p class="inspector-note">
      开关自动保存，仅影响当前会话，从下一回合生效。展开查看发送给模型的英文描述和参数。
    </p>
    <p v-if="loading" role="status">正在加载工具…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <article v-for="tool in tools" :key="tool.name" class="tool-card">
      <label class="tool-toggle">
        <input
          type="checkbox"
          :checked="tool.enabled"
          :aria-label="`启用 ${tool.name}`"
          :disabled="disabled || loading || saving"
          @change="toggle(tool.name, !tool.enabled)"
        />
        <code>{{ tool.name }}</code>
      </label>
      <p class="tool-status">
        {{
          !tool.enabled
            ? '已关闭'
            : tool.available
              ? '本轮可用'
              : missingRead(tool.name)
                ? '已启用 · 读取工具已关闭'
                : '已启用 · 本轮尚未授权'
        }}
      </p>
      <p v-if="tool.enabled && missingRead(tool.name)" class="inspector-note">
        请先启用 {{ missingRead(tool.name) }}，完成本回合当前窗口的读取后才能修改。
      </p>
      <p v-else-if="tool.name === 'patch_state' && tool.enabled && !tool.available" class="inspector-note">
        工具定义保持稳定；勾选本轮“更新变量”后才允许执行，且本轮须成功调用一次。
      </p>
      <p v-else-if="tool.name === 'revise_content' && tool.enabled && !tool.available" class="inspector-note">
        本轮允许修改设定后可用。
      </p>
      <p v-if="tool.name === 'revise_content' && tool.available" class="inspector-note">
        <template v-if="!tool.edit_capabilities?.character">人物设定修改需启用 read_character。 </template>
        <template v-if="!tool.edit_capabilities?.lore_update_remove">
          已有世界书条目的修改和删除需启用 read_lore_entry。
        </template>
        新增普通世界书条目无需读取整本世界书。
      </p>
      <p v-if="tool.name === 'new_context' && !tool.enabled" class="inspector-note">
        上下文满时，需要在场景页手动换窗或重新启用此工具。
      </p>
      <fieldset v-if="Object.keys(tool.result_options || {}).length" class="tool-result-options">
        <legend>返回内容</legend>
        <label v-for="(enabled, key) in tool.result_options" :key="key">
          <input
            type="checkbox"
            :checked="enabled"
            :aria-label="`${tool.name} ${resultLabel(tool.name, String(key))}`"
            :disabled="disabled || loading || saving || !tool.enabled"
            @change="toggleResultOption(tool.name, String(key), !enabled)"
          />
          {{ resultLabel(tool.name, String(key)) }}
        </label>
        <small v-if="tool.name === 'recall_lore'"
          >搜索直接返回命中条目的完整正文；编号和标题为可选附加信息。</small
        >
      </fieldset>
      <details>
        <summary>描述与参数</summary>
        <p class="tool-description" lang="en">{{ tool.description }}</p>
        <pre lang="en">{{ JSON.stringify(tool.parameters, null, 2) }}</pre>
      </details>
    </article>
  </section>
</template>

<style scoped>
.tool-card {
  padding: 12px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.tool-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tool-toggle code {
  overflow-wrap: anywhere;
}
.tool-status {
  font-size: 12px;
  opacity: 0.75;
  margin: 6px 0;
}
.tool-description,
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.tool-result-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin: 10px 0;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
}
.tool-result-options legend {
  padding: 0 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
.tool-result-options label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
}
.tool-result-options small {
  flex-basis: 100%;
  color: var(--text-muted);
}
pre {
  font-size: 12px;
}
summary {
  cursor: pointer;
}
</style>
