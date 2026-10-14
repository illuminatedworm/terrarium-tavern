"""A provisional view of one model response, never a story or tool checkpoint."""


class TurnPreview:
    def __init__(self, store, tid):
        self.store, self.tid, self.text = store, tid, ""

    def append(self, text: str) -> None:
        if text:
            self.store.write_turn_draft(self.tid, text)
            self.text += text

    def replace(self, text: str) -> None:
        if not self.text and text:
            self.append(text)
            return
        if text != self.text:
            self.store.write_turn_draft(self.tid, text, replace=True)
            self.text = text
