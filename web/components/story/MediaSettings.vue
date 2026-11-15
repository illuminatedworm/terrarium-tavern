<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { api } from '../../lib/api';
import { clone } from '../../lib/settingsDocument';
import { mediaUrl, type MediaAsset, type MediaConfig } from '../../lib/media';
import type { Session } from '../../lib/types';
import Icon from '../common/Icon.vue';

const props = defineProps<{ session: Session; disabled: boolean; providerSettingsOpen: boolean }>();
const emit = defineEmits<{ saved: [session: Session]; providers: [] }>();
const draft = ref<MediaConfig>(clone(props.session.media_config));
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(props.session.media_config));
const busy = ref(false);
const loading = ref(false);
const loadError = ref('');
const error = ref('');
const notice = ref('');
const speaker = ref('');
const newVoice = ref('marin');
const selectedCard = ref('');
const referenceName = ref('');
const referenceDescription = ref('');
const file = ref<HTMLInputElement>();
type Catalog = {
  voices: string[];
  providers: Array<{ id: string; name: string; base_url: string }>;
  references: MediaAsset[];
  card_images: string[];
};
const catalog = ref<Catalog>();
let loadRequest = 0;
watch(
  () => JSON.stringify(props.session.media_config),
  () => {
    draft.value = clone(props.session.media_config);
  },
);
watch(
  () => props.providerSettingsOpen,
  (open) => {
    if (!open) void load('providers');
  },
);
onMounted(load);

async function load(scope: 'all' | 'providers' = 'all') {
  const request = ++loadRequest;
  loading.value = true;
  loadError.value = '';
  try {
    const result: Catalog = await api(`/sessions/${props.session.id}/media`);
    if (request !== loadRequest) return;
    // Returning from provider settings must not replace edits to reference names and descriptions.
    if (scope === 'providers' && catalog.value) catalog.value.providers = result.providers;
    else catalog.value = result;
  } catch (failure) {
    if (request === loadRequest) loadError.value = String(failure);
  } finally {
    if (request === loadRequest) loading.value = false;
  }
}
async function saveRequest(path: string, body: unknown, method = 'POST') {
  busy.value = true;
  error.value = '';
  notice.value = '';
  try {
    const updated: Session = await api(`/sessions/${props.session.id}/media${path}`, body, method);
    emit('saved', updated);
    notice.value = '已保存';
    await load();
    return true;
  } catch (failure) {
    error.value = String(failure);
    return false;
  } finally {
    busy.value = false;
  }
}
function save() {
  return saveRequest('', { expected_revision: props.session.revision, config: draft.value }, 'PUT');
}
function addSpeaker() {
  if (!speaker.value.trim()) return;
  draft.value.voices[speaker.value.trim()] = newVoice.value;
  speaker.value = '';
}
async function upload() {
  const chosen = file.value?.files?.[0];
  if (!chosen) return;
  if (chosen.size > 32 * 1024 * 1024) {
    error.value = '参考图不能超过 32 MiB';
    return;
  }
  const body = new FormData();
  body.append('file', chosen);
  body.append('expected_revision', String(props.session.revision));
  body.append('name', referenceName.value || chosen.name);
  body.append('description', referenceDescription.value);
  if ((await saveRequest('/references', body)) && file.value) file.value.value = '';
}
function addCardImage() {
  return saveRequest('/card-reference', {
    expected_revision: props.session.revision,
    key: selectedCard.value,
    name: referenceName.value || selectedCard.value,
    description: referenceDescription.value,
    selected: true,
  });
}
function saveReference(reference: MediaAsset, selected: boolean) {
  return saveRequest(
    `/references/${reference.id}`,
    {
      expected_revision: props.session.revision,
      name: reference.name,
      description: reference.description,
      selected,
    },
    'PUT',
  );
}
</script>

