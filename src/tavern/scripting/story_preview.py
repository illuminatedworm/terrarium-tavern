"""Provisional multi-call decoding for ordinary Full Agent, isolated from Kemini."""

import json

from tavern.core.story_segments import SEPARATOR, text_length
from tavern.scripting.kemini import ArgumentText, ToolArguments


class StoryArguments(ToolArguments):
    def __init__(self, maximum):
        super().__init__({"submit_story"})
        self.maximum = maximum
        self.parts, self.decoders = {}, {}
        self.order = []
        self.current = self.emitted_units = self.segments = 0
        self.segment_started = False
        self.breaks = []
        self.gemini_calls = 0

    def feed(self, event, api):
        if api == "gemini":
            # Gemini emits complete calls, not argument deltas. Part indices
            # restart in each SSE event, so number calls across the response.
            chunks = []
            for candidate in (event.get("candidates") or [])[:1]:
                for part in candidate.get("content", {}).get("parts", []):
                    call = part.get("functionCall")
                    if call:
                        key = self.gemini_calls
                        self.gemini_calls += 1
                        chunks.append(
                            self._arguments(
                                key,
                                call.get("name", ""),
                                json.dumps(call.get("args", {}), ensure_ascii=False),
                            )
                        )
            return "".join(chunks)
        if api == "responses" and event.get("type") == "response.output_item.done":
            decoder = self.decoders.get(event.get("item", {}).get("id"))
            if decoder is not None and (decoder.started or decoder.buffer):
                # The completed item repeats the whole JSON, not another delta.
                # Final response validation replaces any partial preview.
                return ""
        return super().feed(event, api)

    def _arguments(self, key, name, fragment):
        if name:
            self.call_names[key] = name
        name = self.call_names.get(key, "")
        if name not in self.names:
            if any(target.startswith(name) for target in self.names):
                self.pending.setdefault(key, []).append(fragment)
            else:
                self.pending.pop(key, None)
            return ""
        if key not in self.decoders:
            self.decoders[key], self.parts[key] = ArgumentText(), []
            self.order.append(key)
        decoder = self.decoders[key]
        if decoder.done:
            return ""  # Completed-item echoes must not duplicate streamed arguments.
        self.parts[key].append(decoder.feed("".join(self.pending.pop(key, [])) + fragment))
        visible = []
        # Drain only new text. Rejoining/re-encoding the growing story on every
        # token would make long multi-segment previews quadratic.
        while self.current < len(self.order):
            part_key = self.order[self.current]
            part = "".join(self.parts[part_key])
            done = self.decoders[part_key].done
            if not self.segment_started and not part.strip() and not done:
                # Blank content is rejected and takes no slot; hold leading
                # whitespace until the call shows it is a real segment.
                self.parts[part_key][:] = [part] if part else []
                break
            self.parts[part_key].clear()
            if not self.segment_started and part.strip() and self.segments < self.maximum:
                if self.emitted_units:
                    self.breaks.append(self.emitted_units)
                    visible.append(SEPARATOR)
                    self.emitted_units += len(SEPARATOR)
                self.segment_started = True
                self.segments += 1
            if self.segment_started and part:
                visible.append(part)
                self.emitted_units += text_length(part)
            if not done:
                break  # Hold interleaved later calls until the earlier prose ends.
            self.current += 1
            self.segment_started = False
        return "".join(visible)
