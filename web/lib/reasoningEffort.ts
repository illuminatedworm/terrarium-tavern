import type { ModelDetails } from './modelCatalog';

export interface EffortOption {
  value: string;
  label: string;
  disabled?: boolean;
}
export const effortOptions: EffortOption[] = [
  { value: 'inherit', label: '跟随设置' },
  { value: 'default', label: '模型默认' },
  { value: 'none', label: '关闭思考（none）' },
  { value: 'minimal', label: '最低（minimal）' },
  { value: 'low', label: '低（low）' },
  { value: 'medium', label: '中（medium）' },
  { value: 'high', label: '高（high）' },
  { value: 'xhigh', label: '超高（xhigh）' },
  { value: 'max', label: '极高（max）' },
];

export function modelEffortOptions(
  options: EffortOption[],
  details: ModelDetails | undefined,
  selected: string,
): EffortOption[] {
  if (!details?.supported_reasoning_levels) return options;
  const label = (effort: string) => options.find((item) => item.value === effort)?.label || effort;
  const result: EffortOption[] = options
    .filter((item) => ['inherit', 'default'].includes(item.value))
    .map((item) => ({
      ...item,
      label:
        item.value === 'default' && details.default_reasoning_level
          ? `模型默认 · ${label(details.default_reasoning_level)}`
          : item.label,
    }));
  result.push(
    ...details.supported_reasoning_levels.map(({ effort }) => ({ value: effort, label: label(effort) })),
  );
  result.push(
    ...(details.unavailable_reasoning_levels || []).map(({ effort, reason }) => ({
      value: effort,
      label: `${label(effort)} · ${reason}`,
      disabled: true,
    })),
  );
  if (selected && !result.some((item) => item.value === selected)) {
    result.push({ value: selected, label: `${label(selected)} · 此模型不支持，请重选`, disabled: true });
  }
  return result;
}
