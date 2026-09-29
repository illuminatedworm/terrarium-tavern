<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, useId, watch, type Ref } from 'vue';
import { cachePreset, type CacheSettings } from '../../lib/cachePolicy';
const props = defineProps<{ cache: CacheSettings; model: string; api: string }>();
const emit = defineEmits<{ validity: [valid: boolean] }>();
const valueText = ref('');
const requestText = ref('');
const errors = ref<Record<string, string>>({});
const editorId = useId();
const localErrors = inject<Ref<Record<string, string>>>('settings-local-errors', ref({}));
watch(
  errors,
  (value) => {
    if (Object.keys(value).length) localErrors.value[editorId] = '缓存 JSON 尚未保存';
    else delete localErrors.value[editorId];
  },
  { deep: true },
);
onBeforeUnmount(() => {
  delete localErrors.value[editorId];
});
watch(
  () => props.cache.value,
  (value) => {
    if (!errors.value.value) valueText.value = JSON.stringify(value, null, 2);
  },
  { immediate: true },
);
watch(
  () => props.cache.request,
  (value) => {
    if (!errors.value.request) requestText.value = JSON.stringify(value, null, 2);
  },
  { immediate: true, deep: true },
);
watch(
  () => props.cache.mode,
  (mode) => {
    if (mode === 'cloud') {
      errors.value = {};
      valueText.value = JSON.stringify(props.cache.value, null, 2);
      requestText.value = JSON.stringify(props.cache.request, null, 2);
      emit('validity', true);
    }
  },
);
watch(
  () => props.cache.preset,
  (preset) => {
    if (
      preset === 'custom' &&
      props.cache.field === 'cache_control' &&
      JSON.stringify(props.cache.value) === '{"type":"ephemeral"}'
    ) {
      const matched = cachePreset(props.model, props.api, { ...props.cache, preset: 'auto' });
      if (matched.kind === 'block' && matched.field) {
        props.cache.field = matched.field;
        props.cache.value = matched.value;
        props.cache.request = { ...matched.request, ...props.cache.request };
      }
    }
    if (preset === 'auto') {
      delete errors.value.value;
      valueText.value = JSON.stringify(props.cache.value, null, 2);
      emit('validity', Object.keys(errors.value).length === 0);
    }
  },
);
const selected = computed(() => cachePreset(props.model, props.api, props.cache));
function edit(kind: 'value' | 'request', event: Event) {
  const text = (event.target as HTMLTextAreaElement).value;
  if (kind === 'value') valueText.value = text;
  else requestText.value = text;
  try {
    const value = JSON.parse(text);
    if (kind === 'request' && (!value || Array.isArray(value) || typeof value !== 'object'))
      throw new Error('请求级参数必须是 JSON 对象');
    props.cache[kind] = value;
    delete errors.value[kind];
  } catch (e) {
    errors.value[kind] = String(e);
  }
  emit('validity', Object.keys(errors.value).length === 0);
}
</script>

<template>
  <section aria-label="提示缓存" class="cache-settings" data-setting-path="cache">
    <h4>提示缓存</h4>
    <label
      >缓存控制<select v-model="cache.mode" aria-label="缓存控制">
        <option value="cloud">云端默认</option>
        <option value="runtime">本地运行时控制</option>
      </select></label
    >
    <p v-if="cache.mode === 'cloud'" class="muted">不发送本地缓存控制参数，由服务商决定缓存行为。</p>
    <template v-else>
      <p class="muted">断点自动停在两轮已完成对话之前，也可在对话中手动固定。断点不代表已命中缓存。</p>
      <label
        >参数格式<select v-model="cache.preset" aria-label="参数格式">
          <option value="auto">按模型和接口自动匹配</option>
          <option value="custom">自定义参数</option>
        </select></label
      >
      <p v-if="selected.kind === 'gemini'" class="muted">
        由运行时创建和引用 Gemini 缓存对象，无需填写资源 ID。服务商不支持时明确降级为云端默认。
      </p>
      <p v-else-if="selected.kind === 'unknown'" class="muted">
        没有匹配的断点格式。请选择自定义参数；未配置时使用云端默认。
      </p>
      <p v-else-if="cache.preset === 'auto'" class="muted">
        匹配参数：{{ selected.field }} = {{ JSON.stringify(selected.value) }}。模型名称匹配不保证服务商支持。
      </p>
      <template v-if="cache.preset === 'custom'">
        <label>断点字段名<input v-model="cache.field" :list="editorId" aria-label="断点字段名" /></label>
        <datalist :id="editorId">
          <option value="cache_control" />
          <option value="prompt_cache_breakpoint" />
        </datalist>
        <label
          >断点 JSON 值<textarea
            :value="valueText"
            aria-label="断点 JSON 值"
            :aria-invalid="!!errors.value"
            rows="3"
            @input="edit('value', $event)"
          />
        </label>
        <p v-if="errors.value" role="alert">{{ errors.value }}</p>
      </template>
      <label
        >附加请求参数（JSON，可留 {}）<textarea
          :value="requestText"
          aria-label="附加请求参数（JSON，可留 {}）"
          :aria-invalid="!!errors.request"
          rows="3"
          @input="edit('request', $event)"
        />
      </label>
      <p v-if="errors.request" role="alert">{{ errors.request }}</p>
      <p class="muted">仅在本地控制生效时发送。不能覆盖模型、消息、工具和输出额度；云端降级时不发送。</p>
    </template>
  </section>
</template>

<style scoped>
.cache-settings {
  display: grid;
  gap: 0.65rem;
  min-width: 0;
}
.cache-settings label {
  display: grid;
  gap: 0.35rem;
  min-width: 0;
}
.cache-settings textarea {
  width: 100%;
  min-height: 5.5rem;
  resize: vertical;
  font-family: monospace;
}
.cache-settings [role='alert'] {
  color: var(--danger, #a83232);
  overflow-wrap: anywhere;
}
</style>
