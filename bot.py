import os
import logging
import base64
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from github import Github
import aiohttp

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN       = os.environ["GITHUB_TOKEN"]
GITHUB_REPO        = os.environ["GITHUB_REPO"]
ALLOWED_USER_ID    = int(os.environ["ALLOWED_USER_ID"])

# ── State ─────────────────────────────────────────────────────────────────────
note_queue: list[dict] = []
personal_mode: bool = False

# Maps bot question message_id → index in note_queue
# So when you reply to a question, we know which note to enrich
pending_questions: dict[int, int] = {}

# ── Clients ───────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gh     = Github(GITHUB_TOKEN)
repo   = gh.get_repo(GITHUB_REPO)

# ── Valid folders ─────────────────────────────────────────────────────────────
WORK_FOLDERS     = {"fleeting", "literature", "permanent", "tasks", "people", "meetings", "projects"}
PERSONAL_FOLDERS = {"personal"}
ALL_FOLDERS      = WORK_FOLDERS | PERSONAL_FOLDERS


# ─────────────────────────────────────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────────────────────────────────────

CLARIFICATION_PROMPT = """You are a Zettelkasten assistant reviewing a raw note from an Engineering Manager.

Your job: decide if asking ONE clarifying question would make this note significantly richer and more useful.

Ask a question if the note:
- Mentions a person, topic, or thing without enough context to be useful later
- Contains an action item without a clear owner, deadline, or next step
- Is vague enough that future-you might not understand it
- Has a decision or insight that would benefit from knowing the "why"

Do NOT ask a question if:
- The note is already clear and self-contained
- It's a simple reminder or quick thought that doesn't need more detail
- Asking would feel annoying or unnecessary

RESPOND IN EXACTLY ONE OF THESE TWO FORMATS:

If no question needed:
CLEAR

If a question would help:
QUESTION: <your single, specific, conversational question>

Raw note:
"""

WORK_PROMPT = """You are a Zettelkasten assistant for an Engineering Manager.

Below are raw notes captured throughout the day. Each note may include extra context added after clarification.
For EACH distinct idea or action, create a properly structured Zettelkasten note in Markdown.

FOLDER TYPES — choose the best fit:
- fleeting    → quick thought, reminder, something to revisit later
- literature  → insight from an article, book, podcast, or conversation
- permanent   → refined, evergreen engineering or leadership principle
- tasks       → a concrete action item that needs to be done
- people      → information, observations, or context about a specific person
- meetings    → notes or outcomes from a meeting
- projects    → ideas, status, or decisions related to a specific project

REQUIRED FORMAT for every note:

---
id: <YYYYMMDDHHmm>
title: <Clear concise title in English>
type: <folder type from above>
tags: [<tag1>, <tag2>]
links: [<related note title if obvious, else leave empty>]
---

<Body: 2-5 sentences expanding the idea clearly.>

EXTRA FIELDS by type (add these inside the frontmatter):
- tasks    → add:  status: open   and   due: <YYYY-MM-DD if mentioned, else TBD>
- people   → add:  person: <full name>
- meetings → add:  attendees: [<name1>, <name2>]   and   date: <YYYY-MM-DD>
- projects → add:  project: <project name>

TAGS to use (pick what fits):
#people #process #technical #strategy #meeting #project #decision #risk #feedback #growth #hiring #delivery

RULES:
1. One idea per note — atomic.
2. If a raw note contains multiple ideas, split into multiple notes.
3. If you see an image, extract ALL meaningful content from it.
4. Use any clarification context provided to make the note richer.
5. Output ONLY the notes, no commentary or explanation.
6. Separate each note with a line containing only ===

RAW NOTES:
"""

PERSONAL_PROMPT = """You are a Zettelkasten assistant helping organize personal notes and thoughts.

Below are raw personal notes. Each note may include extra context added after clarification.
For EACH distinct idea, create a structured Zettelkasten note.

All notes go into the "personal" folder.

REQUIRED FORMAT:

---
id: <YYYYMMDDHHmm>
title: <Clear concise title in English>
type: personal
tags: [<tag1>, <tag2>]
links: [<related note title if obvious, else leave empty>]
---

<Body: 2-5 sentences expanding the idea clearly.>

EXTRA FIELDS:
- If it is a task → add:  status: open   and   due: <YYYY-MM-DD if mentioned, else TBD>
- If it relates to a person → add:  person: <name>

TAGS to use (pick what fits):
#health #family #finance #learning #goals #ideas #travel #reflection #reading #habits

RULES:
1. One idea per note — atomic.
2. Split multiple ideas into multiple notes.
3. If you see an image, extract all meaningful content.
4. Use any clarification context provided to make the note richer.
5. Output ONLY the notes, no commentary.
6. Separate each note with a line containing only ===

RAW NOTES:
"""


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


