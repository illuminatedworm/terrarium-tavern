import { ref } from 'vue';
export const notification = ref('');
let timer: ReturnType<typeof setTimeout> | undefined;
export function notify(message: string) {
  if (!message) return;
  clearTimeout(timer);
  notification.value = message;
  timer = setTimeout(() => {
    notification.value = '';
  }, 8000);
}
