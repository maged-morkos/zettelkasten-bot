import os
import logging
import json
import base64
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from github import Github
import re

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config from environment variables ────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN     = os.environ["GITHUB_TOKEN"]
GITHUB_REPO      = os.environ["GITHUB_REPO"]          # e.g. maged-morkos/my-zettelkasten
ALLOWED_USER_ID  = int(os.environ["ALLOWED_USER_ID"]) # your Telegram user ID

# In-memory queue: stores raw notes until /process is called
note_queue: list[dict] = []

# ── Anthropic client ──────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── GitHub client ─────────────────────────────────────────────────────────────
gh   = Github(GITHUB_TOKEN)
repo = gh.get_repo(GITHUB_REPO)

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


def generate_note_id() -> str:
    """Timestamp-based Zettelkasten ID: YYYYMMDDHHmm"""
    return datetime.now().strftime("%Y%m%d%H%M")


def build_prompt(raw_notes: list[dict]) -> list:
    """Build the Claude message list from queued notes (text + optional images)."""
    content = []

    intro = (
        "You are a Zettelkasten assistant for an Engineering Manager.\n\n"
        "Below are raw notes captured throughout the day. Each note is separated by ---.\n"
        "For EACH distinct idea, create a properly structured Zettelkasten note in Markdown.\n\n"
        "Rules:\n"
        "1. ONE idea per note (atomic).\n"
        "2. Use this exact format for every note:\n\n"
        "---\n"
        "id: <YYYYMMDDHHmm>\n"
        "title: <Clear, concise title in English>\n"
        "type: <fleeting | literature | permanent>\n"
        "tags: [<tag1>, <tag2>, ...]\n"
        "links: [<related note title if obvious, else empty>]\n"
        "---\n\n"
        "<Body: 2-5 sentences expanding the idea clearly.>\n\n"
        "3. Choose type:\n"
        "   - fleeting  → quick thought, todo, reminder\n"
        "   - literature → insight from a meeting, article, book, or conversation\n"
        "   - permanent  → refined, evergreen engineering or leadership principle\n"
        "4. Tags should reflect EM topics: #people, #process, #technical, #strategy, #meeting, #project, etc.\n"
        "5. If you see an image, extract all meaningful content and ideas from it.\n"
        "6. Output ONLY the notes, no extra commentary.\n"
        "7. Separate each note with a blank line and a line containing only ===\n\n"
        "RAW NOTES:\n"
    )
    content.append({"type": "text", "text": intro})

    for i, note in enumerate(raw_notes):
        content.append({"type": "text", "text": f"\n--- Note {i+1} ---\n"})
        if note["type"] == "text":
            content.append({"type": "text", "text": note["content"]})
        elif note["type"] == "image":
            content.append({"type": "text", "text": "[ Image attached below ]"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": note["media_type"],
                    "data": note["content"]
                }
            })

    return content


def parse_notes(raw_output: str) -> list[dict]:
    """Split Claude's output into individual note blocks."""
    blocks = re.split(r'\n===\n', raw_output.strip())
    notes = []
    for block in blocks:
        block = block.strip()
        if block:
            notes.append(block)
    return notes


def extract_metadata(note_md: str) -> dict:
    """Pull id, title, type from the YAML frontmatter."""
    meta = {"id": generate_note_id(), "title": "untitled", "type": "fleeting"}
    id_match    = re.search(r'^id:\s*(.+)$',    note_md, re.MULTILINE)
    title_match = re.search(r'^title:\s*(.+)$', note_md, re.MULTILINE)
    type_match  = re.search(r'^type:\s*(.+)$',  note_md, re.MULTILINE)
    if id_match:    meta["id"]    = id_match.group(1).strip()
    if title_match: meta["title"] = title_match.group(1).strip()
    if type_match:  meta["type"]  = type_match.group(1).strip()
    return meta


def push_note_to_github(note_md: str, meta: dict) -> str:
    """Commit a single note .md file to GitHub."""
    safe_title = re.sub(r'[^\w\s-]', '', meta["title"]).strip().replace(" ", "-").lower()
    filename   = f"{meta['id']}-{safe_title}.md"
    folder     = meta["type"] if meta["type"] in ["fleeting", "literature", "permanent"] else "fleeting"
    path       = f"{folder}/{filename}"

    try:
        repo.create_file(
            path=path,
            message=f"add: {meta['title']}",
            content=note_md
        )
        return path
    except Exception as e:
        logger.error(f"GitHub push failed for {path}: {e}")
        raise


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "👋 Zettelkasten Bot ready!\n\n"
        "Just send me your raw thoughts anytime — text or images.\n"
        "When you're ready to process and push to Obsidian, send /process\n"
        "To see how many notes are queued, send /queue\n"
        "To clear the queue without processing, send /clear"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = update.message.text
    note_queue.append({"type": "text", "content": text})
    count = len(note_queue)
    await update.message.reply_text(f"✅ Note queued ({count} in queue). Send more or /process when ready.")


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    # Get highest resolution photo
    photo = update.message.photo[-1]
    file  = await context.bot.get_file(photo.file_id)

    # Download as bytes and base64 encode
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(file.file_path) as resp:
            image_bytes = await resp.read()

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    caption = update.message.caption or ""

    note_queue.append({
        "type": "image",
        "content": b64,
        "media_type": "image/jpeg",
        "caption": caption
    })

    count = len(note_queue)
    await update.message.reply_text(f"🖼️ Image queued ({count} in queue). Send more or /process when ready.")


async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    count = len(note_queue)
    if count == 0:
        await update.message.reply_text("📭 Queue is empty. Send some notes first!")
    else:
        await update.message.reply_text(f"📬 You have {count} note(s) waiting to be processed.")


async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    note_queue.clear()
    await update.message.reply_text("🗑️ Queue cleared.")


async def process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not note_queue:
        await update.message.reply_text("📭 Nothing in the queue. Send some notes first!")
        return

    count = len(note_queue)
    await update.message.reply_text(f"⚙️ Processing {count} note(s)... this may take a few seconds.")

    try:
        # 1. Call Claude
        content = build_prompt(note_queue)
        response = claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": content}]
        )
        raw_output = response.content[0].text

        # 2. Parse into individual notes
        notes = parse_notes(raw_output)

        if not notes:
            await update.message.reply_text("⚠️ Claude returned no structured notes. Try again.")
            return

        # 3. Push each note to GitHub
        pushed = []
        failed = []
        for note_md in notes:
            meta = extract_metadata(note_md)
            try:
                path = push_note_to_github(note_md, meta)
                pushed.append(f"📝 `{path}`")
            except Exception:
                failed.append(meta.get("title", "unknown"))

        # 4. Clear queue
        note_queue.clear()

        # 5. Report back
        summary = f"✅ Done! {len(pushed)} note(s) pushed to GitHub → Obsidian:\n\n"
        summary += "\n".join(pushed)
        if failed:
            summary += f"\n\n⚠️ Failed to push: {', '.join(failed)}"

        await update.message.reply_text(summary)

    except Exception as e:
        logger.error(f"Processing error: {e}")
        await update.message.reply_text(f"❌ Something went wrong: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("queue",   queue_status))
    app.add_handler(CommandHandler("clear",   clear_queue))
    app.add_handler(CommandHandler("process", process))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
