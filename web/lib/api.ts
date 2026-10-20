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
