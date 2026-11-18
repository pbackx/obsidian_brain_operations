---
type: task
status: backlog
category: general
priority: normal
created: "{{date}}"
updated: "{{date}}"
---

# {{title}}

> [!tip] Optional Frontmatter Properties
> - **`runner`:** `agy` *(default, quota-aware Antigravity)* | `pi` *(Pi coding agent)*
> - **`model`:** OpenRouter model ID for `runner: pi` *(e.g. `anthropic/claude-sonnet-4.5`, `google/gemini-2.5-pro`, `deepseek/deepseek-chat`; defaults to `PI_MODEL`)*
> - **`provider`:** Restrict OpenRouter upstream provider(s) for `runner: pi` *(e.g. `nebius` or `nebius,mistral` for European hosting; enforces `allow_fallbacks: false`)*
> - **`timeout`:** Per-task time budget *(default `30m`, max `2h`; accepts `45m`, `1h30m`, `1800s`, or plain numbers in minutes like `45`)*

## Notes & Remarks
- 
