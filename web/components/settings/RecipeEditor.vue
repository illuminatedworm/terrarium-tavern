<script setup lang="ts">
import { useTimedError } from '../../composables/useTimedError';
import { computed, ref } from 'vue';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import { sources } from '../../lib/recipeSources';
type Block = {
  id: string;
  source: string;
  role: string;
  enabled: boolean;
  content: string;
  depth: number | null;
  priority: number;
  budget: number | null;
  required: boolean;
  condition: string;
};
const props = defineProps<{ recipe: any; saving?: boolean; saveLabel?: string }>();
const emit = defineEmits<{ save: [recipe: any] }>();

const original = JSON.parse(JSON.stringify(props.recipe));
const items = ref<{ key: string; block: Block }[]>(
  (original.blocks || []).map((block: any) => ({
    key: crypto.randomUUID(),
    block: {
      role: 'system',
      enabled: true,
      content: '',
      depth: null,
      priority: 50,
      budget: null,
      required: false,
      condition: '',
      ...block,
    },
  })),
);
const selectedKey = ref(items.value[0]?.key || '');
const selected = computed(() => items.value.find((item) => item.key === selectedKey.value));
const selectedIndex = computed(() => items.value.findIndex((item) => item.key === selectedKey.value));
const description = computed(
  () => sources.find((source) => source.value === selected.value?.block.source) || sources.at(-1)!,
);
const custom = computed(
  () => !sources.slice(0, -1).some((source) => source.value === selected.value?.block.source),
);
const history = computed(() => selected.value?.block.source === 'history');
const adding = ref('custom'),
  error = ref(''),
  dragging = ref<number | null>(null),
  over = ref<number | null>(null);
function title(block: Block) {
  return sources.find((source) => source.value === block.source)?.label || `其他来源：${block.source}`;
}
function move(from: number, to: number) {
  if (props.saving || from < 0 || to < 0 || from >= items.value.length || to >= items.value.length) return;
  const [item] = items.value.splice(from, 1);
  items.value.splice(to, 0, item);
  selectedKey.value = item.key;
}
function startDrag(index: number, event: DragEvent) {
  if (props.saving) {
    event.preventDefault();
    return;
  }
  dragging.value = index;
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(index));
  }
}
function drop(index: number) {
  if (dragging.value !== null) move(dragging.value, index);
  dragging.value = null;
  over.value = null;
}
function add() {
  const source = adding.value,
    key = crypto.randomUUID();
  items.value.push({
    key,
    block: {
      id: `${source}_${key.slice(0, 8)}`,
      source,
      role: source === 'input' ? 'user' : source === 'examples' ? 'assistant' : 'system',
      enabled: true,
      content: '',
      depth: null,
      priority: 50,
      budget: null,
      required: false,
      condition: '',
    },
  });
  selectedKey.value = key;
}
function remove() {
  const index = selectedIndex.value;
  if (index < 0) return;
  items.value.splice(index, 1);
  selectedKey.value = items.value[Math.min(index, items.value.length - 1)]?.key || '';
}
function nullable(event: Event) {
  const value = (event.target as HTMLInputElement).value;
  return value === '' ? null : Number(value);
}
function save() {
  error.value = '';
  const ids = new Set<string>();
  for (const { block } of items.value) {
    if (!block.id.trim() || !block.source.trim()) {
      error.value = '块标识和来源不能为空。';
      return;
    }
    if (ids.has(block.id)) {
      error.value = '块标识不能重复，请为每个块使用独立标识。';
      return;
    }
    ids.add(block.id);
    if (
      !Number.isInteger(block.priority) ||
      (block.depth !== null && (!Number.isInteger(block.depth) || block.depth < 0)) ||
      (block.budget !== null && (!Number.isInteger(block.budget) || block.budget < 1))
    ) {
      error.value = '优先级需为整数；深度需为非负整数；超长提示阈值需为正整数，或留空。';
      return;
    }
  }
  emit('save', { ...original, blocks: JSON.parse(JSON.stringify(items.value.map((item) => item.block))) });
}
useTimedError(error);
</script>

