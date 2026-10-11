/** Runs inside the opaque-origin frame. Parent messages are authenticated twice. */
export function mountFrame(runtime: any, options: any) {
  const pending = new Map<string, any>();
  const token = options.token;
  const send = (method: string, args?: any, extra?: any) =>
    parent.postMessage({ token, method, args, ...extra }, '*');
  const host = (method: string, args: any) =>
    new Promise((resolve, reject) => {
      const id = crypto.randomUUID();
      pending.set(id, { resolve, reject });
      parent.postMessage({ token, id, method, args }, '*');
    });
  const load = (url: string) =>
    new Promise<void>((resolve, reject) => {
      const script = document.createElement('script');
      script.src = url;
      script.onload = () => {
        script.remove();
        resolve();
      };
      script.onerror = () => {
        script.remove();
        reject(Error('面板资源加载失败'));
      };
      document.head.append(script);
    });
  const loadBlob = async (blob: Blob) => {
    const url = URL.createObjectURL(blob);
    try {
      await load(url);
    } finally {
      URL.revokeObjectURL(url);
    }
  };
  const takeHistory = () => {
    const history = (window as any).__tavernHistory;
    delete (window as any).__tavernHistory;
    return history;
  };
  runtime.install(options.data, host, {
    history: takeHistory(),
    loadCore: async () => loadBlob((await host('runtime-core', {})) as Blob),
  });
  let updates = Promise.resolve();
  addEventListener('message', (event) => {
    if (event.source !== parent || event.data?.token !== token) return;
    const data = event.data;
    if (data.method === 'state') {
      updates = updates
        .then(async () => {
          await loadBlob(data.history);
          await runtime.refresh(data.data, takeHistory());
          send('updated', data.version);
        })
        .catch((error) => send('state-error', String(error)));
      return;
    }
    if (data.method === 'suspend') {
      updates = updates
        .then(async () => {
          const panel = (window as any).TavernPanel;
          if (pending.size || typeof panel?.save !== 'function' || typeof panel?.restore !== 'function')
            return;
          await runtime.flush();
          const saved = await panel.save();
          if (!pending.size) send('saved', saved ?? null, { version: data.version });
        })
        .catch((error) => send('log', { message: String(error) }));
      return;
    }
    const callback = pending.get(data.id);
    if (!callback) return;
    if (data.stream !== undefined) {
      callback.full = (callback.full || '') + data.stream;
      void runtime.emit('stream_token_received_incrementally', data.stream);
      void runtime.emit('stream_token_received_fully', callback.full);
      return;
    }
    pending.delete(data.id);
    if (data.error) callback.reject(Error(data.error));
    else callback.resolve(data.result);
  });
  if (options.findInDialogue)
    addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault();
        send('find');
      }
    });
  addEventListener('DOMContentLoaded', async () => {
    if (!document.querySelector('#send_textarea')) {
      const proxy = document.createElement('textarea');
      proxy.id = 'send_textarea';
      proxy.hidden = true;
      proxy.addEventListener('input', () => void host('input', { text: proxy.value }));
      document.body.append(proxy);
    }
    if (options.expandFirstLevel) {
      const initialized = new WeakSet();
      const expand = () =>
        document.querySelectorAll<HTMLElement>('details, .monitor-container').forEach((el) => {
          if (initialized.has(el)) return;
          initialized.add(el);
          if (!el.parentElement?.closest('details, .monitor-container')) {
            if (el instanceof HTMLDetailsElement) el.open = true;
            else el.classList.add('expanded');
          }
        });
      expand();
      new MutationObserver(expand).observe(document.body, { childList: true, subtree: true });
    }
    try {
      // jQuery-ready and synchronous card initialization run before restoring UI state.
      await new Promise<void>((resolve) => (window as any).$(() => resolve()));
      if (options.hasSaved) {
        const panel = (window as any).TavernPanel;
        if (typeof panel?.restore !== 'function') throw Error('面板未提供恢复方法');
        await panel.restore(options.saved);
      }
      send('ready');
    } catch (error) {
      send('restore-error', String(error));
    }
  });
  addEventListener('load', () =>
    new ResizeObserver(() => send('height', document.body.scrollHeight)).observe(document.body),
  );
  addEventListener('error', (event) => {
    void host('log', { message: event.message });
  });
}
