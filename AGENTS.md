# Agent Guide

## Current Goal

Complete Iteration 0 end to end on one real Kick VOD. Optimize for fast
learning, not production polish.

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

A real run eventually requires `OPENAI_API_KEY` and one representative Kick
VOD URL. Never print or commit credentials.
