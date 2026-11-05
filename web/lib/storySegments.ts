/** Joins delivered segments in raw/display text; mirrors `tavern.core.story_segments`. */
export const STORY_SEGMENT_SEPARATOR = '\n\n';

/** Boundaries precede the joining separator, measured in UTF-16 code units. */
export function splitStorySegments(text: string, breaks: readonly number[] = []): string[] {
  const parts: string[] = [];
  let start = 0;
  for (const end of breaks) {
    parts.push(text.slice(start, end));
    start = end + STORY_SEGMENT_SEPARATOR.length;
  }
  parts.push(text.slice(start));
  return parts;
}
