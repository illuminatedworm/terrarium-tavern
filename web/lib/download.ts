import { notify } from './notifications';

type NativeReply = { id: string; ok: boolean; value?: unknown; error?: string };
type NativeExport = {
  postMessage(message: string): void;
  onmessage: ((event: MessageEvent<string>) => void) | null;
};

declare global {
  interface Window {
    TavernAndroid?: NativeExport;
  }
}

let sequence = 0;
const pending = new Map<string, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();

function nativeRequest(action: string, fields: Record<string, unknown> = {}) {
  const bridge = window.TavernAndroid!;
  bridge.onmessage = ({ data }) => {
    const reply = JSON.parse(data) as NativeReply;
    const request = pending.get(reply.id);
    if (!request) return;
    pending.delete(reply.id);
    if (reply.ok) request.resolve(reply.value);
    else request.reject(Error(reply.error || '文件保存失败'));
  };
  return new Promise<unknown>((resolve, reject) => {
    const id = String(++sequence);
    pending.set(id, { resolve, reject });
    bridge.postMessage(JSON.stringify({ id, action, ...fields }));
  });
}

/** Returns false only when the user cancels Android's system save dialog. */
export async function saveDownload(blob: Blob, name: string): Promise<boolean> {
  if (window.TavernAndroid) {
    const transfer = await nativeRequest('begin', { name, mime: blob.type, size: blob.size });
    try {
      // Bound bridge messages and avoid duplicating a complete backup as a base64 string.
      for (let offset = 0; offset < blob.size; offset += 65536) {
        const bytes = new Uint8Array(await blob.slice(offset, offset + 65536).arrayBuffer());
        let binary = '';
        for (const byte of bytes) binary += String.fromCharCode(byte);
        await nativeRequest('chunk', { transfer, data: btoa(binary) });
      }
      return Boolean(await nativeRequest('finish', { transfer }));
    } catch (error) {
      await nativeRequest('abort', { transfer }).catch(() => undefined);
      throw error;
    }
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return true;
}

/** Preserve ordinary desktop links; Android attachments use the same system-save bridge. */
export async function downloadLink(event: MouseEvent) {
  if (!window.TavernAndroid) return;
  event.preventDefault();
  const link = event.currentTarget as HTMLAnchorElement;
  try {
    const response = await fetch(link.href);
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      throw Error(typeof error.detail === 'string' ? error.detail : '导出失败');
    }
    const disposition = response.headers.get('Content-Disposition') || '';
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const name = encoded
      ? decodeURIComponent(encoded)
      : disposition.match(/filename="([^"]+)"/)?.[1] || link.download || 'tavern-export';
    if (await saveDownload(await response.blob(), name)) notify('文件已保存。');
  } catch (error) {
    notify(String(error).replace(/^Error: /, ''));
  }
}