<template>
  <section class="media-settings" aria-labelledby="media-settings-title">
    <header class="media-settings-head">
      <h2 id="media-settings-title"><Icon name="media" :size="20" />配音与生图</h2>
      <button
        type="button"
        aria-label="管理 API 连接与密钥"
        title="管理 API 连接与密钥"
        @click="emit('providers')"
      >
        <Icon name="external" :size="14" />API 连接
      </button>
      <p>仅影响当前故事 · 保存后生效</p>
    </header>

    <p class="media-cost-note">
      <Icon name="info" :size="14" />配音和生图在正文提交后开始，可能产生 API 费用。
    </p>
    <p v-if="loading && !catalog" role="status" class="muted">正在加载媒体设置…</p>
    <div v-if="loadError" class="media-load-error">
      <p role="alert" class="media-settings-error">{{ loadError }}</p>
      <button type="button" class="quiet" @click="load()">重新加载</button>
    </div>

    <template v-if="catalog">
      <section class="media-card" aria-labelledby="media-speech-title">
        <header class="media-card-head">
          <h3 id="media-speech-title"><Icon name="activity" :size="16" />角色配音</h3>
          <small>OpenAI Realtime</small>
        </header>
        <fieldset
          class="media-fields media-card-body"
          :disabled="disabled || busy"
          aria-labelledby="media-speech-title"
        >
          <label class="media-check">
            <span>允许模型安排角色配音</span>
            <input v-model="draft.speech.enabled" type="checkbox" />
          </label>
          <details class="media-disclosure media-config">
            <summary>连接、额度与声线<Icon name="chevron-right" :size="14" /></summary>
            <div class="media-fields">
              <label>
                连接
                <select v-model="draft.speech.provider_id">
                  <option value="">请选择 API Key 连接</option>
                  <option v-for="provider in catalog.providers" :key="provider.id" :value="provider.id">
                    {{ provider.name }} · {{ provider.base_url }}
                  </option>
                </select>
              </label>
              <label>语音模型<input v-model="draft.speech.model" /></label>
              <label>语音基础指令<textarea v-model="draft.speech.instructions" rows="3" /></label>
              <small>实际语音指令由这份已保存文本与模型本次的演绎要求组成，可在任务详情查看。</small>
              <label>API 路径<input v-model="draft.speech.api_path" placeholder="v1" /></label>
              <label>
                单段生成超时（秒）
                <input v-model.number="draft.speech.timeout_seconds" type="number" min="5" max="3600" />
              </label>
              <label>
                每轮最多片段数
                <input v-model.number="draft.max_speech_segments" type="number" min="1" max="100" />
              </label>
              <label>
                每轮最多配音字符
                <input v-model.number="draft.max_speech_characters" type="number" min="1" max="100000" />
              </label>
              <small>按 Unicode 字符计数，包含空格和标点。配音文本由模型直接提供，不与正文核对。</small>

              <details class="media-disclosure">
                <summary>角色声线<Icon name="chevron-right" :size="14" /></summary>
                <div class="media-fields">
                  <small>已有绑定由你修改，新角色可由模型首次分配。</small>
                  <div v-for="(_, name) in draft.voices" :key="name" class="media-voice">
                    <span>{{ name }}</span>
                    <select v-model="draft.voices[name]" :aria-label="name + ' 的声音'">
                      <option v-for="voice in catalog.voices" :key="voice">{{ voice }}</option>
                    </select>
                    <button type="button" class="quiet" @click="delete draft.voices[name]">移除绑定</button>
                  </div>
                  <label>新增角色<input v-model="speaker" placeholder="角色名或旁白" /></label>
                  <label>
                    声音
                    <select v-model="newVoice">
                      <option v-for="voice in catalog.voices" :key="voice">{{ voice }}</option>
                    </select>
                  </label>
                  <button type="button" class="quiet" :disabled="!speaker.trim()" @click="addSpeaker">
                    加入配音表
                  </button>
                </div>
              </details>
            </div>
          </details>
        </fieldset>
      </section>

      <section class="media-card" aria-labelledby="media-image-title">
        <header class="media-card-head">
          <h3 id="media-image-title"><Icon name="sparkle" :size="16" />剧情插图</h3>
          <small>OpenAI Images</small>
        </header>
        <fieldset
          class="media-fields media-card-body"
          :disabled="disabled || busy"
          aria-labelledby="media-image-title"
        >
          <label class="media-check">
            <span>允许模型安排生图</span>
            <input v-model="draft.image.enabled" type="checkbox" />
          </label>
          <label class="media-check">
            <span>允许当前游玩模型主动查看授权图片</span>
            <input v-model="draft.view_images" type="checkbox" />
          </label>
          <small class="media-view-hint">看图占用当前游玩模型额度，需支持图片输入。</small>
          <details class="media-disclosure media-config">
            <summary>连接、画质与额度<Icon name="chevron-right" :size="14" /></summary>
            <div class="media-fields">
              <label>
                连接
                <select v-model="draft.image.provider_id">
                  <option value="">请选择 API Key 连接</option>
                  <option v-for="provider in catalog.providers" :key="provider.id" :value="provider.id">
                    {{ provider.name }} · {{ provider.base_url }}
                  </option>
                </select>
              </label>
              <label>生图模型<input v-model="draft.image.model" /></label>
              <label>API 路径<input v-model="draft.image.api_path" placeholder="v1" /></label>
              <label>
                画质（由你控制）
                <select v-model="draft.image.quality">
                  <option value="low">低</option>
                  <option value="medium">中</option>
                  <option value="high">高</option>
                  <option value="auto">由生图服务自动选择</option>
                </select>
              </label>
              <label>
                单张生成超时（秒）
                <input v-model.number="draft.image.timeout_seconds" type="number" min="5" max="3600" />
              </label>
              <label>
                每轮最多图片数<input v-model.number="draft.max_images" type="number" min="1" max="20" />
              </label>
              <small>尺寸、横竖比例与构图由剧情模型选择；实际参数可在产物详情中查看。</small>
            </div>
          </details>
        </fieldset>
      </section>

      <details
        class="media-card media-disclosure media-library"
        @toggle="($event.target as HTMLDetailsElement).open && load()"
      >
        <summary>
          <span class="media-library-cover"><Icon name="layers" :size="20" /></span>
          <span class="media-library-title">
            <strong>参考图库</strong>
            <small>{{ catalog.references.length }} 张 · 只发送你选定的图片</small>
          </span>
          <Icon name="chevron-right" :size="14" />
        </summary>
        <div class="media-fields">
          <button type="button" class="quiet" :disabled="busy || loading" @click="load()">
            <Icon name="refresh" :size="14" />刷新参考图库
          </button>
          <fieldset class="media-fields" :disabled="disabled || busy">
            <legend>加入参考图</legend>
            <label
              >图片名称<input v-model="referenceName" placeholder="留空使用文件名" maxlength="120"
            /></label>
            <label>
              用途说明<textarea v-model="referenceDescription" rows="2" placeholder="例如：艾琳的外貌参考" />
            </label>
            <label>
              上传 PNG / JPEG / 静态 WebP（最多 32 MiB）
              <input ref="file" type="file" accept="image/png,image/jpeg,image/webp" />
            </label>
            <button type="button" class="quiet" @click="upload">上传并授权</button>
            <label>
              当前角色卡图片
              <select v-model="selectedCard">
                <option value="">选择卡片图片</option>
                <option v-for="name in catalog.card_images" :key="name">{{ name }}</option>
              </select>
            </label>
            <button type="button" class="quiet" :disabled="!selectedCard" @click="addCardImage">
              加入参考图库
            </button>
          </fieldset>

          <details
            v-for="reference in catalog.references"
            :key="reference.id"
            class="media-disclosure media-reference"
          >
            <summary>
              <span>
                {{ reference.ref }} · {{ reference.name }}
                <small>{{ reference.selected ? '常驻参考' : '未选为常驻参考' }}</small>
              </span>
              <Icon name="chevron-right" :size="14" />
            </summary>
            <fieldset
              class="media-fields"
              :disabled="disabled || busy"
              :aria-label="'编辑参考图 ' + reference.name"
            >
              <img :src="mediaUrl(reference)" :alt="reference.name" loading="lazy" />
              <label>名称<input v-model="reference.name" maxlength="120" /></label>
              <label>说明<textarea v-model="reference.description" rows="2" /></label>
              <button type="button" class="quiet" @click="saveReference(reference, !!reference.selected)">
                保存名称与说明
              </button>
              <button type="button" class="quiet" @click="saveReference(reference, !reference.selected)">
                {{ reference.selected ? '取消常驻参考' : '设为常驻参考' }}
              </button>
              <small>当前剧情中的已生成图片仍可查看；已经送入模型的历史图片记录保留。</small>
            </fieldset>
          </details>
        </div>
      </details>
    </template>

    <div v-if="dirty || error || notice" class="media-settings-feedback">
      <p v-if="error" role="alert" class="media-settings-error">{{ error }}</p>
      <div v-if="dirty" class="media-settings-save">
        <span>有未保存修改</span>
        <button type="button" class="primary" :disabled="disabled || busy" @click="save">保存媒体设置</button>
      </div>
      <p v-else-if="notice && !error" role="status" class="media-settings-saved">{{ notice }}</p>
    </div>
  </section>
