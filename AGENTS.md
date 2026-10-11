# Repository Agent Guidelines & Rules

## 1. Remote Repository & Pushing Policy
- **Public Remote:** `git@github.com:pbackx/obsidian_brain_operations.git` (`https://github.com/pbackx/obsidian_brain_operations`).
- **Primary Branch:** `main`.
- **Push Permission:** You are authorized to push changes to `origin/main` when confident that they are tested, robust, and completely free of secrets or personally identifiable information (PII).

## 2. Secrets & Privacy Guardrails (Strict)
Before staging or pushing any commit to the repository:
1. **No Credentials:** Never commit API keys, tokens, passwords, private keys, or credentials.
2. **No Private/LAN Data:** Never commit private IP addresses (`192.168.x.x`, `10.x.x.x`), internal domain names (`.home.arpa`), hardcoded local paths (`/home/<user>`), or private Obsidian vault IDs. Use `$HOME`, `$USER`, or environment variables.
3. **Keep Settings Private:** `config/settings.env` and `logs/` must remain strictly local and ignored by Git. Only `config/settings.env.example` may be tracked.

## 3. Language Standard
- Maintain strict **English consistency** for all documentation, scripts, commit messages, and Antigravity skill prompts.
