import { afterEach, expect, it, vi } from 'vitest';
import { notification, notify } from '../../web/lib/notifications';
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  notification.value = '';
});
it('replaces the visible notification and dismisses eight seconds after the latest message', () => {
  vi.useFakeTimers();
  notify('first');
  vi.advanceTimersByTime(7000);
  notify('second');
  vi.advanceTimersByTime(1000);
  expect(notification.value).toBe('second');
  vi.advanceTimersByTime(7000);
  expect(notification.value).toBe('');
});
