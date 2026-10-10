import { describe, expect, it } from 'vitest';
import { effortOptions, modelEffortOptions } from '../../web/lib/reasoningEffort';

describe('account reasoning capabilities', () => {
  const details = {
    id: 'model',
    display_name: 'Model',
    default_reasoning_level: 'low',
    supported_reasoning_levels: ['low', 'medium', 'high', 'xhigh', 'max'].map((effort) => ({
      effort,
      description: '',
    })),
    unavailable_reasoning_levels: [{ effort: 'ultra', reason: '当前订阅直连接口不支持此档位' }],
  };
  it('keeps xhigh explicit and selectable, and preserves official ordering', () => {
    const options = modelEffortOptions(effortOptions, details, 'xhigh');
    expect(options.map((item) => item.value)).toEqual([
      'inherit',
      'default',
      'low',
      'medium',
      'high',
      'xhigh',
      'max',
      'ultra',
    ]);
    expect(options.find((item) => item.value === 'xhigh')).toEqual({
      value: 'xhigh',
      label: '超高（xhigh）',
    });
    expect(options.find((item) => item.value === 'ultra')?.disabled).toBe(true);
    expect(options.find((item) => item.value === 'default')?.label).toContain('low');
  });
  it('shows incompatible saved settings without silently changing them', () => {
    expect(modelEffortOptions(effortOptions, details, 'none').at(-1)).toMatchObject({
      value: 'none',
      disabled: true,
    });
    expect(modelEffortOptions(effortOptions, undefined, 'xhigh')).toBe(effortOptions);
  });
});
