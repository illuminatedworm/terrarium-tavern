import { cardName, isWorldbook, type EditorCard, type EditorPersona } from './cardAuthor';

export interface LibraryReference {
  id: string;
  name: string;
  revision: number;
}
export interface CardReferenceOption {
  key: string;
  name: string;
  detail: string;
  attached?: EditorCard;
  source?: LibraryReference;
  worldbookSource?: LibraryReference;
  persona?: EditorPersona;
  personaRef?: string;
}
export interface CardMentionQuery {
  start: number;
  end: number;
  query: string;
}

/** Locate the trigger at the caret, including Chinese prose, but not email addresses. */
export function cardMentionQuery(text: string, start: number, end = start): CardMentionQuery | null {
  if (start !== end) return null;
  const match = /(^|[^\w@])@([^\s@]*)$/u.exec(text.slice(0, start));
  if (!match) return null;
  return { start: match.index + match[1].length, end: start, query: match[2] };
}

export function cardReferenceOptions(
  attached: EditorCard[],
  library: LibraryReference[],
  query: string,
  personas: EditorPersona[] = [],
  attachedPersonas: Record<string, EditorPersona> = {},
  worldbooks: LibraryReference[] = [],
): CardReferenceOption[] {
  const sources = new Set(attached.map((card) => card.card).filter(Boolean));
  const attachedBooks = new Set(attached.map((card) => card.worldbook).filter(Boolean));
  const options: CardReferenceOption[] = [
    ...attached.map((card) => ({
      key: `card:${card.ref}`,
      name: cardName(card),
      detail: isWorldbook(card) ? '世界书 · 已引用' : card.card ? '角色卡 · 已引用' : '角色卡 · 当前草稿',
      attached: card,
    })),
    ...library
      .filter((card) => !sources.has(card.id))
      .map((card) => ({
        key: `card:${card.id}`,
        name: card.name,
        detail: '角色卡',
        source: card,
      })),
    ...worldbooks
      .filter((book) => !attachedBooks.has(book.id))
      .map((book) => ({
        key: `worldbook:${book.id}`,
        name: book.name,
        detail: '世界书',
        worldbookSource: book,
      })),
    ...Object.entries(attachedPersonas).map(([ref, persona]) => ({
      key: `persona:${persona.id}`,
      name: persona.name,
      detail: '用户角色 · 已引用',
      persona,
      personaRef: ref,
    })),
    ...personas
      .filter((persona) => !Object.values(attachedPersonas).some((item) => item.id === persona.id))
      .map((persona) => ({
        key: `persona:${persona.id}`,
        name: persona.name,
        detail: '用户角色',
        persona,
      })),
  ];
  const terms = query.trim().toLocaleLowerCase().split(/\s+/);
  return options.filter((option) => {
    const text = `${option.name} ${option.detail} ${option.attached?.source.name || ''}`.toLocaleLowerCase();
    return terms.every((term) => text.includes(term));
  });
}
