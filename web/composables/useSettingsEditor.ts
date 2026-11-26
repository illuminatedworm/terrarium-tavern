import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue';
import { rawApi } from '../lib/api';
import { refreshSettings, saveSettings } from '../lib/settingsApi';
import {
  applySettingsUpdate,
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
  type SettingsUpdate,
  withFields,
} from '../lib/settingsDocument';

export function useSettingsEditor(updated: (settings: SettingsDocument) => void) {
  const saved = shallowRef<SettingsDocument>();
  const draft = ref<SettingsDocument>();
  const baseline = shallowRef<SettingsDocument>();
  const error = ref('');
  const busy = ref(false);
  const textBusy = ref('');
  // Text and editable collections share manual-save, leave-guard, and undo behavior.
  const textDrafts = ref<SettingsObject>({});
  const textErrors = ref<Record<string, string>>({});
  const token = ref('');
  const secretChanged = ref(false);
  const webSecretChanged = ref(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let queue: Promise<unknown> = Promise.resolve();
  let blurSave: Promise<void> | undefined;
  let disposed = false;
  let applying = false;
  function scopeChanges(scope: SettingsScope) {
    return computed(() =>
      saved.value && draft.value ? difference(saved.value[scope], draft.value[scope]) : {},
    );
  }
  const changes = { story: scopeChanges('story'), shared: scopeChanges('shared') };
  const pending = computed(() => scopes.some((scope) => Object.keys(changes[scope].value).length));
  const textDirty = computed(() =>
    Object.keys(textDrafts.value).filter((key) => {
      const [scope, ...path] = key.split('.');
      return (
        saved.value &&
        !equal(textDrafts.value[key], getPath(saved.value[scope as SettingsScope], path.join('.')))
      );
    }),
  );

  async function initialize(base?: SettingsDocument) {
    try {
      const result = await rawApi('/settings/edit-sessions', base ? { known_revisions: base.revisions } : {});
      if (disposed) {
        await rawApi(`/settings/edit-sessions/${result.id}`, undefined, 'DELETE');
        return;
      }
      const previousToken = token.value;
      token.value = result.id;
      const current: SettingsDocument = base ? applySettingsUpdate(base, result.settings) : result.settings;
      saved.value = baseline.value = current;
      draft.value = { ...current, story: clone(current.story), shared: clone(current.shared) };
      secretChanged.value = webSecretChanged.value = false;
      updated(current);
      if (previousToken) await rawApi(`/settings/edit-sessions/${previousToken}`, undefined, 'DELETE');
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
      const before = saved.value;
      const response = await saveSettings(before, scope, sent, token.value);
      applying = true;
      // Manual controls keep a separate text/collection draft until Save.
      const current = manual
        ? { ...draft.value, [scope]: withFields(draft.value[scope], sent) }
        : draft.value;
      draft.value = rebaseSettings(before, current, response, scope, sent);
      saved.value = response;
      applying = false;
      error.value = '';
      if (scope === 'shared' && sent.providers?.some((provider: { api_key?: string }) => provider.api_key))
        secretChanged.value = true;
      if (scope === 'shared' && Object.keys(sent).some((path) => /^web\.(exa|tavily)\.api_key$/.test(path)))
        webSecretChanged.value = true;
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
        const sent = changes[scope].value;
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
  watch([changes.story, changes.shared], () => schedule(), { flush: 'sync' });

  function textValue(scope: SettingsScope, path: string) {
    return draftValue(scope, path) ?? '';
  }
  function draftValue(scope: SettingsScope, path: string) {
    return textDrafts.value[`${scope}.${path}`] ?? getPath(saved.value?.[scope] || {}, path);
  }
  function editText(scope: SettingsScope, path: string, value: string) {
    editValue(scope, path, value);
  }
  function editValue(scope: SettingsScope, path: string, value: unknown) {
    textDrafts.value[`${scope}.${path}`] = value;
    delete textErrors.value[`${scope}.${path}`];
  }
  async function saveText(scope: SettingsScope, path: string) {
    await saveValue(scope, path);
  }
  async function saveValue(scope: SettingsScope, path: string, replacement = draftValue(scope, path)) {
    const key = `${scope}.${path}`;
    const value = clone(replacement);
    if (textBusy.value || equal(value, getPath(saved.value?.[scope] || {}, path))) return;
    textBusy.value = key;
    try {
      await commitTextValue(scope, path, value);
    } finally {
      textBusy.value = '';
    }
  }
  async function saveOnBlur(scope: SettingsScope, path: string) {
    const value = clone(draftValue(scope, path));
    if (equal(value, getPath(saved.value?.[scope] || {}, path))) return;
    const pending = commitTextValue(scope, path, value);
    blurSave = pending;
    try {
      await pending;
    } finally {
      if (blurSave === pending) blurSave = undefined;
    }
  }
  function pendingBlurSave() {
    return blurSave;
  }
  async function commitTextValue(scope: SettingsScope, path: string, value: unknown) {
    const key = `${scope}.${path}`;
    delete textErrors.value[key];
    try {
      await enqueue(() => commit(scope, { [path]: value }, true));
      if (equal(textDrafts.value[key], value)) delete textDrafts.value[key];
    } catch (e) {
      textErrors.value[key] = String(e);
    }
  }
  function discard(page?: SettingsPage) {
    if (!draft.value || !saved.value) return;
    applying = true;
    if (page) {
      for (const path of page.paths) {
        const value = getPath(saved.value[page.scope], path);
        if (!equal(getPath(draft.value[page.scope], path), value))
          setPath(draft.value[page.scope], path, value);
      }
      for (const key of Object.keys(textDrafts.value))
        if (
          page.paths.some(
            (path) => key === `${page.scope}.${path}` || key.startsWith(`${page.scope}.${path}.`),
          )
        )
          delete textDrafts.value[key];
    } else {
      draft.value = { ...saved.value, story: clone(saved.value.story), shared: clone(saved.value.shared) };
      textDrafts.value = {};
    }
    applying = false;
    error.value = '';
  }
  function pageChanged(page: SettingsPage) {
    if (page.id === 'shared.providers' && secretChanged.value) return true;
    if (page.id === 'shared.web' && webSecretChanged.value) return true;
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
        const before = saved.value!;
        const update: SettingsUpdate = await rawApi(`/settings/edit-sessions/${token.value}/undo`, {
          page: page.id,
          expected_revision: before.revisions[page.scope],
          known_revisions: before.revisions,
        });
        const response = applySettingsUpdate(before, update);
        applying = true;
        draft.value = rebaseSettings(before, draft.value!, response, page.scope, {});
        saved.value = response;
        // An undo acknowledges the entire page; pending edits on other pages survive.
        discard(page);
        updated(response);
        if (page.id === 'shared.providers') secretChanged.value = false;
        if (page.id === 'shared.web') webSecretChanged.value = false;
        if (pending.value) schedule();
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
        void rawApi(`/settings/edit-sessions/${token.value}`, undefined, 'DELETE').catch(() => undefined);
    });
  });
  function refreshAfterProbe() {
    return enqueue(async () => {
      if (!saved.value || !draft.value) return;
      const before = saved.value;
      const response = await refreshSettings(before);
      if (disposed || response === before) return;
      applying = true;
      draft.value = rebaseSettings(before, draft.value, response, 'story', {});
      saved.value = response;
      applying = false;
      updated(response);
      if (pending.value) schedule();
    });
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
    draftValue,
    editText,
    editValue,
    saveText,
    saveValue,
    saveOnBlur,
    pendingBlurSave,
    discard,
    pageChanged,
    undo,
    refreshAfterProbe,
  };
}