<template>
  <h2>上下文配方</h2>
  <p class="recipe-intro">
    拖动左侧手柄调整注入顺序，选择内容块后在右侧编辑。常驻世界书统一放在顶部独立区域，条目位置和深度只影响区域内顺序，不穿插历史，也不参与可选内容裁剪。
    深度定位的配方块会由 Roleplay Runtime
    移到剧情消息附近；滚动方案的非常驻世界书自动紧贴对应消息，忽略前后置世界书块；ST
    方案的非常驻条目使用原位置规则。对话偏好和本次重写要求仍会参与最终编排。
  </p>
  <AppNotification :message="error" />
  <div class="recipe-layout" @input="error = ''" @change="error = ''">
    <div class="recipe-sequence">
      <div class="recipe-sequence-heading">
        <strong>注入顺序</strong><small>{{ items.length }} 个块</small>
      </div>
      <ol class="recipe-list">
        <li
          v-for="(item, index) in items"
          :key="item.key"
          :class="{
            selected: selectedKey === item.key,
            'drop-target': over === index,
            muted: !item.block.enabled,
          }"
          @dragover.prevent="over = index"
          @drop.prevent="drop(index)"
        >
          <button
            class="recipe-grip"
            :draggable="!saving"
            :disabled="saving"
            :aria-label="`移动内容块：${title(item.block)}`"
            title="拖动排序，或使用上下方向键"
            @dragstart="startDrag(index, $event)"
            @dragend="
              dragging = null;
              over = null;
            "
            @keydown.up.prevent="move(index, index - 1)"
            @keydown.down.prevent="move(index, index + 1)"
          >
            ⠿
          </button>
          <button
            class="recipe-select"
            :disabled="saving"
            :aria-pressed="selectedKey === item.key"
            @click="selectedKey = item.key"
          >
            <span class="recipe-number">{{ index + 1 }}</span
            ><span class="recipe-title"
              >{{ title(item.block)
              }}<small>{{
                item.block.enabled
                  ? item.block.depth === null
                    ? '按顺序注入'
                    : `深度 ${item.block.depth}`
                  : '已关闭'
              }}</small></span
            >
          </button>
        </li>
      </ol>
      <p v-if="!items.length" class="recipe-intro">暂无内容块。添加来源后编排角色、历史和玩家输入。</p>
      <div class="recipe-add">
        <select v-model="adding" aria-label="添加内容块来源" :disabled="saving">
          <option v-for="source in sources" :key="source.value" :value="source.value">
            {{ source.label }}
          </option></select
        ><button :disabled="saving" @click="add">＋ 添加</button>
      </div>
    </div>
    <fieldset v-if="selected" class="recipe-parameters" :disabled="saving">
      <div class="recipe-detail-heading">
        <h3>{{ title(selected.block) }}</h3>
        <div>
          <button
            :disabled="selectedIndex === 0"
            aria-label="上移内容块"
            @click="move(selectedIndex, selectedIndex - 1)"
          >
            ↑</button
          ><button
            :disabled="selectedIndex === items.length - 1"
            aria-label="下移内容块"
            @click="move(selectedIndex, selectedIndex + 1)"
          >
            ↓</button
          ><button class="recipe-remove" @click="remove">移除块</button>
        </div>
      </div>
      <p class="recipe-source-description">{{ description.description }}</p>
      <label
        >注入内容来源<select v-model="selected.block.source">
          <option v-for="source in sources" :key="source.value" :value="source.value">
            {{ source.label }}
          </option>
          <option
            v-if="!sources.some((source) => source.value === selected?.block.source)"
            :value="selected.block.source"
          >
            其他来源：{{ selected.block.source }}
          </option>
        </select></label
      >
      <div class="recipe-fields">
        <label
          >块标识<input v-model="selected.block.id" aria-label="块标识" /><small
            >用于上下文追踪和位置锚点，需保持唯一。</small
          ></label
        ><label
          >消息角色<select v-model="selected.block.role" aria-label="消息角色" :disabled="history">
            <option value="system">system · 系统指令</option>
            <option value="developer">developer · 开发者指令</option>
            <option value="user">user · 玩家发言</option>
            <option value="assistant">assistant · AI 发言</option></select
          ><small>{{
            history ? '历史消息保留各自角色。' : '决定模型如何理解这个块；输入通常用 user。'
          }}</small></label
        >
      </div>
      <div class="recipe-checks">
        <label><input v-model="selected.block.enabled" type="checkbox" />启用内容块</label
        ><label
          ><input
            v-model="selected.block.required"
            type="checkbox"
            :disabled="history"
          />预算不足时保留</label
        >
      </div>
      <div class="recipe-fields">
        <label
          >注入深度<input
            type="number"
            min="0"
            :value="selected.block.depth ?? ''"
            aria-label="注入深度"
            :disabled="history"
            placeholder="留空：按配方顺序"
            @input="selected.block.depth = nullable($event)"
          /><small>留空按左侧顺序；0 放在末尾；1 放在最后一条剧情消息前，数值越大越往前。</small></label
        >
      </div>
      <label
        >超长提示阈值（tokens）<input
          type="number"
          min="1"
          :value="selected.block.budget ?? ''"
          aria-label="超长提示阈值（tokens）"
          :disabled="history"
          placeholder="留空：不提示"
          @input="selected.block.budget = nullable($event)"
        /><small
          >使用前台 Writer 选定的分词器估算内容 tokens（不含消息封装）。超过阈值只提示，仍完整发送。</small
        ></label
      >
      <label
        >启用条件<input
          v-model="selected.block.condition"
          aria-label="启用条件"
          placeholder="留空：始终启用，例如 stat_data.世界.已进入灯塔"
        /><small>填写当前状态的点分路径；对应值为真时才注入。不是表达式或触发词。</small></label
      >
      <template v-if="custom"
        ><label
          >来源标识<input v-model="selected.block.source" aria-label="来源标识" /><small
            >自定义文本可使用 custom；动态世界书出口使用 outlet:名称。</small
          ></label
        ><label
          >自定义注入文本<ExpandableTextarea v-model="selected.block.content" label="自定义注入文本" /></label
      ></template>
      <p class="recipe-footnote">
        启用、条件和来源有无内容决定块是否注入。“预算不足时保留”只控制裁剪，不会强制注入已关闭的块。历史摘要保护及端点角色兼容可能调整最终结果，可在“上下文”中查看实际编排。
      </p>
    </fieldset>
  </div>
  <div class="modal-footer">
    <button class="primary" :disabled="saving" @click="save">
      {{ saving ? '正在保存…' : saveLabel || '保存配方' }}
    </button>
  </div>
