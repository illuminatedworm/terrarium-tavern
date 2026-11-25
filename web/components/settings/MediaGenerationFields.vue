<script setup lang="ts">
import { computed, ref, useId } from 'vue';
import { channelLabels, channelProviders, type Channel } from '../../lib/channels';
import {
  adapterLabels,
  isSeedClone,
  metadataStyle,
  seedCloneResource,
  voiceError,
  type Adapter,
  type MediaDefaults,
  type MediaKind,
  type MediaProtocols,
  type Profile,
} from '../../lib/media';
import { useModelCatalog } from '../../lib/modelCatalog';
import { clone, type Provider } from '../../lib/settingsDocument';
import ChannelSwitch from '../common/ChannelSwitch.vue';
import Icon from '../common/Icon.vue';
import SelectMenu from '../common/SelectMenu.vue';
import ModelSelect from './ModelSelect.vue';
import MediaVoiceSelect from './MediaVoiceSelect.vue';
import MediaPreview from './MediaPreview.vue';

const props = defineProps<{
  kind: MediaKind;
  route: MediaDefaults[MediaKind];
  catalog: MediaProtocols;
  providers: Provider[];
  disabled?: boolean;
  flush?: () => Promise<unknown>;
  compact?: boolean;
}>();
const editing = ref<Channel>(props.route.active_channel);
const preview = ref(false);
const sizeList = useId();
const current = computed(() => props.route[editing.value]);
const live = computed(() => props.route[props.route.active_channel]);
const speech = computed(() => ('default_voice' in current.value ? current.value : undefined));
const image = computed(() => ('default_voice' in current.value ? undefined : current.value));
const speechRoute = computed(() => ('max_segments' in props.route ? props.route : undefined));
const imageRoute = computed(() => ('max_images' in props.route ? props.route : undefined));
const available = computed(() =>
  channelProviders(props.providers, editing.value).filter((p) => p.auth_mode !== 'chatgpt_subscription'),
);
const provider = computed(() => available.value.find((p) => p.id === current.value.provider_id));
function hasCredential(p: Provider | undefined, profile: Profile) {
  return (
    !!p &&
    (!!p.api_key || !!p.has_api_key || !!props.catalog.keyless_adapters[p.id]?.includes(profile.adapter))
  );
}
const missingKey = computed(() => !!provider.value && !hasCredential(provider.value, current.value));
function ready(p: Provider | undefined, profile: Profile) {
  return (
    !!profile.model.trim() &&
    hasCredential(p, profile) &&
    (!('default_voice' in profile) || !voiceError(profile, props.catalog, profile.default_voice))
  );
}
const liveReady = computed(() =>
  ready(
    props.providers.find((p) => p.id === live.value.provider_id),
    live.value,
  ),
);
const providerOptions = computed(() => [
  ...available.value.map((p) => ({ value: p.id, label: p.name, description: p.base_url })),
  { value: '', label: '不选择提供商' },
]);
const choices = {
  speech: ['openai_speech', 'openai_realtime', 'gemini_tts', 'seed_tts'],
  image: ['openai_images', 'chat_completions_image', 'gemini_image', 'ark_seedream'],
} as const;
const protocols = computed(() =>
  editing.value === 'standard'
    ? choices[props.kind]
    : props.kind === 'speech'
      ? (['openai_speech', 'openai_realtime'] as const)
      : (['openai_images'] as const),
);
const ratios = ['1:1', '3:2', '2:3', '4:3', '3:4', '16:9', '9:16', '5:4', '4:5', '21:9'];
const aliasId = ref(''),
  aliasName = ref('');
