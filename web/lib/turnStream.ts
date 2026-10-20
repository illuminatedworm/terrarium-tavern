import { responseData } from './storyPresentation';

/** Events are replayable. A subscription belongs to one view, never global state. */
export interface TurnEvent {
  seq: number;
  kind: string;
  payload: Record<string, any>;
}
/** A row of the turn trace: a streamed event, or a script log added by the page. */
export type TraceEvent = Pick<TurnEvent, 'kind' | 'payload'>;

/** A model step can become tool activity or be rejected after text was previewed. */
export function turnDraft(current: string, event: TurnEvent): string {
  if (typeof event.payload.text !== 'string') return current;
  if (event.kind === 'text_reset') return event.payload.text;
  return event.kind === 'text' ? current + event.payload.text : current;
}

export function eventCursor() {
  let sequence = 0;
  return (event: TurnEvent): boolean => {
    if (!Number.isSafeInteger(event.seq) || event.seq <= sequence) return false;
    sequence = event.seq;
    return true;
  };
}

export function requestScope() {
  let version = 0;
  return {
    enter: () => ++version,
    current: () => version,
    owns: (token: number) => token === version,
  };
}

export function observeTurn(
  id: string,
  owns: () => boolean,
  receive: (event: TurnEvent) => void,
  end: () => Promise<void>,
) {
  const stream = new EventSource(`/api/turns/${id}/events`);
  const accept = eventCursor();
  stream.onmessage = (message) => {
    if (!owns()) return;
    const event = responseData(JSON.parse(message.data)) as TurnEvent;
    if (accept(event)) receive(event);
  };
  stream.addEventListener('end', () => {
    stream.close();
    if (owns()) void end();
  });
  // EventSource handles transport reconnects using Last-Event-ID.
  return stream;
}
