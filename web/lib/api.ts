import { responseData } from './storyPresentation';

async function failure(response: Response) {
  const error = await response.json().catch(() => ({ detail: response.statusText }));
  return Error(typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail));
}

/** Debug views need the exact persisted payload, including removed lore records. */
export async function rawApi(path: string, data?: any, method?: string, signal?: AbortSignal) {
  const response = await fetch(`/api${path}`, {
    method: method || (data === undefined ? 'GET' : 'POST'),
    headers: data instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : data instanceof FormData ? data : JSON.stringify(data),
    signal,
  });
  if (!response.ok) throw await failure(response);
  return response.json();
}

export async function api(path: string, data?: any, method?: string, signal?: AbortSignal) {
  return responseData(await rawApi(path, data, method, signal));
}

/** Send the File itself; never materialize a model-sized ArrayBuffer or multipart spool. */
export function uploadModel(
  path: string,
  file: File,
  signal: AbortSignal,
  progress: (fraction: number) => void,
) {
  return new Promise<void>((resolve, reject) => {
    const request = new XMLHttpRequest();
    const abort = () => request.abort();
    const finish = (error?: Error) => {
      signal.removeEventListener('abort', abort);
      error ? reject(error) : resolve();
    };
    request.open('POST', `/api${path}`);
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.responseType = 'json';
    request.upload.onprogress = (event) => progress(file.size ? event.loaded / file.size : 0);
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) finish();
      else {
        const detail = request.response?.detail;
        finish(Error(typeof detail === 'string' ? detail : `模型导入失败（HTTP ${request.status}）`));
      }
    };
    request.onerror = () => finish(Error('模型上传连接中断，请重试。'));
    request.onabort = () => finish(new DOMException('模型导入已取消', 'AbortError'));
    if (signal.aborted) return finish(new DOMException('模型导入已取消', 'AbortError'));
    signal.addEventListener('abort', abort, { once: true });
    request.send(file);
  });
}

/** POST returning NDJSON; each line is handed over as soon as it arrives. */
export async function streamApi(path: string, data: any, signal: AbortSignal, onLine: (line: any) => void) {
  const response = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
    signal,
  });
  if (!response.ok) throw await failure(response);
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    let end: number;
    while ((end = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      if (line) onLine(JSON.parse(line));
    }
    if (done) {
      // A final record without a trailing newline is still a complete record.
      if (buffer) onLine(JSON.parse(buffer));
      return;
    }
  }
}
