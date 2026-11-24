<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue';
import { api } from '../../lib/api';
import { channelLabels } from '../../lib/channels';
import { notify } from '../../lib/notifications';
import { clone, type Provider } from '../../lib/settingsDocument';
import {
  mediaUrl,
  type MediaProtocols,
  type MediaDefaults,
  type MediaSelection,
  type MediaKind,
  type MediaAsset,
  type ModelKey,
} from '../../lib/media';
import type { Session } from '../../lib/types';
import Icon from '../common/Icon.vue';
import MediaGenerationFields from '../settings/MediaGenerationFields.vue';
import RoleVoiceTable from './RoleVoiceTable.vue';
const props = defineProps<{ session: Session; disabled: boolean; active: boolean }>();
const emit = defineEmits<{ saved: [session: Session]; settings: [] }>();
type Catalog = MediaProtocols & {
  providers: Provider[];
  defaults: MediaDefaults;
  selection: MediaSelection;
  selection_token: string;
  story_model: ModelKey;
  story_provider_name: string;
  revision: number;
};
type Library = {
  revision: number;
  references: MediaAsset[];
  card_images: string[];
};
type ReferenceText = Pick<MediaAsset, 'name' | 'description'>;
type LoadScope = 'all' | 'settings';
const catalog = ref<Catalog>();
const library = shallowRef<Library>();
const libraryOpen = ref(false);
const selectedCount = computed(() => library.value?.references.filter((item) => item.selected).length ?? 0);
const draft = ref<MediaSelection>();
const baseline = ref('');
const revision = ref(0);
const token = ref('');
const conflict = ref(false);
const dirty = computed(() => !!draft.value && JSON.stringify(draft.value) !== baseline.value);
const busy = ref(false);
const loadingScope = ref<LoadScope>();
const loading = computed(() => loadingScope.value !== undefined);
const error = ref(''),
  loadError = ref('');
const selectedCard = ref(''),
  referenceName = ref(''),
  referenceDescription = ref('');
