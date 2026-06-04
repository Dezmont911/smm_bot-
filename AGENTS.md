# Project Agent Instructions

This is a Telegram AI content bot for automated content generation and posting.

## Main goals
- Keep the bot stable for production usage.
- Avoid large rewrites unless explicitly requested.
- Prefer small, reviewable changes.
- Preserve existing behavior unless fixing a confirmed bug.

## Safety rules
- Never commit secrets, tokens, cookies, private keys, .env files, or production credentials.
- Never deploy to production without explicit approval.
- Never run destructive database migrations without approval.
- Never push directly to main.
- Use feature branches for all changes.

## Review priorities
1. Security vulnerabilities
2. Runtime bugs
3. Data loss risks
4. Duplicate posting risks
5. Scheduler reliability
6. Cost control for LLM/image generation
7. UX and onboarding improvements

## Expected workflow
1. Analyze before editing.
2. Explain findings.
3. Ask before large refactors.
4. Make minimal patches.
5. Run tests.
6. Show diff summary.