</template>

<style scoped>
.recipe-intro,
.recipe-source-description,
.recipe-footnote {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.8;
}
.recipe-layout {
  display: grid;
  grid-template-columns: 290px minmax(0, 1fr);
  gap: 24px;
  margin-top: 20px;
}
.recipe-sequence {
  min-width: 0;
}
.recipe-sequence-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
  font-size: 13px;
}
.recipe-sequence-heading strong {
  font-weight: 500;
}
.recipe-sequence-heading small {
  font-size: 11px;
}
.recipe-list {
  list-style: none;
  padding: 0;
  margin: 0;
  border-top: 1px solid var(--border-subtle);
}
.recipe-list li {
  display: flex;
  align-items: center;
  border-bottom: 1px solid var(--border-subtle);
}
.recipe-list li.selected {
  background: var(--accent-soft);
}
.recipe-list li.drop-target {
  box-shadow: inset 0 2px var(--accent);
}
.recipe-list button {
  border: 0;
  background: transparent;
  border-radius: 0;
}
.recipe-grip {
  flex-shrink: 0;
  padding: 8px;
  cursor: grab;
  color: var(--text-muted);
  font-size: 19px;
}
.recipe-grip:active {
  cursor: grabbing;
}
.recipe-select {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
  text-align: left;
  padding: 10px 8px 10px 0;
}
.recipe-number {
  font-size: 10px;
  color: var(--text-faint);
  width: 16px;
  flex-shrink: 0;
}
.recipe-title {
  font-size: 12px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.recipe-title small {
  display: block;
  margin-top: 4px;
  font-size: 10px;
  color: var(--text-muted);
}
.recipe-add {
  display: flex;
  gap: 6px;
  margin-top: 14px;
}
.recipe-add select {
  min-width: 0;
  flex: 1;
  font-size: 12px;
  padding: 7px;
}
.recipe-add button {
  flex-shrink: 0;
  font-size: 12px;
  padding: 7px;
}
.recipe-parameters {
  min-width: 0;
  border: 0;
  padding: 0 0 0 24px;
  margin: 0;
  border-left: 1px solid var(--border-subtle);
}
.recipe-detail-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.recipe-detail-heading h3 {
  font-size: 15px;
  margin: 0;
}
.recipe-detail-heading > div {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.recipe-detail-heading button {
  font-size: 11px;
  padding: 5px 7px;
}
.recipe-detail-heading .recipe-remove {
  color: var(--danger);
}
.recipe-source-description {
  padding: 12px;
  background: var(--surface-base);
  border-radius: 7px;
  margin: 14px 0;
}
.recipe-parameters input,
.recipe-parameters select {
  font-size: 12px;
}
.recipe-parameters label {
  font-size: 12px;
  margin-top: 14px;
}
.recipe-parameters label small {
  display: block;
  font-size: 10px;
  line-height: 1.65;
  margin-top: 6px;
}
.recipe-fields {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.recipe-checks {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 12px 0;
}
.recipe-checks label {
  display: flex;
  gap: 6px;
  align-items: center;
  margin: 0;
}
.recipe-checks input {
  margin: 0;
}
.recipe-error {
  background: var(--danger-surface);
  color: var(--danger);
  padding: 10px;
  font-size: 12px;
  border-radius: 6px;
}
.recipe-footnote {
  margin-top: 18px;
  font-size: 10px;
}
@media (max-width: 760px) {
  .recipe-layout {
    grid-template-columns: 1fr;
    gap: 20px;
  }
  .recipe-list {
    max-height: 230px;
    overflow: auto;
  }
  .recipe-parameters {
    border-left: 0;
    border-top: 1px solid var(--border-subtle);
    padding: 20px 0 0;
  }
}
@media (max-width: 420px) {
  .recipe-fields {
    grid-template-columns: 1fr;
    gap: 0;
  }
  .recipe-detail-heading {
    flex-wrap: wrap;
  }
}
</style>
