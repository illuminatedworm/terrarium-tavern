<script setup lang="ts">
import { computed, ref } from 'vue';
import type { ModelProfile, Provider } from '../../lib/settingsDocument';
import type { NovelConfig } from '../../lib/novel';
import { channelProviders, type Channel } from '../../lib/channels';
import ChannelSwitch from '../common/ChannelSwitch.vue';
import ModelProfileEditor from './ModelProfileEditor.vue';
const props = defineProps<{
  value: NovelConfig;
  main: ModelProfile;
  officialMain: ModelProfile | null;
  providers: Provider[];
  initialChannel?: Channel;
  effortOptions: Array<{ value: string; label: string }>;
  flush: () => Promise<unknown>;
}>();
defineEmits<{ checked: [] }>();
const channel = ref<Channel>(props.initialChannel || 'standard');
const key = computed(() => (channel.value === 'chatgpt' ? 'official_model' : 'model'));
const base = computed(() => (channel.value === 'chatgpt' ? props.officialMain : props.main));
const profile = computed(() => props.value[key.value]);
function follow(value: boolean) {
  props.value[key.value] = value ? null : base.value ? JSON.parse(JSON.stringify(base.value)) : null;
}
</script>
<template>
  <div class="agent-settings">
    <ChannelSwitch v-model="channel" />
    <label class="settings-check"
      ><span>跟随 Full Agent 的实际模型（含对话内选择）</span
      ><input
        type="checkbox"
        :checked="!profile"
        :disabled="!base && !profile"
        @change="follow(($event.target as HTMLInputElement).checked)"
    /></label>
    <p v-if="!profile" class="muted">
      创建时使用主 agent 的模型与生成参数；不继承剧情上下文或消息适配。当前全局模型：{{
        base?.model || '尚未配置此渠道'
      }}。
    </p>
    <ModelProfileEditor
      v-if="profile"
      :key="channel"
      :profile="profile"
      :providers="channelProviders(providers, channel)"
      role="novel_research"
      :channel="channel"
      label="用户指定的原著调查模型"
      :effort-options="effortOptions"
      :flush="flush"
      @checked="$emit('checked')"
    />
    <p class="muted">
      调查者不能选择或更换模型。配置在创建时固定，修改只影响新建调查者；旧调查的实际模型在原著面板显示。
    </p>
    <fieldset>
      <legend>每个调查者的独立额度</legend>
      <div class="agent-setting-grid">
        <label
          >最多模型请求（次）<input
            v-model.number="value.max_requests"
            type="number"
            min="2"
            max="100" /></label
        ><label
          >累计运行时限（秒）<input v-model.number="value.timeout_seconds" type="number" min="30" max="3600"
        /></label>
      </div>
    </fieldset>
    <p class="muted">
      压缩计入调用次数，工具和网络等待计时；等待主 agent
      回答、完成后空闲不计时。跨轮、恢复和追问不刷新额度。每个分支同时最多三个调查者，每轮最多新建五次，重试不刷新启动次数。
    </p>
    <p class="muted">
      上下文压缩使用固定机制，无需设置：GPT 渠道使用云端压缩，普通 API
      使用当前调查模型生成摘要。完整阅读过程可回查，不建立故事记忆。
    </p>
  </div>
</template>
