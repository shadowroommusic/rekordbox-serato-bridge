# Contributing

Thanks for taking a look! These are the house rules for this repository (and the sibling
ShadowRoom plugin repositories).

## README style

- `README.md` is written for users: what it is, features, requirements, install, configuration,
  tools, usage, safety, troubleshooting, license. Nothing else.
- The **opening lines are bilingual**: one English sentence describing the plugin, then the same
  sentence in Chinese. The GitHub repository description follows the same pattern
  (`中文一句话 · English sentence`).
- The rest of `README.md` is English; the complete Chinese version lives in `README.zh-CN.md`.
- **No implementation details in the README** — no reverse-engineered formats, byte layouts,
  measured tables or "how we figured it out" stories. Those go to `docs/internals.md`.
- Keep `README.zh-CN.md` as a Chinese translation with the same structure and headings.
- Prefer tables for options/tools/formats and keep every code block copy-pasteable.

## Commits

- Prefix with a scope, e.g. `feat(bridge): …`, `fix(bridge): …`, `docs(bridge): …`,
  `chore(repo): …`.
- One logical change per commit; mention the affected direction (Rekordbox → Serato / Serato →
  Rekordbox) or component when it helps.

## Tests

```sh
PYTHONPATH=src python3 tests/test_bridge_unittest.py
```

Please keep the suite green and add a test for every new field or format rule.

## Licensing

This project is released under AGPL-3.0. By contributing you agree that your work is released
under the same license.