const file = ref<HTMLInputElement>();
const chosenFile = shallowRef<File>();
const referenceEdits = reactive(new Map<string, Partial<ReferenceText>>());
const scratch: Partial<MediaDefaults> = {};
const routes = computed<MediaDefaults | undefined>(
  () =>
    draft.value &&
    catalog.value && {
      speech: draft.value.speech.custom ?? catalog.value.defaults.speech,
      image: draft.value.image.custom ?? catalog.value.defaults.image,
    },
);
function changeSource(kind: MediaKind, custom: boolean) {
  if (!draft.value || !catalog.value) return;
  if (kind === 'speech') {
    const s = draft.value.speech;
    if (s.custom) scratch.speech = s.custom;
    s.custom = custom ? clone(scratch.speech ?? catalog.value.defaults.speech) : null;
  } else {
    const i = draft.value.image;
    if (i.custom) scratch.image = i.custom;
    i.custom = custom ? clone(scratch.image ?? catalog.value.defaults.image) : null;
  }
}
let loadRequest = 0;
let disposed = false;
async function load(scope: LoadScope = 'all', discard = false) {
  const request = ++loadRequest;
  // A revision or focus refresh can supersede a library read, so it must also fetch the library.
  const includeLibrary = scope === 'all' || !library.value || loadingScope.value === 'all';
  loadingScope.value = includeLibrary ? 'all' : 'settings';
  loadError.value = '';
  try {
    const { references, card_images, ...result }: Catalog & Library = await api(
      `/sessions/${props.session.id}/media${includeLibrary ? '' : '?library=false'}`,
    );
    if (request !== loadRequest) return false;
    if (includeLibrary) {
      library.value = { references, card_images, revision: result.revision };
      if (!card_images.includes(selectedCard.value)) selectedCard.value = '';
    }
    const incoming = JSON.stringify(result.selection);
    // Other tabs and committed turns can add bindings while this editor is open.
    if (discard || !dirty.value) {
      draft.value = clone(result.selection);
      baseline.value = incoming;
      conflict.value = false;
      delete scratch.speech;
      delete scratch.image;
    } else conflict.value = incoming !== baseline.value;
    if (!conflict.value) {
      revision.value = result.revision;
      token.value = result.selection_token;
    }
    catalog.value = result;
    return true;
  } catch (e) {
    if (request === loadRequest) loadError.value = String(e);
    return false;
  } finally {
    if (request === loadRequest) loadingScope.value = undefined;
  }
}
function refreshSettings() {
  if (props.active && !busy.value) void load(libraryOpen.value ? 'all' : 'settings');
}
function toggleLibrary(event: Event) {
  libraryOpen.value = (event.currentTarget as HTMLDetailsElement).open;
  if (libraryOpen.value && !busy.value) void load();
}
watch(
  [
    () => props.active,
    () => props.session.revision,
    () => props.session.media_story_model.provider_id,
    () => props.session.media_story_model.model,
  ],
  ([active], [wasActive]) => {
    if (active && !wasActive && !busy.value) void load();
    else refreshSettings();
  },
);
onMounted(() => {
  window.addEventListener('focus', refreshSettings);
  void load();
});
onBeforeUnmount(() => {
  disposed = true;
  ++loadRequest;
  window.removeEventListener('focus', refreshSettings);
});
async function saveRequest(path: string, body: unknown, method = 'POST') {
  if (busy.value) return false;
  busy.value = true;
  error.value = '';
  try {
    const updated: Session = await api('/sessions/' + props.session.id + '/media' + path, body, method);
    if (disposed) return false;
    emit('saved', updated);
    if (!path) {
      baseline.value = JSON.stringify(draft.value);
      revision.value = updated.revision;
    }
    const refreshed = await load(path || libraryOpen.value ? 'all' : 'settings', !path);
    if (disposed) return false;
    if (refreshed) notify('已保存');
    else loadError.value = `已保存，但刷新失败。${loadError.value}`;
    return true; // The write succeeded even if reloading its result failed.
  } catch (e) {
    if (disposed) return false;
    error.value = String(e);
    await load(path ? 'all' : 'settings');
    return false;
  } finally {
    busy.value = false;
  }
}
async function discardDraft() {
  busy.value = true;
  try {
    await load(libraryOpen.value ? 'all' : 'settings', true);
  } finally {
    busy.value = false;
  }
}
function save() {
  return saveRequest(
    '',
    { expected_revision: revision.value, expected_selection: token.value, config: draft.value },
    'PUT',
  );
}
async function upload() {
  const chosen = chosenFile.value;
  if (!chosen) return;
  if (chosen.size > 32 * 1024 * 1024) {
    error.value = '参考图不能超过 32 MiB';
    return;
  }
  const body = new FormData();
  body.append('file', chosen);
  body.append('expected_revision', String(library.value!.revision));
  body.append('name', referenceName.value.trim() || chosen.name);
  body.append('description', referenceDescription.value);
  if (await saveRequest('/references', body)) {
    file.value!.value = '';
    chosenFile.value = undefined;
    referenceName.value = '';
    referenceDescription.value = '';
  }
}
async function addCardImage() {
  if (
    await saveRequest('/card-reference', {
      expected_revision: library.value!.revision,
      key: selectedCard.value,
      name: referenceName.value.trim() || selectedCard.value,
      description: referenceDescription.value,
      selected: true,
    })
  ) {
    selectedCard.value = '';
    referenceName.value = '';
    referenceDescription.value = '';
  }
}
function editReference(reference: MediaAsset, field: keyof ReferenceText, value: string) {
  const edits = referenceEdits.get(reference.id) ?? {};
  if (value === reference[field]) delete edits[field];
  else edits[field] = value;
  if (edits.name === undefined && edits.description === undefined) referenceEdits.delete(reference.id);
  else referenceEdits.set(reference.id, edits);
}
function saveReference(reference: MediaAsset, change: Partial<ReferenceText & { selected: boolean }>) {
  return saveRequest(
    `/references/${reference.id}`,
    {
      expected_revision: library.value!.revision,
      name: reference.name,
      description: reference.description,
      selected: !!reference.selected,
      ...change,
    },
    'PUT',
  );
}
async function saveReferenceText(reference: MediaAsset) {
  if (await saveReference(reference, referenceEdits.get(reference.id)!)) referenceEdits.delete(reference.id);
}
</script>

