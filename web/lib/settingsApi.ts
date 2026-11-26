import { rawApi } from './api';
import {
  applySettingsUpdate,
  type SettingsDocument,
  type SettingsObject,
  type SettingsScope,
  type SettingsUpdate,
} from './settingsDocument';

export function settingsQuery(settings: SettingsDocument): string {
  return new URLSearchParams({
    story_revision: String(settings.revisions.story),
    shared_revision: String(settings.revisions.shared),
  }).toString();
}

export async function refreshSettings(base?: SettingsDocument | null): Promise<SettingsDocument> {
  if (!base) return rawApi('/settings');
  const update: SettingsUpdate = await rawApi(`/settings?${settingsQuery(base)}`);
  return applySettingsUpdate(base, update);
}

export async function saveSettings(
  base: SettingsDocument,
  scope: SettingsScope,
  changes: SettingsObject,
  editSession?: string,
): Promise<SettingsDocument> {
  const update: SettingsUpdate = await rawApi(
    `/settings/${scope}`,
    {
      expected_revision: base.revisions[scope],
      known_revisions: base.revisions,
      changes,
      edit_session: editSession,
    },
    'PATCH',
  );
  return applySettingsUpdate(base, update);
}
