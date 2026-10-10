<script setup lang="ts">
import { computed, getCurrentInstance, inject } from 'vue';
import type { ContextOperations } from '../../composables/useContextOperations';
const props = defineProps<{ session: any; disabled?: boolean; pressureRatio?: number }>();
const ops = inject<ContextOperations>('context-operations');
const compact = computed(() => props.session.context_strategy === 'openai_compact');
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
        />本地换窗</label
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
      {{ compact ? '历史由云端压缩，长期笔记始终保留。' : '由 Agent 保存交接笔记并换窗，长期笔记独立保留。' }}
    </p>
    <template v-if="compact">
      <div class="compact-auto-row">
        <label class="compact-auto"
          ><input
            type="checkbox"
            :checked="session.compact_auto"
            :disabled="working"
            @change="ops.automatic(($event.target as HTMLInputElement).checked)"
          />自动压缩</label
        >
        <span v-if="pressureRatio != null" class="inspector-note"
          >达到窗口的 {{ Math.round(pressureRatio * 1000) / 10 }}% 时触发</span
        >
      </div>
      <div class="context-actions">
        <button class="primary" :disabled="working" @click="ops.start('compact')">立即压缩</button>
        <button :disabled="working" @click="ops.start('enable_local')">切回本地换窗</button>
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
          ops.operation.value.kind === 'enable_local'
            ? '新的交接笔记已保存，已切回本地换窗。'
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
