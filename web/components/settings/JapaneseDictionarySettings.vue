<script setup lang="ts">
import { inject, onBeforeUnmount, ref } from 'vue';
import { japaneseDictionaryKey } from '../../composables/useJapaneseDictionary';
import { uploadResource } from '../../lib/api';
const { status, busy, label, progress, connectionError, action } = inject(japaneseDictionaryKey)!;
const props = defineProps<{ settings: any; flush: () => Promise<unknown> }>();
const fileInput = ref<HTMLInputElement>();
const error = ref('');
const pending = ref(false);
const uploadProgress = ref(0);
const controller = ref<AbortController>();
async function download() {
  pending.value = true;
  error.value = '';
  try {
    await props.flush();
    await action('download');
  } catch (e) {
    error.value = String(e);
  } finally {
    pending.value = false;
  }
}
async function cancel() {
  controller.value?.abort();
  try {
    await action('cancel');
  } catch (e) {
    error.value = String(e);
  }
}
async function importFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  const kind = file.name.toLowerCase().endsWith('.dic') ? 'dic' : 'wheel';
  error.value = '';
  controller.value = new AbortController();
  uploadProgress.value = 0;
  try {
    await uploadResource(
      `/dictionaries/japanese/import?kind=${kind}`,
      file,
      controller.value.signal,
      (value) => {
        uploadProgress.value = value;
      },
      '词典',
    );
  } catch (e) {
    if (!(e instanceof DOMException && e.name === 'AbortError')) error.value = String(e);
  } finally {
    controller.value = undefined;
    input.value = '';
  }
}
onBeforeUnmount(() => controller.value?.abort());
</script>

<template>
  <p v-if="!status && connectionError" role="alert">{{ connectionError }}</p>
  <section v-if="status?.platform === 'desktop'" class="local-model-card" aria-label="日文分词词典">
    <header>
      <strong>日文分词词典</strong><span class="local-model-state" role="status">{{ label }}</span>
    </header>
    <p class="muted">
      Sudachi core {{ status.version }} · 下载 {{ (status.download_bytes / 1048576).toFixed(1) }} MiB，落盘
      {{ (status.dictionary_bytes / 1048576).toFixed(1) }}
      MiB。首次日文分词（包括世界书索引）自动准备并复用；纯中英文无需词典。可提前准备，或导入下方指定的 wheel
      / system.dic。
    </p>
    <p class="muted">
      使用“联网与代理”的本地资源下载代理开关（当前{{
        settings.network.hf_proxy && settings.network.proxy ? '使用代理' : '直连'
      }}）。失败或取消后须在此重试。
    </p>
    <div class="actions">
      <button :disabled="busy || pending || !!controller || status.state === 'ready'" @click="download">
        {{ ['error', 'cancelled'].includes(status.state) ? '重试准备词典' : '提前准备词典' }}
      </button>
      <input
        ref="fileInput"
        hidden
        type="file"
        accept=".whl,.dic"
        aria-label="导入日文词典"
        @change="importFile"
      />
      <button
        :disabled="busy || pending || !!controller || status.state === 'ready'"
        @click="fileInput?.click()"
      >
        离线导入词典
      </button>
      <button v-if="busy || controller" @click="cancel">取消准备</button>
    </div>
    <div v-if="busy || controller" class="local-job-progress">
      <progress
        :value="progress ?? (controller ? uploadProgress : undefined)"
        max="1"
        aria-label="日文词典准备进度"
      />
      <span role="status"
        >{{ label
        }}<template v-if="status.total_bytes">
          · {{ (status.completed_bytes / 1048576).toFixed(1) }} /
          {{ (status.total_bytes / 1048576).toFixed(1) }} MiB</template
        ></span
      >
    </div>
    <p v-if="controller" class="muted">导入期间请保持此页打开，离开将取消尚未完成的导入。</p>
    <p v-if="status.error || error || connectionError" class="error" role="alert">
      {{ error || status.error || connectionError }}
    </p>
    <details class="settings-advanced dictionary-source">
      <summary>固定来源与生效路径</summary>
      <p>
        来源：<a :href="status.source" target="_blank" rel="noopener noreferrer">{{ status.filename }}</a
        >（Apache-2.0）。只读取词典数据，不运行安装器。
      </p>
      <p>
        生效路径：<code>{{ status.path }}</code
        >。资源随数据目录保留，日志和检索缓存清理不会删除。
      </p>
      <p>
        wheel SHA-256：<code>{{ status.sha256 }}</code>
      </p>
      <p>
        system.dic SHA-256：<code>{{ status.dictionary_sha256 }}</code>
      </p>
    </details>
  </section>
</template>
