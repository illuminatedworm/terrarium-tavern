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
        {{ !tool.enabled ? '已关闭' : tool.available ? '本轮可用' : '已启用 · 本轮尚未授权' }}
      </p>
      <p v-if="tool.name === 'patch_state' && tool.enabled && !tool.available" class="inspector-note">
        开启本轮“更新变量”后可用。
      </p>
      <p v-if="tool.name === 'revise_content' && tool.enabled && !tool.available" class="inspector-note">
        本轮允许修改设定后可用。
      </p>
      <p v-if="tool.name === 'new_context' && !tool.enabled" class="inspector-note">
        上下文满时，需要在场景页手动换窗或重新启用此工具。
      </p>
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
pre {
  font-size: 12px;
}
summary {
  cursor: pointer;
}
</style>