function selectProvider(id: string) {
  if (id === current.value.provider_id) return;
  const saved = props.route.remembered.find((p) => p.provider_id === id);
  const initial = current.value.provider_id
    ? props.catalog.presets[props.kind][editing.value]
    : current.value;
  const next = clone(saved ?? initial);
  next.provider_id = id;
  const remembered: Profile[] = props.route.remembered.filter((p) => p.provider_id !== id);
  if (current.value.provider_id) remembered.push(clone(current.value));
  Object.assign(props.route, { [editing.value]: next, remembered });
}
function selectProtocol(adapter: Adapter) {
  if (adapter === current.value.adapter) return;
  const defaults = props.catalog.presets[props.kind][editing.value];
  const { enabled: _, ...preset } = clone(props.catalog.adapters[adapter]);
  const next = defaults.adapter === adapter ? clone(defaults) : preset;
  next.provider_id = current.value.provider_id;
  next.timeout_seconds = current.value.timeout_seconds;
  if ('instructions' in next && speech.value) next.instructions = speech.value.instructions;
  Object.assign(props.route, { [editing.value]: next });
}
function saveAlias() {
  const p = speech.value;
  const id = aliasId.value.trim(),
    name = aliasName.value.trim();
  if (p?.adapter === 'openai_speech' && id && name) {
    p.voice_names = { ...p.voice_names, [id]: name };
    aliasId.value = '';
    aliasName.value = '';
  }
}
const list = useModelCatalog(
  () => ({
    provider_id: current.value.provider_id,
    api_path: current.value.api_path,
    api_mode: current.value.adapter.startsWith('gemini_') ? 'gemini' : 'chat_completions',
  }),
  () => provider.value,
  props.flush,
  () => !props.disabled && !!provider.value && current.value.adapter !== 'seed_tts',
);
const models = computed(() => {
  if (current.value.adapter === 'seed_tts') return [props.catalog.adapters.seed_tts.model, seedCloneResource];
  const names = list.catalog.value?.models || [];
  const relevant = props.kind === 'image' ? /image|banana|seedream|seededit/i : /tts|realtime|speech/i;
  return [
    ...new Set(
      [
        ...names.filter((n) => relevant.test(n)),
        props.catalog.adapters[current.value.adapter].model,
        ...names,
      ].filter(Boolean),
    ),
  ];
});
async function refreshModels() {
  await props.flush?.();
  return list.refreshMedia(current.value.api_path);
}
</script>
<template>
  <section
    :class="['media-model-card', compact ? 'compact' : 'local-model-card']"
    :aria-label="kind === 'speech' ? '角色配音' : '剧情插图'"
  >
    <header v-if="!compact">
      <strong
        ><Icon :name="kind === 'speech' ? 'activity' : 'sparkle'" :size="17" />{{
          kind === 'speech' ? '角色配音' : '剧情插图'
        }}</strong
      >
      <span class="local-model-state" :class="liveReady ? 'ready' : 'missing'">{{
        liveReady
          ? channelLabels[route.active_channel] + ' 使用中'
          : channelLabels[route.active_channel] + ' · 待配置'
      }}</span>
    </header>
    <ChannelSwitch v-model="editing" :label="kind === 'speech' ? '配音渠道' : '生图渠道'" />
    <fieldset class="media-model-form" :disabled="disabled">
      <div class="local-model-fields">
        <div class="media-choice">
          <span>提供商</span
          ><SelectMenu
            :model-value="current.provider_id"
            :options="providerOptions"
            label="提供商"
            placeholder="选择连接"
            :disabled="disabled"
            @update:model-value="selectProvider"
          />
        </div>
        <div v-if="speech" class="media-choice">
          <span>默认音色</span
          ><MediaVoiceSelect
            v-model="speech.default_voice"
            :profile="speech"
            :catalog="catalog"
            label="默认音色"
            :disabled="disabled"
          />
        </div>
        <label v-else-if="image?.adapter === 'openai_images'"
          >画质<select v-model="image.quality">
            <option value="auto">服务默认</option>
            <option value="low">低</option>
            <option value="medium">中</option>
            <option value="high">高</option>
          </select></label
        >
        <label v-else-if="image?.adapter === 'gemini_image'"
          >分辨率<select v-model="image.resolution">
            <option value="auto">服务默认</option>
            <option value="512">512 px</option>
            <option v-for="size in ['1K', '2K', '4K']" :key="size">{{ size }}</option>
          </select></label
        >
        <label v-else-if="image?.adapter === 'ark_seedream'"
          >分辨率<input v-model.trim="image.size" :list="sizeList" placeholder="2K 或 2048x1536" /><datalist
            :id="sizeList"
          >
            <option v-for="size in ['1K', '1.5K', '2K', '3K', '4K']" :key="size" :value="size" /></datalist
        ></label>
        <div class="media-choice full">
          <span>{{ current.adapter === 'seed_tts' ? '语音资源 ID' : '模型' }}</span>
          <ModelSelect
            :key="current.provider_id + current.adapter"
            v-model="current.model"
            :models="models"
            :label="kind === 'speech' ? '配音模型' : '生图模型'"
            :disabled="disabled"
            :details="list.catalog.value?.model_details"
            :refresh="provider && current.adapter !== 'seed_tts' ? refreshModels : undefined"
            :error="list.error.message"
          />
        </div>
      </div>
      <details class="media-advanced">
        <summary>高级设置<Icon name="chevron-right" :size="14" /></summary>
        <div class="local-model-fields">
          <label class="full"
            >接口协议<select
              :value="current.adapter"
              @change="selectProtocol(($event.target as HTMLSelectElement).value as Adapter)"
            >
              <option v-for="item in protocols" :key="item" :value="item">{{ adapterLabels[item] }}</option>
            </select></label
          >
          <label>API 路径<input v-model.trim="current.api_path" placeholder="服务根路径" /></label>
          <label
            >超时（秒）<input v-model.number="current.timeout_seconds" type="number" min="5" max="3600"
          /></label>
          <template v-if="speechRoute && speech">
            <label
              >每轮最多片段<input v-model.number="speechRoute.max_segments" type="number" min="1" max="100"
            /></label>
            <label
              >每轮最多字符<input
                v-model.number="speechRoute.max_characters"
                type="number"
                min="1"
                max="100000"
            /></label>
            <template v-if="speech.adapter === 'openai_speech'">
              <label
                >语速倍数<input v-model.number="speech.speed" type="number" min="0.25" max="4" step="0.05"
              /></label>
              <label class="media-check"
                ><input type="checkbox" v-model="speech.send_instructions" />发送演绎指令</label
              >
              <details class="full media-aliases">
                <summary>自定义音色名称</summary>
                <div v-for="(name, id) in speech.voice_names" :key="id" class="alias-row">
                  <span>{{ name }} · {{ id }}</span
                  ><button type="button" class="quiet" @click="delete speech.voice_names[id]">
                    移除名称
                  </button>
                </div>
                <label>音色 ID<input v-model="aliasId" /></label
                ><label>显示名称<input v-model="aliasName" /></label
                ><button type="button" :disabled="!aliasId.trim() || !aliasName.trim()" @click="saveAlias">
                  保存名称
                </button>
              </details>
            </template>
            <template v-if="speech.adapter === 'gemini_tts'">
              <label class="full"
                >演绎指令格式<select v-model="speech.instruction_format">
                  <option value="auto">
                    按模型选择{{
                      speech.instruction_format === 'auto'
                        ? metadataStyle(speech, catalog)
                          ? ' · 独立样式'
                          : ' · 提示词'
                        : ''
                    }}
                  </option>
                  <option value="speech_metadata">独立样式</option>
                  <option value="prompt">台词前的提示词（模型别名使用旧版预设音色）</option>
                </select></label
              >
              <label>朗读语言<input v-model.trim="speech.language_code" placeholder="自动 / zh-CN" /></label>
              <label
                >采样温度<input
                  :value="speech.temperature ?? ''"
                  placeholder="服务默认"
                  type="number"
                  min="0"
                  max="2"
                  step="0.1"
                  @input="
                    speech.temperature =
                      ($event.target as HTMLInputElement).value === ''
                        ? null
                        : ($event.target as HTMLInputElement).valueAsNumber
                  "
              /></label>
            </template>
            <template v-if="speech.adapter === 'seed_tts'">
              <small class="full"
                >套餐语音使用专属密钥、路径 api/v3/plan 和资源
                seed-tts-2.0。调用已复刻的音色，请使用按量语音连接，路径填 api/v3，资源选
                seed-icl-2.0。</small
              >
              <label
                >语速调整（%）<input v-model.number="speech.speech_rate" type="number" min="-50" max="100"
              /></label>
              <label
                >音量调整（%）<input v-model.number="speech.loudness_rate" type="number" min="-50" max="100"
              /></label>
              <label
                >采样率（Hz）<select v-model.number="speech.sample_rate">
                  <option v-for="rate in [8000, 16000, 22050, 24000, 32000, 44100, 48000]" :key="rate">
                    {{ rate }}
                  </option>
                </select></label
              >
            </template>
            <small v-if="isSeedClone(speech)" class="full"
              >复刻标准版会忽略随请求发送的演绎指令，语速和音量仍可调。</small
            >
            <label v-if="speech.adapter !== 'openai_speech' || speech.send_instructions" class="full"
              >语音基础指令<textarea v-model="speech.instructions" rows="3" />
            </label>
          </template>
          <template v-if="imageRoute && image">
            <label
              >每轮最多图片<input v-model.number="imageRoute.max_images" type="number" min="1" max="20"
            /></label>
            <label v-if="image.adapter === 'openai_images'"
              >输出尺寸<select v-model="image.size">
                <option value="auto">剧情模型选择</option>
                <option v-for="size in ['1024x1024', '1536x1024', '1024x1536']" :key="size">
                  {{ size }}
                </option>
              </select></label
            >
            <label v-if="image.adapter === 'gemini_image'"
              >画幅<select v-model="image.aspect_ratio">
                <option value="auto">剧情模型选择</option>
                <option v-for="ratio in ratios" :key="ratio">{{ ratio }}</option>
              </select></label
            >
            <label v-else-if="image.adapter === 'openai_images' || image.adapter === 'ark_seedream'"
              >图片格式<select v-model="image.output_format">
                <option value="auto">服务默认</option>
                <option value="png">PNG</option>
                <option value="jpeg">JPEG</option>
                <option v-if="image.adapter === 'openai_images'" value="webp">WebP</option>
              </select></label
            >
            <label v-if="image.adapter === 'ark_seedream'" class="media-check"
              ><input v-model="image.watermark" type="checkbox" />添加生成水印</label
            >
          </template>
        </div>
      </details>
    </fieldset>
    <div class="media-card-actions">
      <button type="button" :aria-expanded="preview" @click="preview = !preview">
        <Icon :name="kind === 'speech' ? 'play' : 'sparkle'" :size="14" />{{
          kind === 'speech' ? '试听' : '试生成图片'
        }}
      </button>
      <button
        v-if="editing !== route.active_channel && !disabled"
        type="button"
        :disabled="!ready(provider, current)"
        @click="route.active_channel = editing"
      >
        使用此渠道
      </button>
      <small v-else-if="editing !== route.active_channel"
        >使用中 · {{ channelLabels[route.active_channel] }}</small
      >
      <small v-if="missingKey">请在提供商设置中填写此连接的 API Key。</small>
    </div>
    <MediaPreview
      v-if="preview"
      :kind="kind"
      :channel="editing"
      :profile="current"
      :provider-name="provider?.name"
      :missing-key="missingKey"
      :catalog="catalog"
      :flush="flush"
    />
  </section>
