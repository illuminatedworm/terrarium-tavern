import { marked } from 'marked';

export function storyHtml(text: string) {
  const tokens = marked.lexer(text);
  const isInterface = (value: string) => /<(?:html|!doctype|script|style|div|section)\b/i.test(value);
  for (const token of tokens) {
    if (
      token.type === 'code' &&
      (!token.lang || token.lang.toLowerCase() === 'html') &&
      isInterface(token.text)
    )
      return token.text;
  }
  return tokens.some((token) => token.type === 'html' && isInterface(token.text)) ? text : '';
}