def generate_note_id() -> str:
    return datetime.now().strftime("%Y%m%d%H%M")


def mode_label(is_personal: bool) -> str:
    return "🏠 Personal" if is_personal else "💼 Work"


async def check_if_needs_clarification(note_content: str) -> tuple[bool, str]:
    """
    Ask Claude if this note needs clarification.
    Returns (needs_question: bool, question_text: str)
    """
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": CLARIFICATION_PROMPT + note_content
        }]
    )
    reply = response.content[0].text.strip()

    if reply.startswith("QUESTION:"):
        question = reply[len("QUESTION:"):].strip()
        return True, question
    return False, ""


def build_claude_messages(queue: list[dict], is_personal: bool) -> list:
    """Build the message payload for Claude including text, images, and clarifications."""
    prompt = PERSONAL_PROMPT if is_personal else WORK_PROMPT
    content = [{"type": "text", "text": prompt}]

    for i, note in enumerate(queue):
        content.append({"type": "text", "text": f"\n--- Note {i+1} ---\n"})

        if note["type"] == "text":
            content.append({"type": "text", "text": note["content"]})

        elif note["type"] == "image":
            content.append({"type": "text", "text": "[Image attached]"})
            if note.get("caption"):
                content.append({"type": "text", "text": f"Caption: {note['caption']}"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": note["media_type"],
                    "data": note["content"]
                }
            })

        # Append clarification if it exists
        if note.get("clarification"):
            content.append({
                "type": "text",
                "text": f"\n[Clarification provided]: {note['clarification']}"
            })

    return content


def parse_notes(raw_output: str) -> list[str]:
    blocks = re.split(r'\n===\n|^===\n', raw_output.strip(), flags=re.MULTILINE)
    return [b.strip() for b in blocks if b.strip()]


def extract_metadata(note_md: str) -> dict:
    meta = {"id": generate_note_id(), "title": "untitled", "type": "fleeting"}
    for field in ["id", "title", "type"]:
        match = re.search(rf'^{field}:\s*(.+)$', note_md, re.MULTILINE)
        if match:
            meta[field] = match.group(1).strip()
    if meta["type"] not in ALL_FOLDERS:
        meta["type"] = "fleeting"
    return meta


def push_note_to_github(note_md: str, meta: dict) -> str:
    safe_title = re.sub(r'[^\w\s-]', '', meta["title"]).strip().replace(" ", "-").lower()
    safe_title = safe_title[:60]
    filename   = f"{meta['id']}-{safe_title}.md"
    folder     = meta["type"]
    path       = f"{folder}/{filename}"
    repo.create_file(
        path=path,
        message=f"add: {meta['title']}",
        content=note_md
    )
    return path


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "👋 *Zettelkasten Bot ready!*\n\n"
        "Send me your thoughts anytime — text or images.\n"
        "If I need more context, I'll ask you a question.\n"
        "Just use Telegram's *Reply* feature to answer me.\n\n"
        "*Commands:*\n"
        "/process — structure & push all queued notes\n"
        "/queue — see how many notes are waiting\n"
        "/clear — clear the queue\n"
        "/personal — switch to 🏠 Personal mode\n"
        "/work — switch to 💼 Work mode (default)\n"
        "/mode — see current mode\n\n"
        f"Current mode: {mode_label(personal_mode)}",
        parse_mode="Markdown"
    )


async def switch_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    global personal_mode
    personal_mode = True
    await update.message.reply_text(
        "🏠 Switched to *Personal mode*.\nNotes will go into `personal/`.\nSend /work to switch back.",
        parse_mode="Markdown"
    )


async def switch_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    global personal_mode
    personal_mode = False
    await update.message.reply_text(
        "💼 Switched to *Work mode*.",
        parse_mode="Markdown"
    )