</template>
<style scoped>
.media-model-card {
  min-width: 0;
}
.media-model-card header strong,
.media-card-actions,
.alias-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.media-model-card header strong .icon {
  color: var(--accent);
}
.media-model-card .channel-switch {
  margin: 0;
}
.media-model-card :deep(.channel-switch button) {
  flex: 1;
  padding: 7px 6px;
  font-size: var(--fs-sm);
}
.media-model-form {
  min-width: 0;
  border: 0;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 12px;
}
.media-choice,
.media-model-form label {
  display: grid;
  gap: 5px;
  min-width: 0;
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
.media-choice :deep(.select-menu),
.media-model-form :is(input:not([type='checkbox']), select, textarea) {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  margin: 0;
}
.full {
  grid-column: 1/-1;
}
.media-advanced {
  border: 0;
  border-top: 1px solid var(--border-subtle);
  padding: 0;
}
.media-advanced > summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  list-style: none;
  cursor: pointer;
  padding: 10px 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
.media-advanced > summary::-webkit-details-marker {
  display: none;
}
.media-advanced[open] > summary .icon {
  rotate: 90deg;
}
.media-advanced > .local-model-fields {
  margin-top: 12px;
}
.media-model-form .media-check {
  display: flex;
  align-items: center;
  flex-direction: row;
  align-self: end;
  gap: 8px;
  min-height: 32px;
}
.media-card-actions {
  flex-wrap: wrap;
}
.media-card-actions button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.media-card-actions small,
.media-model-form small {
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.media-aliases > * {
  margin-top: 6px;
}
.alias-row {
  justify-content: space-between;
  overflow-wrap: anywhere;
}
.compact {
  display: grid;
  gap: 10px;
}
.compact .local-model-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
</style>
