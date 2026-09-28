import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../lib/api';
import {
  clone,
  difference,
  equal,
  getPath,
  rebaseSettings,
  scopes,
  setPath,
  type SettingsDocument,
  type SettingsObject,
  type SettingsPage,
  type SettingsScope,
} from '../lib/settingsDocument';

export function useSettingsEditor(updated: (settings: SettingsDocument) => void) {
  const saved = ref<SettingsDocument>();
  const draft = ref<SettingsDocument>();
  const baseline = ref<SettingsDocument>();
  const error = ref('');
  const busy = ref(false);
  const textBusy = ref('');
  const textDrafts = ref<Record<string, string>>({});
  const textErrors = ref<Record<string, string>>({});
  const token = ref('');
  const secretChanged = ref(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let queue: Promise<unknown> = Promise.resolve();
  let disposed = false;
  let applying = false;
  const changes = computed(
    () =>
      Object.fromEntries(
        scopes.map((scope) => [
          scope,
          saved.value && draft.value ? difference(saved.value[scope], draft.value[scope]) : {},
        ]),
      ) as Record<SettingsScope, SettingsObject>,
  );
  const pending = computed(() => scopes.some((scope) => Object.keys(changes.value[scope]).length));
  const textDirty = computed(() =>
    Object.keys(textDrafts.value).filter((key) => {
      const [scope, ...path] = key.split('.');
      return (
        saved.value && textDrafts.value[key] !== getPath(saved.value[scope as SettingsScope], path.join('.'))
      );
    }),
  );

  async function initialize() {
    try {
      const result = await api('/settings/edit-sessions', {});
      if (disposed) {
        await api(`/settings/edit-sessions/${result.id}`, undefined, 'DELETE');
        return;
      }
      token.value = result.id;
      saved.value = clone(result.settings);
      draft.value = clone(result.settings);
      baseline.value = clone(result.settings);
      updated(result.settings);
    } catch (e) {
      error.value = String(e);
    }
  }
  function enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const next = queue.then(operation, operation);
    queue = next.catch(() => undefined);
    return next;
  }
  async function commit(scope: SettingsScope, sent: SettingsObject, manual = false) {
    if (!saved.value || !draft.value || !Object.keys(sent).length) return;
    busy.value = true;
    try {
      const before = clone(saved.value);
      const response: SettingsDocument = await api(
        `/settings/${scope}`,
        {
          expected_revision: before.revisions[scope],
          changes: sent,
          edit_session: token.value,
        },
        'PATCH',
      );
      applying = true;
      const current = clone(draft.value);
      if (manual) for (const [path, value] of Object.entries(sent)) setPath(current[scope], path, value);
      draft.value = rebaseSettings(before, current, response, scope, sent);
      saved.value = clone(response);
      applying = false;
      error.value = '';
      if (scope === 'shared' && sent.providers?.some((provider: { api_key?: string }) => provider.api_key))
        secretChanged.value = true;
      updated(response);
    } catch (e) {
      error.value = String(e);
      throw e;
    } finally {
      busy.value = false;
      if (!error.value && pending.value) schedule();
    }
  }
  function flush() {
    clearTimeout(timer);
    return enqueue(async () => {
      const failures: string[] = [];
      for (const scope of scopes) {
        const sent = clone(changes.value[scope]);
        try {
          await commit(scope, sent);
        } catch (e) {
          failures.push(String(e));
        }
      }
      if (failures.length) {
        error.value = failures.join('；');
        throw Error(error.value);
      }
    });
  }
  function schedule(immediate = false) {
    if (disposed || applying) return;
    clearTimeout(timer);
    if (!pending.value) {
      error.value = '';
      return;
    }
    timer = setTimeout(
      () => {
        void flush().catch(() => undefined);
      },
      immediate ? 0 : 600,
    );
  }
  watch(draft, () => schedule(), { deep: true, flush: 'sync' });

  function textValue(scope: SettingsScope, path: string) {
    return textDrafts.value[`${scope}.${path}`] ?? getPath(saved.value?.[scope] || {}, path) ?? '';
  }
  function editText(scope: SettingsScope, path: string, value: string) {
    textDrafts.value[`${scope}.${path}`] = value;
    delete textErrors.value[`${scope}.${path}`];
  }
  async function saveText(scope: SettingsScope, path: string) {
    const key = `${scope}.${path}`;
    const value = textValue(scope, path);
    if (textBusy.value || value === getPath(saved.value?.[scope] || {}, path)) return;
    textBusy.value = key;
    delete textErrors.value[key];
    try {
      await enqueue(() => commit(scope, { [path]: value }, true));
      if (textDrafts.value[key] === value) delete textDrafts.value[key];
    } catch (e) {
      textErrors.value[key] = String(e);
    } finally {
      textBusy.value = '';
    }
  }
  function discard(page?: SettingsPage) {
    if (!draft.value || !saved.value) return;
    applying = true;
    if (page) {
      for (const path of page.paths)
        setPath(draft.value[page.scope], path, getPath(saved.value[page.scope], path));
      for (const key of Object.keys(textDrafts.value))
        if (
          page.paths.some(
            (path) => key === `${page.scope}.${path}` || key.startsWith(`${page.scope}.${path}.`),
          )
        )
          delete textDrafts.value[key];
    } else {
      draft.value = clone(saved.value);
      textDrafts.value = {};
    }
    applying = false;
    error.value = '';
  }
  function pageChanged(page: SettingsPage) {
    if (page.id === 'shared.providers' && secretChanged.value) return true;
    return (
      !!draft.value &&
      !!baseline.value &&
      (page.paths.some(
        (path) => !equal(getPath(draft.value![page.scope], path), getPath(baseline.value![page.scope], path)),
      ) ||
        textDirty.value.some((key) =>
          page.paths.some(
            (path) => key === `${page.scope}.${path}` || key.startsWith(`${page.scope}.${path}.`),
          ),
        ))
    );
  }
  async function undo(page: SettingsPage) {
    clearTimeout(timer);
    await enqueue(async () => {
      busy.value = true;
      try {
        const response: SettingsDocument = await api(`/settings/edit-sessions/${token.value}/undo`, {
          page: page.id,
          expected_revision: saved.value!.revisions[page.scope],
        });
        const sent = Object.fromEntries(
          page.paths.map((path) => [path, getPath(response[page.scope], path)]),
        );
        applying = true;
        // Preserve other pages, but deliberately discard all local changes on this page.
        for (const [path, value] of Object.entries(sent)) setPath(draft.value![page.scope], path, value);
        draft.value = rebaseSettings(saved.value!, draft.value!, response, page.scope, sent);
        saved.value = clone(response);
        discard(page);
        applying = false;
        updated(response);
        if (page.id === 'shared.providers') secretChanged.value = false;
      } catch (e) {
        error.value = String(e);
      } finally {
        busy.value = false;
      }
    });
  }
  function beforeUnload(event: BeforeUnloadEvent) {
    if (textDirty.value.length || pending.value || busy.value) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
  window.addEventListener('beforeunload', beforeUnload);
  onBeforeUnmount(() => {
    disposed = true;
    clearTimeout(timer);
    window.removeEventListener('beforeunload', beforeUnload);
    void queue.finally(() => {
      if (token.value)
        void api(`/settings/edit-sessions/${token.value}`, undefined, 'DELETE').catch(() => undefined);
    });
  });
  function acceptRefresh(response: SettingsDocument) {
    if (!saved.value || !draft.value) return;
    applying = true;
    draft.value = rebaseSettings(saved.value, draft.value, response, 'full_agent', {});
    saved.value = clone(response);
    applying = false;
    updated(response);
    if (pending.value) schedule();
  }
  return {
    saved,
    draft,
    baseline,
    error,
    busy,
    textBusy,
    textDirty,
    pending,
    initialize,
    flush,
    schedule,
    textErrors,
    textValue,
    editText,
    saveText,
    discard,
    pageChanged,
    undo,
    acceptRefresh,
  };
}
