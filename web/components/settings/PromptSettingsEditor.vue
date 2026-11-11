<script setup lang="ts">
import { computed, nextTick, ref } from 'vue';
import type { useSettingsEditor } from '../../composables/useSettingsEditor';
import { getPath, type PromptChoices, type PromptDefinition } from '../../lib/settingsDocument';
import SavedText from './SavedText.vue';

const props = defineProps<{
  definition: PromptDefinition;
  editor: ReturnType<typeof useSettingsEditor>;
  guard: (action: () => void) => void;
}>();
const { saved, textBusy, textErrors, textValue, editText, saveText, saveValue, saveOnBlur } = props.editor;
const scope = props.definition.scope;
const path = props.definition.path;
const choices = computed<PromptChoices | undefined>(() =>
  props.definition.choices ? getPath(saved.value![scope], path) : undefined,
);
const viewed = ref<number>();
const selected = computed(() => viewed.value ?? choices.value?.active ?? 1);
const optionPath = computed(() => `${path}.options.${selected.value - 1}`);
const textPath = computed(() => (choices.value ? `${optionPath.value}.text` : path));
const namePath = computed(() => `${optionPath.value}.name`);
const activePath = `${path}.active`;
const text = computed(() => textValue(scope, textPath.value));
const savedName = computed(() => choices.value?.options[selected.value - 1].name ?? '');
const name = computed(() => textValue(scope, namePath.value));
const renaming = ref(false);
const savingName = ref(false);
const choiceList = ref<HTMLDivElement>();

function browse(option: number) {
  if (option === selected.value) return;
  props.guard(() => {
    viewed.value = option;
    renaming.value = false;
  });
}
function activate() {
  const option = selected.value;
  props.guard(() => void saveValue(scope, activePath, option));
}
function closeRename(restoreFocus = true) {
  renaming.value = false;
  if (restoreFocus)
    void nextTick(() =>
      choiceList.value?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus(),
    );
}
function startRename(option: number) {
  const open = () => {
    viewed.value = option;
    renaming.value = true;
    void nextTick(() => {
      const input = choiceList.value?.querySelector('input');
      input?.focus();
      input?.select();
    });
  };
  if (option === selected.value) open();
  else props.guard(open);
}
function cancelRename(event: KeyboardEvent) {
  if (event.isComposing) return;
  event.preventDefault();
  event.stopPropagation();
  if (savingName.value) return;
  editText(scope, namePath.value, savedName.value);
  closeRename();
}
async function saveName(restoreFocus: boolean) {
  if (!renaming.value || savingName.value) return;
  const savingPath = namePath.value;
  savingName.value = true;
  try {
    await saveOnBlur(scope, savingPath);
    if (
      savingPath === namePath.value &&
      !textErrors.value[`${scope}.${savingPath}`] &&
      name.value === savedName.value
    ) {
      const stillFocused = choiceList.value?.querySelector('input') === document.activeElement;
      closeRename(restoreFocus && stillFocused);
    }
  } finally {
    savingName.value = false;
  }
}
function onNameEnter(event: KeyboardEvent) {
  if (event.isComposing) return;
  event.preventDefault();
  event.stopPropagation();
  void saveName(true);
}

function focusChoiceOnClick(event: MouseEvent) {
  if (!renaming.value || event.button !== 0 || !(event.target instanceof Element)) return;
  const button = event.target.closest('button');
  if (!button || choiceList.value?.querySelector('input') !== document.activeElement) return;
  // Closing the name input resizes the tabs. Wait for the click before moving focus.
  if (event.type === 'mousedown') event.preventDefault();
  else button.focus();
}
</script>

