# Agent notes — ShadowRoom

Repo conventions and the current roadmap, for anyone (human or agent) picking this up later.

## Repo map

One MCP plugin = one repository.

These MCPs are the **tool layer**: each one is useful on its own (any MCP-capable agent can drive
it), and together they are the toolkit that `producer-tools` bundles into one app and
`shadow-producers` drives as an agent.

| Repo | What it is | Status |
| --- | --- | --- |
| `rekordbox-serato-bridge` | Rekordbox ⇄ Serato library conversion (cues, loops, colors, playlists) | published; real-machine tested |
| `audio-analysis-dedupe` | Audio analysis + duplicate detection | published; not yet user-tested |
| `set-planner` | Set timing + next-track suggestions | published; not yet user-tested |
| `shadow-music-generator` | Queue for local AI music generation pipelines | published; not yet user-tested |
| `producer-tools` (planned) | the app that collects these tools into one product | not started |
| `shadow-producers` (planned) | the AI agent | not started |

## House rules

- **License:** AGPL-3.0 everywhere (see `LICENSE`).
- **README:** user-facing only — features / requirements / install / configuration / tools / usage /
  safety / troubleshooting / license. The **intro is bilingual** (one English sentence + the same
  sentence in Chinese), and so is the GitHub repository description; the rest of `README.md` stays
  English with the full Chinese text in `README.zh-CN.md`. Implementation notes belong in
  `docs/internals.md`. See `CONTRIBUTING.md`.
- **Commits:** `feat(scope):`, `fix(scope):`, `docs(scope):`, `chore(scope):`; author
  `shadowroommusic <shadowroommusic@users.noreply.github.com>` (set per repo via
  `git config user.name` / `user.email`).
- **No guessing about vendor formats.** Only behaviour that was measured or verified on a real
  machine goes into code, docs or tests.
- **Real-machine verification** (Rekordbox, Serato, the actual hardware) is required before calling a
  conversion feature done.

## Decisions

- **2026-09-14 — generator backend (option B).** `shadow-music-generator` stays backend-less for
  now: it is used as a job record + request manager (prompts, lyrics, parameters, license notes,
  dry-run by default). The real provider wiring happens later inside `producer-tools`. The YuE2
  adapter in `src/shadow_music_generator/adapters/` is kept as ready groundwork for whenever a
  machine or API is chosen — YuE2 itself needs Linux + an NVIDIA GPU (BF16, 24 GB), so it normally
  runs remotely (local GPU box, ssh, or a cloud API).

## Roadmap

1. Publish each MCP as its own public repository. ✅ *(done)*
2. **Test and optimise every MCP and the software gradually** — plugin by plugin, on the real
   machine, revisiting behaviour, edge cases and UX as we go.
3. Build `producer-tools`: the application that collects these MCPs into one product.
4. Build `shadow-producers`: the AI agent repository.
5. Open-source both when they are ready.

## Local environment (maintainer machine)

- Working copies live in `~/Documents/ShadowRoom/<repo>`; those are the git repos that push to
  GitHub.
- GitHub: `https://github.com/shadowroommusic/*`, pushed over SSH (`~/.ssh/id_ed25519`, key titled
  "shadowroom-automation (Codex)"). 2FA is enabled on the account.
- The Codex plugin marketplace named `shadowroom` currently points at an older monorepo checkout;
  the repositories above are the source of truth.
