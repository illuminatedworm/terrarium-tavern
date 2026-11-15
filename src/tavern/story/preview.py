"""A provisional story view; later model responses do not reset accepted prose."""

from tavern.core.story_segments import SEPARATOR, text_length


class TurnPreview:
    def __init__(self, store, tid):
        self.store, self.tid, self.text = store, tid, ""
        self.breaks = []
        self.segment_started = False

    def begin_segment(self):
        self.segment_started = False
        self.base_length = text_length(self.text)
        self.base_breaks = self.breaks.copy()

    def append_segment(self, text, *, breaks=()):
        if not text:
            return
        if not self.segment_started:
            self.segment_started = True
            if self.base_length:
                text = SEPARATOR + text
        offset = self.base_length + len(SEPARATOR) if self.base_length else 0
        boundaries = [
            *self.base_breaks,
            *([self.base_length] if self.base_length else []),
            *(offset + boundary for boundary in breaks),
        ]
        self.store.write_turn_draft(self.tid, text, breaks=boundaries if boundaries != self.breaks else None)
        self.text += text
        self.breaks = boundaries

    def append(self, text: str) -> None:
        if text:
            self.store.write_turn_draft(self.tid, text)
            self.text += text

    def replace(self, text: str, *, breaks=()) -> None:
        breaks = list(breaks)
        if not self.text and text and not breaks and not self.breaks:
            self.append(text)
            return
        if text != self.text or breaks != self.breaks:
            self.store.write_turn_draft(self.tid, text, replace=True, breaks=breaks)
            self.text = text
            self.breaks = breaks
