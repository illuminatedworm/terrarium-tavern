# Third-party notices

## KohakuTerrarium

- Source: https://github.com/Kohaku-Lab/KohakuTerrarium
- Pinned commit: `c01854de42eacf280d09b2dcdaeae51bab28d5ef`
- Copyright 2024–2026 Shih-Ying Yeh (KohakuBlueLeaf) and contributors.
- License: KohakuTerrarium License 1.0. The application retains Terrarium in its name and visible attribution.
- Installed as a dependency; license is included in the upstream distribution.

## MagVarUpdate

- Source: https://github.com/MagicalAstrogy/MagVarUpdate
- Pinned commit: `4a3645f19705e9e73bb4df3a8e731d5ff994ce80`
- License: MIT, full text in `vendor/mvu/LICENSE`.
- `vendor/mvu/update_variables.ts`, `schema.ts`, and `variable_def.ts` are unchanged copies from the pinned source. Build-time aliases replace ST-specific host imports with `compat/upstream-host.ts`. The application owns storage, initialization lifecycle, isolated events, and atomic commit.

## Tavern Helper / JS-Slash-Runner

- Interface reference: https://github.com/n0vi028/JS-Slash-Runner
- Reference commit: `04a62b763513744b8cb3d85322f9b5c73bdf8a85`
- Its implementation is not included or redistributed. `compat/runtime.ts` is an independently written compatibility adapter; reference APIs do not imply full compatibility.

## Character card specifications

- V2: https://github.com/malfoyslastname/character-card-spec-v2
- V3: https://github.com/kwaroran/character-card-spec-v3

## Browser libraries

Vue, lodash, jQuery, Zod, YAML, JSON5, fast-json-patch, Acorn, DOMPurify, marked, mathjs and klona are installed through npm. Their package license files and preserved bundler notices apply. Exact versions are in `package-lock.json`.

The two sample character cards are original fixtures created for this project and may be used, modified and redistributed with attribution to Terrarium Tavern.

## Token counting

`tiktoken` (MIT, https://github.com/openai/tiktoken) supplies the BPE implementation and the o200k_base/cl100k_base vocabularies. Verified vocabularies are bundled in src/tavern/tokenizer_data for offline counting. They provide reference estimates for models with different tokenizers.