<template>
  <section class="media-settings" aria-labelledby="media-settings-title">
    <header class="media-settings-head">
      <h2 id="media-settings-title"><Icon name="media" :size="20" />配音与生图</h2>
      <button type="button" aria-label="编辑全局媒体设置" title="编辑全局媒体设置" @click="emit('settings')">
        <Icon name="external" :size="14" />全局设置
      </button>
      <p>本故事的开关、角色声线和参考图库在此保存。</p>
    </header>

    <p class="media-cost-note">
      <Icon name="info" :size="14" />配音和生图在正文提交后开始，可能产生 API 费用。
    </p>
    <p v-if="loading && !catalog" role="status" class="muted">正在加载媒体设置…</p>
    <template v-if="catalog && draft && routes && library">
      <section
        v-for="kind in ['speech', 'image'] as const"
        :key="kind"
        class="media-card"
        :aria-label="kind === 'speech' ? '角色配音' : '剧情插图'"
      >
        <header class="media-card-head">
          <h3>
            <Icon :name="kind === 'speech' ? 'activity' : 'sparkle'" :size="16" />{{
              kind === 'speech' ? '角色配音' : '剧情插图'
            }}
          </h3>
          <small
            >{{ draft[kind].custom ? '本故事' : '跟随全局' }} ·
            {{ channelLabels[routes[kind].active_channel] }}</small
          >
        </header>
        <fieldset class="media-fields media-card-body" :disabled="disabled || busy">
          <label class="media-check"
            ><span>{{ kind === 'speech' ? '允许模型安排角色配音' : '允许模型安排生图' }}</span
            ><input v-model="draft[kind].enabled" type="checkbox"
          /></label>
          <template v-if="kind === 'image'">
            <label class="media-check"
              ><span>允许剧情模型查看授权图片</span><input v-model="draft.view_images" type="checkbox"
            /></label>
            <small class="media-view-hint">看图使用剧情模型额度，需支持图片输入。</small>
          </template>
          <details class="media-disclosure media-config">
            <summary>渠道与生成设置<Icon name="chevron-right" :size="14" /></summary>
            <div class="media-fields">
              <label
                >设置来源<select
                  :value="draft[kind].custom ? 'story' : 'global'"
                  @change="changeSource(kind, ($event.target as HTMLSelectElement).value === 'story')"
                >
                  <option value="global">跟随全局设置</option>
                  <option value="story">本故事自定义</option>
                </select></label
              >
              <small v-if="catalog.selection[kind].custom && !draft[kind].custom"
                >保存后将移除这一项的故事自定义设置。</small
              >
              <MediaGenerationFields
                :key="kind + !!draft[kind].custom"
                :kind="kind"
                :route="routes[kind]"
                :catalog="catalog"
                :providers="catalog.providers"
                :disabled="disabled || busy || !draft[kind].custom"
                compact
              />
            </div>
          </details>
          <details v-if="kind === 'speech'" class="media-disclosure media-config">
            <summary>角色音色表<Icon name="chevron-right" :size="14" /></summary>
            <div class="media-fields">
              <RoleVoiceTable
                :tables="draft.voice_tables"
                :story="catalog.story_model"
                :story-provider-name="catalog.story_provider_name"
                :speech="routes.speech[routes.speech.active_channel]"
                :catalog="catalog"
                :providers="catalog.providers"
                :disabled="disabled || busy"
                @change="draft.voice_tables = $event"
              />
            </div>
          </details>
        </fieldset>
      </section>
      <details class="media-card media-disclosure media-library" @toggle="toggleLibrary">
        <summary>
          <span class="media-library-cover"><Icon name="layers" :size="20" /></span>
          <span class="media-library-title">
            <strong>参考图库</strong>
            <small>{{ library.references.length }} 张 · {{ selectedCount }} 张常驻参考</small>
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
              <input
                ref="file"
                type="file"
                accept="image/png,image/jpeg,image/webp"
                @change="chosenFile = ($event.target as HTMLInputElement).files?.[0]"
              />
            </label>
            <button type="button" class="quiet" :disabled="!chosenFile || loading" @click="upload">
              上传并授权
            </button>
            <label>
              当前角色卡图片
              <select v-model="selectedCard" :disabled="!library.card_images.length">
                <option value="">
                  {{ library.card_images.length ? '选择卡片图片' : '当前角色卡没有可用图片' }}
                </option>
                <option v-for="name in library.card_images" :key="name">{{ name }}</option>
              </select>
            </label>
            <button type="button" class="quiet" :disabled="!selectedCard || loading" @click="addCardImage">
              加入参考图库
            </button>
          </fieldset>

          <details
            v-for="reference in library.references"
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
              <label>
                名称<input
                  :value="referenceEdits.get(reference.id)?.name ?? reference.name"
                  maxlength="120"
                  @input="editReference(reference, 'name', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label>
                说明<textarea
                  :value="referenceEdits.get(reference.id)?.description ?? reference.description"
                  rows="2"
                  @input="
                    editReference(reference, 'description', ($event.target as HTMLTextAreaElement).value)
                  "
                />
              </label>
              <button
                type="button"
                class="quiet"
                :disabled="
                  loading ||
                  !referenceEdits.has(reference.id) ||
                  !(referenceEdits.get(reference.id)?.name ?? reference.name).trim()
                "
                @click="saveReferenceText(reference)"
              >
                保存名称与说明
              </button>
              <button
                type="button"
                class="quiet"
                :disabled="loading"
                @click="saveReference(reference, { selected: !reference.selected })"
              >
                {{ reference.selected ? '取消常驻参考' : '设为常驻参考' }}
              </button>
              <small>当前剧情中的已生成图片仍可查看；已经送入模型的历史图片记录保留。</small>
            </fieldset>
          </details>
        </div>
      </details>
    </template>

    <div v-if="dirty || busy || error || loadError" class="media-settings-feedback">
      <p v-if="error" role="alert" class="media-settings-error">{{ error }}</p>
      <div v-if="loadError" class="media-load-error">
        <p role="alert" class="media-settings-error">{{ loadError }}</p>
        <button type="button" class="quiet" :disabled="busy || loading" @click="load()">重新加载</button>
      </div>
      <p v-if="busy" role="status">{{ loading ? '正在刷新媒体数据…' : '正在保存…' }}</p>
      <div v-if="dirty" class="media-settings-save">
        <span>{{ conflict ? '故事中的媒体设置已更新。重新载入会舍弃本页草稿。' : '有未保存修改' }}</span>
        <button v-if="conflict" type="button" :disabled="busy || loading" @click="discardDraft">
          重新载入
        </button>
        <button
          type="button"
          class="primary"
          :disabled="disabled || busy || loading || conflict"
          @click="save"
        >
          保存媒体设置
        </button>
      </div>
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
.media-settings-error {
  color: var(--danger);
  overflow-wrap: anywhere;
}
</style>
