# Agent Guide

## Current Goal

Complete the submission requested in [Elmiillor's AI editor challenge](https://x.com/Elmiillor/status/2100601890383835200)
end to end. Eligible source content is limited to Kick VODs from September 17
through October 1, 2026. The submission requires one approximately 10-minute
AI-edited video, two AI-edited vertical shorts, and one AI-generated thumbnail;
creativity, humor, editorial judgment, quality, and productivity are the
evaluation criteria.

Use the September 23, 2026 League of Legends VOD [ELISE BUFFS](https://kick.com/elmiillor/videos/809acd6b-dd4b-4660-b0ee-746f6398ad8e)
for the current real run. Optimize for fast learning before production polish.

## Rules

- Follow YAGNI: do not build speculative features or abstractions.
- Follow KISS: prefer the simplest correct implementation that meets the current goal.
- Use `bun`, never npm/pnpm/yarn, for the website.
- Use `gpt-5-nano` for development AI roles.
- Keep arithmetic, timestamps, duration limits, and media validation in code.
- Cache each stage; prompt changes must not redownload or retranscribe media.
- Keep raw model output separate from reviewed corrections.
- Rank all detected games before choosing the long-form game. Discover and judge shorts independently across the entire VOD, including between-game footage.
- For now, shorts must be League of Legends moments with meaningful Elmiillor speech. Exclude slots/other games and silent gameplay. Allow up to 90 seconds for complete setup, outcome and spoken reaction.
- Leave about three seconds after a short's final event for streamer reaction, finishing any spoken sentence. Render captions without exclamation marks or periods; preserve question marks.
- Do not build queues, a vector database, or automatic publishing.
- Upgrade a model only after a reviewed Nano-vs-stronger-model comparison.
- Run tests without `-v`; capture noisy media logs and show summaries or relevant failures.

## Verify

```bash
uv run python -m unittest discover -s tests
uv run milloclips --help
```

The public gallery is a separate SvelteKit app in `website/`:

```bash
cd website && bun install && bun run check && bun run test
```

For media integration, run the synthetic fixture command documented by
`uv run milloclips fixture --help`.

## Missing External Inputs

A real run requires `OPENAI_API_KEY`. The representative Kick VOD is fixed
above. Never print or commit credentials.
