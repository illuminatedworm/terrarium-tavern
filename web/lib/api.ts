import { responseData } from './storyPresentation';

/** Debug views need the exact persisted payload, including removed lore records. */
export async function rawApi(path: string, data?: any, method?: string, signal?: AbortSignal) {
  const response = await fetch(`/api${path}`, {
    method: method || (data === undefined ? 'GET' : 'POST'),
    headers: data instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : data instanceof FormData ? data : JSON.stringify(data),
    signal,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw Error(typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail));
  }
  return response.json();
}

export async function api(path: string, data?: any, method?: string, signal?: AbortSignal) {
  return responseData(await rawApi(path, data, method, signal));
}
