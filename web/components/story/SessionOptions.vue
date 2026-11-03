<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
import FieldHelp from '../common/FieldHelp.vue';
import Icon from '../common/Icon.vue';
import PreferencePresetPicker from '../settings/PreferencePresetPicker.vue';
import PolicySelect from '../settings/PolicySelect.vue';
import { clone } from '../../lib/settingsDocument';
import type { PreferencePreset, SettingsDocument } from '../../lib/settingsDocument';
import type { Session } from '../../lib/types';

export type SessionOptionsDraft = Pick<Session, 'persona' | 'persona_data' | 'policy' | 'turn_preference'>;

/** Inspector "会话设置" page. Edits stay in a local draft until saved. */
const props = defineProps<{
  session: Session;
  fullAgent: boolean;
  notice: string;
  scriptsEnabled: boolean;
  permissionDisabled: boolean;
  recipeDisabled: boolean;
  preferencePrompt: string;
  inheritedPolicy?: string;
  presets: PreferencePreset[];
  exportDisabled: boolean;
  exporting: boolean;
}>();
const emit = defineEmits<{
  'message-adapter': [];
  'export-request': [];
  save: [draft: SessionOptionsDraft];
  'script-permission': [enabled: boolean];
  'presets-saved': [settings: SettingsDocument];
  'manage-presets': [];
  'edit-recipe': [];
  'reset-recipe': [];
}>();

const draft = ref<SessionOptionsDraft>(snapshot());
function snapshot(): SessionOptionsDraft {
  const { persona, persona_data, policy, turn_preference } = props.session;
  return clone({ persona, persona_data, policy, turn_preference });
}
// Another story, a save or an advisor preference replaces the saved values; unsaved
// edits are kept while unrelated story updates (revision, messages) arrive.
watch(
  () => JSON.stringify([props.session.id, snapshot()]),
  () => {
    draft.value = snapshot();
  },
);
// The save bar appears only while the draft differs from the saved values.
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(snapshot()));
const advanced = ref(false);
</script>

<template>
  <section class="inspector-options" aria-label="会话设置">
    <header class="inspector-options-head">
      <h2><Icon name="sliders" :size="20" />会话设置</h2>
      <p class="options-summary">
        <span v-if="dirty" class="options-dirty">有未保存修改</span>
        <span v-else-if="notice" role="status" class="options-saved">{{ notice }}</span>
        <span v-else>仅影响当前对话，随剧情进度继承</span>
      </p>
    </header>

    <section class="options-section" aria-labelledby="options-identity">
      <h3 id="options-identity"><span>01</span>身份</h3>
      <label class="options-field">你的身份<input v-model="draft.persona" /></label>
      <label class="options-field"
        >用户完整描述<ExpandableTextarea
          v-model="draft.persona_data.description"
          label="用户完整描述"
          placeholder="外貌、背景、性格、能力与边界"
      /></label>
    </section>

    <section class="options-section" aria-labelledby="options-preference">
      <h3 id="options-preference"><span>02</span>本轮偏好</h3>
      <label class="options-field"
        ><span
          >补充当前对话的系统提示（可选）<FieldHelp
            tooltip="仅补充当前对话的系统提示，保存后持续生效；清空可取消。" /></span
        ><ExpandableTextarea
          v-model="draft.turn_preference"
          label="本轮偏好"
          :prefix="preferencePrompt"
          placeholder="例如：多用对话推进，节奏放缓，给我留下行动空间。"
      /></label>
      <PreferencePresetPicker
        :key="session.id"
        v-model="draft.turn_preference"
        :revision="session.revision"
        :presets="presets"
        @saved="emit('presets-saved', $event)"
        @manage="emit('manage-presets')"
      />
    </section>

    <section class="options-section" aria-labelledby="options-policy">
      <h3 id="options-policy"><span>03</span>修改保存方式</h3>
      <PolicySelect v-model="draft.policy" :inherited-policy="inheritedPolicy" compact />
    </section>

    <section class="options-section" aria-labelledby="options-card">
      <h3 id="options-card"><span>04</span>角色卡</h3>
      <label class="options-switch"
        ><span>启用此对话的角色卡脚本</span
        ><input
          type="checkbox"
          class="switch"
          role="switch"
          :checked="scriptsEnabled"
          :disabled="permissionDisabled"
          @change="emit('script-permission', ($event.target as HTMLInputElement).checked)"
      /></label>
    </section>

    <section class="options-section" aria-labelledby="options-context">
      <h3 id="options-context"><span>05</span>上下文</h3>
      <dl class="options-values">
        <dt>世界书注入</dt>
        <dd>
          {{ session.lore_injection_mode === 'conversation' ? '随对话滚动' : 'ST 式顶部可变'
          }}<FieldHelp tooltip="随剧情进度继承。" />
        </dd>
        <dt>对话模式</dt>
        <dd>
          {{ fullAgent ? 'Full Agent' : 'Half Agent'
          }}<FieldHelp
            :tooltip="
              fullAgent
                ? 'Full Agent 使用独立的工作窗口与交接笔记。模式在创建会话时确定。'
                : '模式在创建会话时确定。'
            "
          />
        </dd>
      </dl>
      <template v-if="!fullAgent">
        <p class="options-label">上下文结构编排<FieldHelp tooltip="仅影响当前对话，随剧情进度继承。" /></p>
        <div class="options-actions">
          <button :disabled="recipeDisabled" @click="emit('edit-recipe')">编辑上下文配方</button
          ><button :disabled="recipeDisabled" @click="emit('reset-recipe')">恢复当前全局编排</button>
        </div>
      </template>
    </section>

    <section class="options-section" aria-labelledby="options-advanced">
      <button class="options-advanced" :aria-expanded="advanced" @click="advanced = !advanced">
        <h3 id="options-advanced"><span>06</span>高级</h3>
        <small>消息适配 · 导出请求</small>
        <Icon :name="advanced ? 'chevron-up' : 'chevron-down'" :size="16" />
      </button>
      <div v-if="advanced" class="options-actions">
        <button @click="emit('message-adapter')">消息适配</button
        ><button
          :disabled="exportDisabled"
          title="调试用：导出按当前分支和输入编译的下次请求 messages（JSON）；只读预览不运行神经召回。给他人阅读请用顶栏的“分享对话”"
          @click="emit('export-request')"
        >
          {{ exporting ? '正在导出…' : '导出请求（调试）' }}
        </button>
      </div>
    </section>

    <div v-if="dirty" class="options-savebar">
      <span>有未保存修改</span>
      <button class="quiet" @click="draft = snapshot()">放弃</button>
      <button class="primary" @click="emit('save', clone(draft))">保存</button>
    </div>
  </section>
</template>
