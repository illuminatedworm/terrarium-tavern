<script setup lang="ts">
import { computed, getCurrentInstance, inject } from 'vue';
import type { ContextOperations } from '../../composables/useContextOperations';
const props = defineProps<{ session: any; disabled?: boolean }>();
const ops = inject<ContextOperations>('context-operations');
const compact = computed(() => props.session.context_strategy === 'openai_compact');
const half = computed(() => props.session.agent_mode === 'half_agent');
const localLabel = computed(() => (half.value ? '本地滚动窗口' : '本地换窗'));
const working = computed(() => props.disabled || ops?.busy.value);
const groupName = 'context-strategy-' + getCurrentInstance()?.uid;
const usage = computed(() => {
  const rows = ops?.operation.value?.usage || [];
  if (!rows.length) return null;
  const inputs = rows.map((row: any) => row.prompt_tokens ?? row.input_tokens);
  const outputs = rows.map((row: any) => row.completion_tokens ?? row.output_tokens);
  if (![...inputs, ...outputs].every((value) => typeof value === 'number')) return null;
  return {
    input: inputs.reduce((a: number, b: number) => a + b, 0),
    output: outputs.reduce((a: number, b: number) => a + b, 0),
  };
});
</script>
<template>
  <section
    v-if="session.channel_origin === 'chatgpt' && ops"
    class="context-strategy"
    aria-label="上下文方式"
  >
    <h3>上下文方式</h3>
    <div class="context-strategy-options">
      <label
        ><input
          type="radio"
          :name="groupName"
          :checked="!compact"
          :disabled="working"
          @change="ops.start('enable_local')"
        />{{ localLabel }}</label
      >
      <label
        ><input
          type="radio"
          :name="groupName"
          :checked="compact"
          :disabled="working"
          @change="ops.start('enable_compact')"
        />云端 compact</label
      >
    </div>
    <p class="inspector-note">
      {{
        half
          ? compact
            ? 'Writer 历史由云端压缩；Tracker 继续整理记忆、维护变量与设定。'
            : '按完整交流滚动历史，退出前由 Tracker 检查记忆覆盖。'
          : compact
            ? '历史由云端压缩，长期笔记始终保留。'
            : '由 Agent 保存交接笔记并换窗，长期笔记独立保留。'
      }}
    </p>
    <template v-if="compact">
      <p v-if="half" class="inspector-note">
        本地消息上限、移除批量与压力裁剪不生效；记忆周期保持不变。切回本地时会按需补齐记忆覆盖。
      </p>
      <div class="compact-auto-row">
        <label class="compact-auto"
          ><input
            type="checkbox"
            :checked="session.compact_auto"
            :disabled="working"
            @change="ops.automatic(($event.target as HTMLInputElement).checked)"
          /><span>自动压缩</span></label
        >
        <span class="inspector-note" title="用户发起一轮对话时，若上下文超过窗口上限，先压缩再继续本轮。"
          >发送时超出窗口则先压缩</span
        >
      </div>
      <div class="context-actions">
        <button class="primary" :disabled="working" @click="ops.start('compact')">立即压缩</button>
      </div>
    </template>
    <div v-if="ops.operation.value" class="context-operation" role="status" aria-live="polite">
      <template v-if="ops.busy.value">
        <strong>正在整理历史</strong>
        <progress
          :value="ops.operation.value.progress?.processed || 0"
          :max="ops.operation.value.progress?.total || 1"
        />
        <p>
          第 {{ ops.operation.value.progress?.batches || 0 }} 批 · 已处理
          {{ ops.operation.value.progress?.processed || 0 }} 条记录
        </p>
        <button class="quiet" @click="ops.control('cancel')">取消</button>
        <p class="inspector-note">完成前保持当前上下文；关闭面板不会中断整理。</p>
      </template>
      <template v-else-if="['error', 'cancelled', 'interrupted'].includes(ops.operation.value.status)">
        <p>{{ ops.operation.value.error || '整理已中断，原上下文保持不变。' }}</p>
        <button :disabled="disabled" @click="ops.control('resume')">恢复整理</button>
      </template>
      <p v-else-if="ops.operation.value.status === 'completed'">
        {{
          !ops.operation.value.converted && !ops.operation.value.compacted
            ? '上下文方式已切换，完整历史无需转换。'
            : ops.operation.value.kind === 'enable_local'
              ? half
                ? '记忆覆盖已补齐，已切回本地滚动窗口。'
                : '新的交接笔记已保存，已切回本地换窗。'
              : ops.operation.value.compacted
                ? '云端上下文已就绪。'
                : '上下文已就绪，本次未发生压缩。'
        }}
      </p>
    </div>
    <p v-if="ops.error.value" role="alert">{{ ops.error.value }}</p>
    <p v-if="usage" class="inspector-note">
      整理用量（服务端回报）：输入 {{ usage.input.toLocaleString() }} / 输出
      {{ usage.output.toLocaleString() }} tokens
    </p>
  </section>
</template>
<style scoped>
.context-strategy {
  border-bottom: 1px solid var(--surface-hover);
  padding-bottom: 20px;
  margin-bottom: 20px;
}
.context-strategy h3 {
  margin: 0 0 16px;
}
.context-strategy-options,
.context-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}
.context-strategy-options label,
.compact-auto {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
  min-height: 32px;
  margin: 0;
}
.compact-auto-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0 16px;
}
.compact-auto-row .compact-auto,
.compact-auto-row .inspector-note {
  margin: 0;
  font-size: var(--fs-md);
  line-height: 1.5;
}
.compact-auto-row .compact-auto {
  flex-direction: row;
}
.context-strategy input[type='radio'],
.context-strategy input[type='checkbox'] {
  width: 16px;
  min-width: 16px;
  height: 16px;
  flex: none;
  margin: 0;
  accent-color: var(--accent);
}
.context-actions {
  margin-top: 16px;
}
.context-operation {
  margin-top: 18px;
  padding: 14px 0;
  border-top: 1px solid var(--surface-hover);
}
.context-operation progress {
  display: block;
  width: 100%;
  height: 5px;
  accent-color: var(--accent);
  margin: 12px 0;
}
.context-operation p {
  overflow-wrap: anywhere;
  margin: 0;
}
</style>
