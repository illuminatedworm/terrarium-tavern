/** Share immutable SDK sources across message frames; each frame remains isolated. */
export function createRuntimeLoader(request: typeof fetch = fetch) {
  let pending: Promise<string[]> | undefined;
  return () => {
    if (!pending) {
      pending = Promise.all(
        ['/static/runtime.js', '/static/mvu-core.js'].map(async (url) => {
          const response = await request(url);
          if (!response.ok) throw new Error(`角色运行时加载失败：${response.status}`);
          return response.text();
        }),
      ).catch((error) => {
        pending = undefined;
        throw error;
      });
    }
    return pending;
  };
}

export const loadStoryRuntime = createRuntimeLoader();
