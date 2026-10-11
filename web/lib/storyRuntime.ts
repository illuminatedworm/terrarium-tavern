/** Downloads are shared; the MVU execution core is fetched only on demand. */
export function createRuntimeLoader(request: typeof fetch = fetch) {
  const pending = new Map<string, Promise<string>>();
  return (kind: 'sdk' | 'core' = 'sdk') => {
    const url = kind === 'sdk' ? '/static/runtime.js' : '/static/mvu-core.js';
    if (!pending.has(url)) {
      pending.set(
        url,
        request(url)
          .then((response) => {
            if (!response.ok) throw new Error(`角色运行时加载失败：${response.status}`);
            return response.text();
          })
          .catch((error) => {
            pending.delete(url);
            throw error;
          }),
      );
    }
    return pending.get(url)!;
  };
}

export const loadStoryRuntime = createRuntimeLoader();
