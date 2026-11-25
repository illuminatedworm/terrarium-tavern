import { Lexer, Marked, Tokenizer } from 'marked';
import { eastAsianCharacter } from './eastAsianCharacter';

const whitespace = /\s/u;
const trailingSurrogate = /[\udc00-\udfff]/u;
const storyStarDelimiters = new RegExp(Lexer.rules.inline.gfm.emStrongRDelimAst);

storyStarDelimiters.exec = function (source) {
  const match = RegExp.prototype.exec.call(this, source);
  if (!match) return null;

  // Marked 16.3.0 uses groups 2 and 3 for one-way runs that can gain
  // the opposite role. Move them to group 5 so Marked still owns pairing.
  const stars = match[2] || match[3];
  if (!stars) return match;

  const afterPoint = source.codePointAt(this.lastIndex);
  if (afterPoint === undefined) return match;

  const before = match[0].slice(0, -stars.length);
  const after = String.fromCodePoint(afterPoint);
  if (
    !whitespace.test(before + after) &&
    (eastAsianCharacter.test(before) || eastAsianCharacter.test(after))
  ) {
    match[2] = '';
    match[3] = '';
    match[5] = stars;
  }
  return match;
};

// Keep this compatibility rule local to story prose, not the global parser.
const storyMarkdown = new Marked({
  tokenizer: {
    emStrong(source, maskedSource, previousChar = '') {
      if (source[0] !== '*') return false;

      const inline = this.rules.inline;
      if (inline.emStrongRDelimAst !== storyStarDelimiters) {
        // Each lexer owns its rules object, but the default inline table is shared.
        this.rules.inline = { ...inline, emStrongRDelimAst: storyStarDelimiters };
      }

      if (!previousChar || inline.punctuation.test(previousChar)) return false;
      const openingPunctuation = inline.emStrongLDelim.exec(source)?.[1];
      if (!openingPunctuation) return false;

      const start = maskedSource.length - source.length;
      // Marked tracks one UTF-16 unit before a potential opener.
      const before = trailingSurrogate.test(previousChar)
        ? maskedSource.slice(Math.max(0, start - 2), start)
        : previousChar;
      if (!eastAsianCharacter.test(before) && !eastAsianCharacter.test(openingPunctuation)) return false;

      return Tokenizer.prototype.emStrong.call(this, source, maskedSource, ' ');
    },
  },
});

export function parseStoryMarkdown(source: string, breaks = false): string {
  return storyMarkdown.parse(source, { async: false, gfm: true, breaks });
}
