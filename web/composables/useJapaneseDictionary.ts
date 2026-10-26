import { computed, onBeforeUnmount, onMounted, ref, type InjectionKey } from 'vue';
import { api } from '../lib/api';
import { notify } from '../lib/notifications';

export interface JapaneseDictionaryStatus {
  platform: 'desktop' | 'android';
  state:
    | 'missing'
    | 'unchecked'
    | 'checking'
    | 'downloading'
    | 'importing'
    | 'verifying'
    | 'ready'
    | 'error'
    | 'cancelled'
    | 'bundled';
  version: string;
  source: string;
  filename: string;
  sha256: string;
  dictionary_sha256: string;
  download_bytes: number;
  dictionary_bytes: number;
  path: string;
  completed_bytes: number;
  total_bytes: number;
  error: string;
  proxy_enabled: boolean;
}

const labels: Record<JapaneseDictionaryStatus['state'], string> = {
  missing: '尚未准备',
  unchecked: '已在本地，首次使用时校验',
  checking: '正在检查本地词典',
  downloading: '正在下载',
  importing: '正在导入',
  verifying: '正在解包与校验',
  ready: '已就绪',
  error: '准备失败',
  cancelled: '已取消',
  bundled: 'APK 内置',
};

export function useJapaneseDictionary() {
  const status = ref<JapaneseDictionaryStatus>();
  const connectionError = ref('');
  const busy = computed(
    () =>
      !!status.value && ['checking', 'downloading', 'importing', 'verifying'].includes(status.value.state),
  );
  const label = computed(() => (status.value ? labels[status.value.state] : '正在读取状态'));
  const progress = computed(() =>
    status.value?.total_bytes ? status.value.completed_bytes / status.value.total_bytes : undefined,
  );
  let events: EventSource | undefined;
  onMounted(() => {
    events = new EventSource('/api/dictionaries/japanese/events');
    events.onmessage = (event) => {
      const next = JSON.parse(event.data) as JapaneseDictionaryStatus;
      if (status.value && next.state === 'ready' && status.value.state !== 'ready')
        notify('日文词典已就绪，可继续日文检索。');
      status.value = next;
      connectionError.value = '';
    };
    events.onerror = () => {
      connectionError.value = '词典状态连接中断，正在重连。';
    };
  });
  onBeforeUnmount(() => events?.close());
  async function action(name: 'download' | 'cancel') {
    status.value = await api(`/dictionaries/japanese/${name}`, {});
  }
  return { status, busy, label, progress, connectionError, action };
}

export type JapaneseDictionaryState = ReturnType<typeof useJapaneseDictionary>;
export const japaneseDictionaryKey: InjectionKey<JapaneseDictionaryState> = Symbol('japanese-dictionary');
