import { api } from './api';
import { requestScope } from './turnStream';

export class StaleView extends Error {}

/** A completed mutation belongs to the view that submitted it, even after navigation. */
export function createSessionController(request: typeof api = api) {
  const scope = requestScope();
  return {
    ...scope,
    async request(...args: Parameters<typeof api>) {
      const token = scope.current();
      const result = await request(...args);
      if (!scope.owns(token)) throw new StaleView('已切换对话，操作结果保留在原对话。');
      return result;
    },
  };
}
