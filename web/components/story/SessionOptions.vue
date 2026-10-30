<script setup lang="ts">
import { ref, watch } from 'vue';
import ExpandableTextarea from '../common/ExpandableTextarea.vue';
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
</script>

<template>
  <section class="inspector-options" aria-label="会话设置">
    <button @click="emit('message-adapter')">消息适配</button
    ><button
      :disabled="exportDisabled"
      title="调试用：导出按当前分支和输入编译的下次请求 messages（JSON）；只读预览不运行神经召回。给他人阅读请用顶栏的“分享对话”"
      @click="emit('export-request')"
    >
      {{ exporting ? '正在导出…' : '导出请求（调试）' }}
    </button>
    <div class="inspector-options-heading">
      <h2>会话设置</h2>
      <div class="inspector-save">
        <span v-if="notice" role="status">{{ notice }}</span>
        <button class="primary" @click="emit('save', clone(draft))">保存</button>
      </div>
    </div>
    <label class="check">
      <input
        type="checkbox"
        :checked="scriptsEnabled"
        :disabled="permissionDisabled"
        @change="emit('script-permission', ($event.target as HTMLInputElement).checked)"
      />启用此对话的角色卡脚本
    </label>
    <label>你的身份<input v-model="draft.persona" /></label
    ><label
      >用户完整描述<ExpandableTextarea
        v-model="draft.persona_data.description"
        label="用户完整描述"
        placeholder="外貌、背景、性格、能力与边界" /></label
    ><label
      >本轮偏好（可选）<ExpandableTextarea
        v-model="draft.turn_preference"
        label="本轮偏好"
        :prefix="preferencePrompt"
        placeholder="例如：多用对话推进，节奏放缓，给我留下行动空间。"
      />
    </label>
    <PreferencePresetPicker
      :key="session.id"
      v-model="draft.turn_preference"
      :revision="session.revision"
      :presets="presets"
      @saved="emit('presets-saved', $event)"
      @manage="emit('manage-presets')"
    />
    <p class="inspector-note">仅补充当前对话的系统提示，保存后持续生效；清空可取消。</p>
    <PolicySelect v-model="draft.policy" :inherited-policy="inheritedPolicy" compact />
    <p class="inspector-note">
      世界书注入：{{
        session.lore_injection_mode === 'conversation' ? '随对话滚动' : 'ST 式顶部可变'
      }}（随剧情进度继承）。
    </p>
    <p v-if="fullAgent" class="inspector-note">
      Full Agent 使用独立的工作窗口与交接笔记。模式在创建会话时确定。
    </p>
    <template v-else>
      <h3>上下文结构编排</h3>
      <p class="inspector-note">仅影响当前对话，随剧情进度继承。</p>
      <button :disabled="recipeDisabled" @click="emit('edit-recipe')">编辑上下文配方</button
      ><button :disabled="recipeDisabled" @click="emit('reset-recipe')">恢复当前全局编排</button>
    </template>
  </section>
</template>
