# Zettelkasten Telegram Bot v3

AI-powered note-taking bot for Engineering Managers. Send raw thoughts via Telegram → Claude asks clarifying questions if needed → structures notes → pushes to Obsidian automatically.

## How It Works

1. Send a note (text or image) to your bot
2. If the note is ambiguous, the bot asks you one clarifying question
3. Reply using Telegram's **Reply feature** to enrich the note
4. Send `/process` when ready — Claude structures everything and pushes to GitHub
5. Notes appear in Obsidian within 5 minutes

## Vault Structure

```
my-zettelkasten/
├── fleeting/      💭 Quick thoughts, reminders
├── literature/    📚 Insights from articles, books, conversations
├── permanent/     🏛️ Evergreen principles and frameworks
├── tasks/         ✅ Action items with due dates
├── people/        👤 Notes about team members
├── meetings/      🤝 Meeting notes and outcomes
├── projects/      🗂️ Project ideas and decisions
└── personal/      🏠 Personal notes (separate from work)
```

## Commands

| Command | What it does |
|---|---|
| `/start` | Show welcome message |
| `/personal` | Switch to Personal mode (notes → personal/) |
| `/work` | Switch back to Work mode (default) |
| `/mode` | See current mode |
| `/queue` | See how many notes are waiting + clarification status |
| `/process` | Process all queued notes and push to GitHub |
| `/clear` | Clear the queue and all pending questions |

## Clarifying Questions

When the bot asks a question, use Telegram's native **Reply** feature (long press the bot's message → Reply) to answer. Your answer gets attached to the original note as extra context before processing.

If you send `/process` while questions are still unanswered, the bot will warn you but process anyway with what it has.

## Note Format Examples

**Regular note:**
```markdown
---
id: 202502201430
title: Async Standups Reduce Meeting Fatigue
type: permanent
tags: [#process, #meetings, #team]
links: [Ahmed Standup Issue]
---
Body of the note...
```

**Task:**
```markdown
---
id: 202502201435
title: Follow up with Sarah on Q2 Roadmap
type: tasks
status: open
due: 2026-02-25
tags: [#delivery, #strategy]
links: []
---
Body of the note...
```

**Meeting:**
```markdown
---
id: 202502201500
title: Sprint Planning Feb 20
type: meetings
attendees: [Ahmed, Sara, Khalid]
date: 2026-02-20
tags: [#meeting, #sprint]
links: []
---
Body of the note...
```

## Environment Variables (set in Railway)

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Your bot token from BotFather |
| `ANTHROPIC_API_KEY` | Your Claude API key |
| `GITHUB_TOKEN` | Your GitHub Personal Access Token |
| `GITHUB_REPO` | `maged-morkos/my-zettelkasten` |
| `ALLOWED_USER_ID` | Your Telegram numeric user ID |
