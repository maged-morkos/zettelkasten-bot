# Zettelkasten Telegram Bot

A personal AI-powered note-taking bot that structures your raw thoughts into Zettelkasten notes and pushes them directly to your Obsidian vault via GitHub.

## How it works

1. Send text or images to your Telegram bot throughout the day
2. Send `/process` when ready — Claude structures everything into atomic Zettelkasten notes
3. Notes are automatically pushed to GitHub and appear in Obsidian

## Bot Commands

| Command | What it does |
|---|---|
| `/start` | Show welcome message |
| `/queue` | See how many notes are waiting |
| `/process` | Process all queued notes and push to GitHub |
| `/clear` | Clear the queue without processing |

## Environment Variables (set these in Railway)

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Your bot token from BotFather |
| `ANTHROPIC_API_KEY` | Your Claude API key |
| `GITHUB_TOKEN` | Your GitHub Personal Access Token |
| `GITHUB_REPO` | Format: `maged-morkos/my-zettelkasten` |
| `ALLOWED_USER_ID` | Your Telegram numeric user ID |

## Deployment

1. Push this repo to GitHub
2. Connect to Railway
3. Set all environment variables
4. Deploy as a Worker (not a web service)