<template>
  <div class="settings-prompt-editor" :data-setting-path="path">
    <p v-if="path === 'system_prompt'" class="settings-prompt-warning">
      除非你知道自己在做什么，否则不要修改。
    </p>
    <SavedText
      :label="definition.label"
      :model-value="text"
      :saved-value="getPath(saved![scope], textPath)"
      :saving="!!textBusy"
      :error="textErrors[scope + '.' + textPath]"
      :guard="guard"
      @update:model-value="editText(scope, textPath, $event)"
      @save="saveText(scope, textPath)"
    >
      <template v-if="choices" #toolbar>
        <div class="prompt-choice-toolbar">
          <div class="prompt-choice-row" @mousedown="focusChoiceOnClick" @click.capture="focusChoiceOnClick">
            <div
              ref="choiceList"
              class="prompt-choice-list"
              role="group"
              :aria-label="definition.label + '选项'"
            >
              <div
                v-for="(option, index) in choices.options"
                :key="index"
                class="prompt-choice-tab"
                :class="{ 'is-selected': selected === index + 1 }"
              >
                <div v-if="renaming && selected === index + 1" class="prompt-choice-rename">
                  <span class="prompt-choice-number">{{ index + 1 }}</span>
                  <input
                    class="prompt-choice-name-input"
                    type="text"
                    enterkeyhint="done"
                    aria-label="选项名称"
                    aria-description="失去焦点或按回车保存，按 Escape 取消"
                    title="失焦或回车保存 · Esc 取消"
                    :value="name"
                    @input="editText(scope, namePath, ($event.target as HTMLInputElement).value)"
                    @blur="saveName(false)"
                    @keydown.enter="onNameEnter"
                    @keydown.esc="cancelRename"
                  />
                  <span
                    v-if="choices.active === index + 1"
                    class="prompt-choice-status prompt-choice-current"
                  >
                    当前
                  </span>
                </div>
                <button
                  v-else
                  type="button"
                  :aria-pressed="selected === index + 1"
                  :aria-label="
                    [
                      String(index + 1),
                      option.name.trim(),
                      choices.active === index + 1 ? '当前' : '',
                      !option.text.trim() ? '空白' : '',
                    ]
                      .filter(Boolean)
                      .join('，')
                  "
                  aria-keyshortcuts="F2"
                  :disabled="!!textBusy"
                  :title="[index + 1, option.name.trim(), '双击或按 F2 改名'].filter(Boolean).join(' · ')"
                  @click="browse(index + 1)"
                  @dblclick.prevent="startRename(index + 1)"
                  @keydown.f2.prevent.stop="startRename(index + 1)"
                >
                  <span class="prompt-choice-number">{{ index + 1 }}</span>
                  <span v-if="option.name.trim()" class="prompt-choice-name">{{ option.name.trim() }}</span>
                  <span
                    v-if="choices.active === index + 1 || !option.text.trim()"
                    class="prompt-choice-status"
                    aria-hidden="true"
                  >
                    <span v-if="choices.active === index + 1" class="prompt-choice-current">当前</span>
                    <span v-if="!option.text.trim()">空白</span>
                  </span>
                </button>
              </div>
            </div>
            <button
              type="button"
              class="quiet prompt-choice-activate"
              :disabled="!!textBusy || selected === choices.active"
              @click="activate"
            >
              {{ textBusy === scope + '.' + activePath ? '正在切换…' : '设为当前' }}
            </button>
          </div>
          <p v-if="renaming && textErrors[scope + '.' + namePath]" class="prompt-choice-error" role="alert">
            {{ textErrors[scope + '.' + namePath] }}
          </p>
          <p v-if="textErrors[scope + '.' + activePath]" class="prompt-choice-error" role="alert">
            {{ textErrors[scope + '.' + activePath] }}
          </p>
        </div>
      </template>
    </SavedText>
    <p v-if="scope !== 'shared'" class="muted">{{ definition.description }}</p>
    <p
      v-if="
        scope === 'full_agent' && path === 'system_prompt' && /handoff|new_context|tavern_memory/.test(text)
      "
      class="muted"
    >
      此自定义主规则包含本地换窗要求；使用云端 compact 前请移除其中的换窗命令，或恢复通用默认文本。
    </p>
    <button
      v-if="!choices || selected === 1"
      type="button"
      @click="editText(scope, textPath, definition.default)"
    >
      恢复默认文本
    </button>
  </div>
</template>

<style scoped>
.prompt-choice-toolbar {
  margin-top: 4px;
}
.prompt-choice-row {
  display: flex;
  align-items: flex-end;
  gap: 6px;
}
.prompt-choice-list {
  display: flex;
  flex-wrap: wrap;
  min-width: 0;
  gap: 3px;
}
.prompt-choice-tab {
  --choice-edge: var(--border-control);
  --choice-face: var(--surface-base);
  position: relative;
  isolation: isolate;
  flex: none;
  min-width: 0;
  max-inline-size: min(100%, 15rem);
  color: var(--text-secondary);
}
.prompt-choice-tab button,
.prompt-choice-rename {
  display: flex;
  align-items: center;
  gap: 6px;
  max-inline-size: 100%;
  padding: 5px 16px 5px 8px;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: var(--fs-sm);
  line-height: 1.25;
}
.prompt-choice-tab button {
  touch-action: manipulation;
  user-select: none;
}
.prompt-choice-tab::before,
.prompt-choice-tab::after {
  content: '';
  position: absolute;
  z-index: -1;
  border-radius: var(--radius-xs) 0 0;
  clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 100%, 0 100%);
  pointer-events: none;
}
.prompt-choice-tab::before {
  inset: 0;
  background: var(--choice-edge);
}
.prompt-choice-tab::after {
  inset: 1px 2px 0 1px;
  background: var(--choice-face);
}
.prompt-choice-tab:hover {
  --choice-face: var(--surface-hover);
}
.prompt-choice-tab button:is(:hover, :active):not(:disabled) {
  background: transparent;
}
.prompt-choice-tab.is-selected {
  --choice-edge: var(--border-strong);
  --choice-face: var(--surface-active);
  color: var(--text-strong);
}
.prompt-choice-number {
  flex: none;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}
.prompt-choice-tab.is-selected .prompt-choice-number {
  color: var(--accent-hover);
}
.prompt-choice-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}
.prompt-choice-status {
  display: flex;
  flex: none;
  gap: 3px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.prompt-choice-current {
  color: var(--accent-hover);
  font-weight: 600;
}
.prompt-choice-activate {
  flex: none;
  padding: 3px 7px;
  font-size: var(--fs-sm);
  white-space: nowrap;
}
.prompt-choice-rename > .prompt-choice-name-input[type='text'] {
  inline-size: 11em;
  min-inline-size: 0;
  min-block-size: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: inset 0 -1px var(--accent);
  color: inherit;
  font: inherit;
  font-weight: 600;
}
.prompt-choice-error {
  margin: 4px;
  color: var(--danger);
  overflow-wrap: anywhere;
}
</style>
