export const DIRECT_MESSAGE_LIMIT = 120;
export const DIRECT_TEXT_LIMIT = 200_000;

export type HistoryMessage = {
  id: string;
  parent?: string;
  role: string;
  display?: string;
  raw?: string;
  created?: number;
};
export type HistoryRound = {
  id: string;
  user?: HistoryMessage;
  reply?: HistoryMessage;
  messageIds: string[];
};

export function needsVirtualHistory(messages: HistoryMessage[]) {
  return (
    messages.length > DIRECT_MESSAGE_LIMIT ||
    messages.reduce((total, message) => total + (message.display || message.raw || '').length, 0) >
      DIRECT_TEXT_LIMIT
  );
}

export function historyMessageIntervals(messages: HistoryMessage[]) {
  const intervals = new Map<string, string>();
  for (let index = 1; index < messages.length; index++) {
    const current = messages[index].created;
    const previous = messages[index - 1].created;
    if (
      typeof current !== 'number' ||
      typeof previous !== 'number' ||
      !Number.isFinite(current) ||
      !Number.isFinite(previous)
    )
      continue;
    const elapsed = current - previous;
    if (elapsed < 0 || elapsed > 24 * 60 * 60) continue;
    const seconds = Math.floor(elapsed);
    intervals.set(messages[index].id, `+${Math.floor(seconds / 60)}分${seconds % 60}秒`);
  }
  return intervals;
}

export function historyRounds(messages: HistoryMessage[]): HistoryRound[] {
  const rounds: HistoryRound[] = [];
  for (const message of messages) {
    const previous = rounds.at(-1);
    if (message.role === 'assistant' && previous && previous.user?.id === message.parent && !previous.reply) {
      previous.reply = message;
      previous.messageIds.push(message.id);
    } else {
      rounds.push({
        id: message.id,
        messageIds: [message.id],
        ...(message.role === 'user' ? { user: message } : { reply: message }),
      });
    }
  }
  return rounds;
}

export function historySummary(message?: HistoryMessage) {
  return (message?.display || message?.raw || '')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '')
    .replace(/<[^>]*>/g, '')
    .replace(/[#*_`>]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 180);
}

export class HistoryHeights {
  private tree: number[] = [];
  private heights: number[] = [];

  reset(heights: number[]) {
    this.heights = [...heights];
    this.tree = new Array(heights.length + 1).fill(0);
    heights.forEach((height, index) => this.add(index, height));
  }

  private add(index: number, delta: number) {
    for (let cursor = index + 1; cursor < this.tree.length; cursor += cursor & -cursor)
      this.tree[cursor] += delta;
  }

  set(index: number, height: number) {
    const delta = height - this.heights[index];
    if (Math.abs(delta) < 0.5) return false;
    this.heights[index] = height;
    this.add(index, delta);
    return true;
  }

  offset(index: number) {
    let total = 0;
    for (let cursor = Math.min(index, this.heights.length); cursor > 0; cursor -= cursor & -cursor)
      total += this.tree[cursor];
    return total;
  }

  indexAt(offset: number) {
    let index = 0;
    let sum = 0;
    for (let step = 2 ** Math.floor(Math.log2(this.tree.length || 1)); step >= 1; step >>= 1) {
      const next = index + step;
      if (next < this.tree.length && sum + this.tree[next] <= offset) {
        index = next;
        sum += this.tree[next];
      }
    }
    return Math.min(index, Math.max(0, this.heights.length - 1));
  }
}
