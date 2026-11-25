// Unicode 17.0 UCD, following CommonMark proposal #839. Intersect assigned
// code points with (width W/F/H minus Emoji_Presentation) union Script=Hangul.
// Inputs: EastAsianWidth.txt, emoji/emoji-data.txt, Scripts.txt, and
// extracted/DerivedGeneralCategory.txt. See LICENSE/Unicode-3.0.txt.
export const eastAsianCharacter = new RegExp(
  '^[' +
    '\\u1100-\\u11ff\\u20a9\\u2329-\\u232a\\u2630-\\u2637\\u268a-\\u268f\\u2e80-\\u2e99' +
    '\\u2e9b-\\u2ef3\\u2f00-\\u2fd5\\u2ff0-\\u303e\\u3041-\\u3096\\u3099-\\u30ff' +
    '\\u3105-\\u312f\\u3131-\\u318e\\u3190-\\u31e5\\u31ef-\\u321e\\u3220-\\u3247' +
    '\\u3250-\\ua48c\\ua490-\\ua4c6\\ua960-\\ua97c\\uac00-\\ud7a3\\ud7b0-\\ud7c6' +
    '\\ud7cb-\\ud7fb\\uf900-\\ufa6d\\ufa70-\\ufad9\\ufe10-\\ufe19\\ufe30-\\ufe52' +
    '\\ufe54-\\ufe66\\ufe68-\\ufe6b\\uff01-\\uffbe\\uffc2-\\uffc7\\uffca-\\uffcf' +
    '\\uffd2-\\uffd7\\uffda-\\uffdc\\uffe0-\\uffe6\\uffe8-\\uffee\\u{16fe0}-\\u{16fe4}' +
    '\\u{16ff0}-\\u{16ff6}\\u{17000}-\\u{18cd5}\\u{18cff}-\\u{18d1e}' +
    '\\u{18d80}-\\u{18df2}\\u{1aff0}-\\u{1aff3}\\u{1aff5}-\\u{1affb}' +
    '\\u{1affd}-\\u{1affe}\\u{1b000}-\\u{1b122}\\u{1b132}\\u{1b150}-\\u{1b152}' +
    '\\u{1b155}\\u{1b164}-\\u{1b167}\\u{1b170}-\\u{1b2fb}\\u{1d300}-\\u{1d356}' +
    '\\u{1d360}-\\u{1d376}\\u{1f200}\\u{1f202}\\u{1f210}-\\u{1f219}' +
    '\\u{1f21b}-\\u{1f22e}\\u{1f230}-\\u{1f231}\\u{1f237}\\u{1f23b}' +
    '\\u{1f240}-\\u{1f248}\\u{1f260}-\\u{1f265}\\u{20000}-\\u{2a6df}' +
    '\\u{2a700}-\\u{2b81d}\\u{2b820}-\\u{2cead}\\u{2ceb0}-\\u{2ebe0}' +
    '\\u{2ebf0}-\\u{2ee5d}\\u{2f800}-\\u{2fa1d}\\u{30000}-\\u{3134a}' +
    '\\u{31350}-\\u{33479}' +
    ']$',
  'u',
);