async def current_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(f"Current mode: {mode_label(personal_mode)}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    msg = update.message

    # ── Case 1: This is a reply to one of the bot's clarifying questions ──────
    if msg.reply_to_message and msg.reply_to_message.message_id in pending_questions:
        queue_index = pending_questions.pop(msg.reply_to_message.message_id)

        if queue_index < len(note_queue):
            note_queue[queue_index]["clarification"] = msg.text
            await msg.reply_text(
                "✅ Got it! Context added to the note.\n"
                "Send more notes or /process when ready."
            )
        else:
            await msg.reply_text("⚠️ Couldn't find the original note. Please re-send it.")
        return

    # ── Case 2: This is a new note ────────────────────────────────────────────
    raw_text = msg.text

    # Add to queue first so we have the index
    note_queue.append({"type": "text", "content": raw_text, "clarification": None})
    queue_index = len(note_queue) - 1

    # Quick clarification check
    try:
        needs_question, question = await check_if_needs_clarification(raw_text)
    except Exception as e:
        logger.error(f"Clarification check failed: {e}")
        needs_question = False

    if needs_question:
        # Send the question and track which message_id maps to which queue index
        sent = await msg.reply_text(
            f"🤔 Quick question before I queue this:\n\n*{question}*\n\n"
            f"_Use Telegram's Reply feature to answer me._",
            parse_mode="Markdown"
        )
        pending_questions[sent.message_id] = queue_index
    else:
        await msg.reply_text(
            f"{mode_label(personal_mode)} ✅ Queued ({len(note_queue)} total). "
            "Send more or /process when ready."
        )


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    photo = update.message.photo[-1]
    file  = await context.bot.get_file(photo.file_id)

    async with aiohttp.ClientSession() as session:
        async with session.get(file.file_path) as resp:
            image_bytes = await resp.read()

    b64     = base64.b64encode(image_bytes).decode("utf-8")
    caption = update.message.caption or ""

    # For images we queue directly — clarification via caption is enough
    note_queue.append({
        "type":          "image",
        "content":       b64,
        "media_type":    "image/jpeg",
        "caption":       caption,
        "clarification": None
    })

    queue_index = len(note_queue) - 1

    # If no caption, ask what this image is about
    if not caption:
        sent = await update.message.reply_text(
            "🖼️ Image received! Quick question:\n\n"
            "*What's the context for this image?*\n"
            "_(e.g. 'whiteboard from sprint planning', 'article screenshot about system design')_\n\n"
            "_Use Telegram's Reply feature to answer._",
            parse_mode="Markdown"
        )
        pending_questions[sent.message_id] = queue_index
    else:
        await update.message.reply_text(
            f"{mode_label(personal_mode)} 🖼️ Image queued ({len(note_queue)} total). "
            "Send more or /process when ready."
        )


async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    count = len(note_queue)
    if count == 0:
        await update.message.reply_text("📭 Queue is empty.")
    else:
        clarified   = sum(1 for n in note_queue if n.get("clarification"))
        unanswered  = len(pending_questions)
        await update.message.reply_text(
            f"📬 *{count} note(s)* in queue\n"
            f"✅ {clarified} enriched with clarification\n"
            f"⏳ {unanswered} awaiting your reply\n"
            f"Mode: {mode_label(personal_mode)}",
            parse_mode="Markdown"
        )


async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    note_queue.clear()
    pending_questions.clear()
    await update.message.reply_text("🗑️ Queue and pending questions cleared.")


async def process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not note_queue:
        await update.message.reply_text("📭 Nothing in the queue. Send some notes first!")
        return

    # Warn if there are unanswered questions
    if pending_questions:
        await update.message.reply_text(
            f"⚠️ You have *{len(pending_questions)} unanswered question(s)* from me.\n"
            "I'll process what I have, but those notes will be less detailed.\n"
            "Processing now...",
            parse_mode="Markdown"
        )

    count = len(note_queue)
    await update.message.reply_text(
        f"⚙️ Processing *{count} note(s)* in {mode_label(personal_mode)} mode...",
        parse_mode="Markdown"
    )

    try:
        # 1. Send to Claude
        content  = build_claude_messages(note_queue, personal_mode)
        response = claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": content}]
        )
        raw_output = response.content[0].text

        # 2. Parse notes
        notes = parse_notes(raw_output)
        if not notes:
            await update.message.reply_text("⚠️ Claude returned no structured notes. Try again.")
            return

        # 3. Push to GitHub
        pushed = []
        failed = []
        for note_md in notes:
            meta = extract_metadata(note_md)
            try:
                path = push_note_to_github(note_md, meta)
                folder_emoji = {
                    "fleeting":  "💭",
                    "literature":"📚",
                    "permanent": "🏛️",
                    "tasks":     "✅",
                    "people":    "👤",
                    "meetings":  "🤝",
                    "projects":  "🗂️",
                    "personal":  "🏠"
                }.get(meta["type"], "📝")
                pushed.append(f"{folder_emoji} `{path}`")
            except Exception as e:
                logger.error(f"GitHub push failed: {e}")
                failed.append(meta.get("title", "unknown"))

        # 4. Clear state
        note_queue.clear()
        pending_questions.clear()

        # 5. Summary
        summary = f"✅ *Done!* {len(pushed)} note(s) pushed to Obsidian:\n\n"
        summary += "\n".join(pushed)
        if failed:
            summary += f"\n\n⚠️ Failed to push: {', '.join(failed)}"

        await update.message.reply_text(summary, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Processing error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("personal", switch_personal))
    app.add_handler(CommandHandler("work",     switch_work))
    app.add_handler(CommandHandler("mode",     current_mode))
    app.add_handler(CommandHandler("queue",    queue_status))
    app.add_handler(CommandHandler("clear",    clear_queue))
    app.add_handler(CommandHandler("process",  process))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
