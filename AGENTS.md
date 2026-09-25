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
- Use `gpt-5-nano` for development AI roles.
- Keep arithmetic, timestamps, duration limits, and media validation in code.
- Cache each stage; prompt changes must not redownload or retranscribe media.
- Keep raw model output separate from reviewed corrections.
- Long-form and Iteration 0 shorts come from the selected game. Whole-VOD short discovery is deferred.
- Do not build the website, queues, a vector database, or automatic publishing.
- Upgrade a model only after a reviewed Nano-vs-stronger-model comparison.

## Verify

```bash
uv run python -m unittest discover -s tests -v
uv run milloclips --help
```

For media integration, run the synthetic fixture command documented by
`uv run milloclips fixture --help`.

## Missing External Inputs

A real run requires `OPENAI_API_KEY`. The representative Kick VOD is fixed
above. Never print or commit credentials.
