import { Marked, Tokenizer } from 'marked';

// Keep this compatibility rule local to story prose, not the global parser.
const storyMarkdown = new Marked({
  tokenizer: {
    emStrong(source, maskedSource, previousChar = '') {
      const standard = Tokenizer.prototype.emStrong.call(this, source, maskedSource, previousChar);
      if (standard || !/^\p{Script=Han}$/u.test(previousChar) || !/^\*\*[‘“「『]/u.test(source)) {
        return standard;
      }
      // Chinese prose omits spaces before quoted names. Relax only this opening
      // boundary; keep Marked's closing, nesting, escape and code-masking rules.
      const quotedStrong = Tokenizer.prototype.emStrong.call(this, source, maskedSource, ' ');
      return quotedStrong?.type === 'strong' ? quotedStrong : undefined;
    },
  },
});

export function parseStoryMarkdown(source: string, breaks = false): string {
  return storyMarkdown.parse(source, { async: false, gfm: true, breaks });
}
