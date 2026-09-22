import { visibleLoreData } from './loreRecords';

/** Events are replayable. A subscription belongs to one view, never global state. */
export interface TurnEvent {
  seq: number;
  kind: string;
  payload: Record<string, any>;
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
    const event = visibleLoreData(JSON.parse(message.data)) as TurnEvent;
    if (accept(event)) receive(event);
  };
  stream.addEventListener('end', () => {
    stream.close();
    if (owns()) void end();
  });
  // EventSource handles transport reconnects using Last-Event-ID.
  return stream;
}