</template>

<style scoped>
.media-settings {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  min-height: 100%;
  font-size: var(--fs-sm);
}
.media-settings button {
  font-size: var(--fs-sm);
}
.media-settings-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 6px 10px;
}
.media-settings-head h2 {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 0;
  font-size: var(--fs-xl);
}
.media-settings-head h2 .icon,
.media-card-head .icon {
  flex: none;
  color: var(--accent);
}
.media-settings-head button {
  padding: 5px 9px;
  font-size: var(--fs-xs);
  white-space: nowrap;
}
.media-settings-head p {
  flex-basis: 100%;
  margin: 0 0 0 29px;
  color: var(--text-muted);
}
.media-settings > p,
.media-load-error p {
  margin: 0;
}
.media-cost-note {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  color: var(--text-secondary);
}
.media-cost-note .icon {
  flex: none;
  margin-top: 2px;
  color: var(--warning);
}
.media-load-error {
  display: grid;
  justify-items: start;
  gap: 6px;
}
.media-disclosure {
  padding: 0;
  border-bottom: 0;
}
.media-card {
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: rgb(255 255 255 / 0.03);
}
.media-card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 4px 8px;
  min-height: 42px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border-subtle);
  border-radius: calc(var(--radius-md) - 1px) calc(var(--radius-md) - 1px) 0 0;
  background: rgb(255 255 255 / 0.02);
}
.media-card-head h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: var(--fs-md);
}
.media-card small {
  font-size: var(--fs-xs);
}
.media-fields {
  display: grid;
  gap: 10px;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}
