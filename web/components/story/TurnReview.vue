<script setup lang="ts">
import { getValueByPointer } from 'fast-json-patch';

type ReviewChange = {
  kind: string;
  label: string;
  before: unknown;
  after: unknown;
  operations: { path: string; op: string }[];
};
defineProps<{ changes?: ReviewChange[] }>();
defineEmits<{ decide: [accept: boolean] }>();

function display(value: unknown) {
  return value === undefined ? '（无）' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function valueAt(value: unknown, path: string) {
  // A removed array member or parent object may no longer exist on one side.
  try {
    return getValueByPointer(value, path);
  } catch {
    return undefined;
  }
}
</script>

<template>
  <div class="review" aria-label="审核本轮修改">
    <strong>本轮有变量、角色设定或世界书修改，等待审核</strong>
    <p class="muted">接受后保存整轮正文与修改；拒绝则整轮不提交。</p>
    <details v-for="change in changes" :key="change.kind" open>
      <summary>{{ change.label }} · {{ change.operations.length }} 处修改</summary>
      <div v-for="(operation, index) in change.operations" :key="index" class="review-change">
        <code>{{ operation.path || '/' }}</code>
        <div class="review-values">
          <div>
            <span>修改前</span>
            <pre>{{ display(valueAt(change.before, operation.path)) }}</pre>
          </div>
          <div>
            <span>修改后</span>
            <pre>{{ display(valueAt(change.after, operation.path)) }}</pre>
          </div>
        </div>
      </div>
    </details>
    <p v-if="!changes?.length" class="muted">这是旧版生成的待审核草稿，可接受或拒绝后重新生成。</p>
    <button class="primary" @click="$emit('decide', true)">接受并保存</button>
    <button @click="$emit('decide', false)">拒绝本轮修改</button>
  </div>
</template>

<style scoped>
.review details {
  margin: 12px 0;
}
.review-change {
  margin: 8px 0;
  min-width: 0;
}
.review-change code {
  overflow-wrap: anywhere;
}
.review-values {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.review-values > div {
  min-width: 0;
}
.review-values span {
  color: var(--text-muted);
  font-size: 12px;
}
.review-values pre {
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 760px) {
  .review-values {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