.media-card-body {
  gap: 0;
  padding-inline: 12px;
}
.media-fields legend {
  margin-bottom: 8px;
  padding: 0;
  font-weight: 600;
}
.media-fields > button {
  justify-self: start;
}
.media-fields label {
  display: grid;
  gap: 4px;
  min-width: 0;
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
.media-fields :is(input, select, textarea) {
  min-width: 0;
  max-width: 100%;
  margin: 0;
}
.media-fields .media-check {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 36px;
  padding-block: 5px;
  color: var(--text-primary);
}
.media-check + .media-check {
  border-top: 1px solid var(--border-subtle);
}
.media-check:has(:enabled) {
  cursor: pointer;
}
.media-check input {
  flex: none;
}
.media-view-hint {
  margin: 0 28px 8px 0;
}
.media-disclosure > summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  min-height: 34px;
  list-style: none;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.media-disclosure > summary::-webkit-details-marker {
  display: none;
}
.media-disclosure > summary:hover,
.media-library-title strong {
  color: var(--text-primary);
}
.media-disclosure > summary > .icon {
  flex: none;
}
.media-disclosure[open] > summary > .icon {
  rotate: 90deg;
}
.media-disclosure > .media-fields {
  margin-top: 8px;
}
.media-disclosure > summary small {
  display: block;
  margin-top: 2px;
}
.media-config {
  border-top: 1px solid var(--border-subtle);
}
.media-config > .media-fields {
  margin-bottom: 12px;
}
.media-library {
  border-color: var(--glass-edge-strong);
  background: var(--glass-sheen), rgb(255 255 255 / 0.02);
}
.media-library > summary {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) auto;
  gap: 0;
  min-height: 62px;
  padding-right: 12px;
  border-radius: inherit;
}
.media-library-cover {
  display: grid;
  place-items: center;
  align-self: stretch;
  border-right: 1px solid var(--glass-edge);
  border-radius: calc(var(--radius-md) - 1px) 0 0 calc(var(--radius-md) - 1px);
  background: var(--glass-sheen), var(--accent-soft);
  color: var(--accent);
}
.media-library[open] .media-library-cover {
  border-bottom-left-radius: 0;
}
.media-library-title {
  padding: 10px 12px;
}
.media-library-title strong {
  font-size: var(--fs-md);
}
.media-library > .media-fields {
  margin: 0;
  padding: 12px;
  border-top: 1px solid var(--border-subtle);
}
.media-voice {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  overflow-wrap: anywhere;
}
.media-voice select {
  width: auto;
}
.media-reference {
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}
.media-reference img {
  display: block;
  max-width: 100%;
  max-height: 12rem;
  object-fit: contain;
}
.media-settings-feedback {
  position: sticky;
  bottom: 0;
  z-index: 1;
  display: grid;
  gap: 6px;
  margin-top: auto;
  padding: 8px 10px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-sm);
  background: var(--surface-panel);
}
.media-settings-feedback p {
  margin: 0;
}
.media-settings-save {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  color: var(--warning);
}
.media-settings-saved {
  color: var(--success);
}
.media-settings-error {
  color: var(--danger);
  overflow-wrap: anywhere;
}
</style>
