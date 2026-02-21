import os
import logging
import base64
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from github import Github, GithubException
import aiohttp

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN      = os.environ["GITHUB_TOKEN"]
GITHUB_REPO       = os.environ["GITHUB_REPO"]
ALLOWED_USER_ID   = int(os.environ["ALLOWED_USER_ID"])

# ── Clients ───────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gh     = Github(GITHUB_TOKEN)
repo   = gh.get_repo(GITHUB_REPO)

# ── State ─────────────────────────────────────────────────────────────────────
note_queue:        list[dict]     = []   # all queued notes
personal_mode:     bool           = False
pending_questions: dict[int, int] = {}   # msg_id → queue_index

# ── Constants ─────────────────────────────────────────────────────────────────
FOLDER_EMOJI = {
    "fleeting":  "💭",
    "literature":"📚",
    "permanent": "🏛️",
    "tasks":     "✅",
    "people":    "👤",
    "meetings":  "🤝",
    "projects":  "🗂️",
    "personal":  "🏠",
    "journal":   "📓",
}
ALL_FOLDERS = {
    "fleeting", "literature", "permanent", "tasks",
    "people", "meetings", "projects", "personal", "journal"
}


# ─────────────────────────────────────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────────────────────────────────────

CLARIFICATION_PROMPT = """You are a Zettelkasten assistant reviewing a raw note from an Engineering Manager.

Decide if ONE clarifying question would make this note significantly richer.

Ask if the note:
- Mentions a person/project/thing without enough context
- Has an action item without a deadline or next step
- Is vague enough that future-you might not understand it
- Has a decision that would benefit from knowing the "why"

Do NOT ask if:
- The note is already clear and self-contained
- It's a simple reminder or quick thought
- Asking would feel annoying or unnecessary
- The note starts with @, #, or ! (these are already structured)

RESPOND IN EXACTLY ONE FORMAT:

If no question needed:
CLEAR

If a question would help:
QUESTION: <single specific conversational question>

Raw note:
"""

WORK_PROMPT = """You are a Zettelkasten assistant for an Engineering Manager.

Process the raw notes below. Notes may have clarification context attached.

PREFIX RULES (highest priority — always follow these):
- Notes starting with @ are PEOPLE notes → type: people, extract person's full name
- Notes starting with # are PROJECT notes → type: projects, extract project name
- Notes starting with ! are MEETING notes → type: meetings, extract meeting name
- Notes with no prefix → choose the best folder from the list below

FOLDER TYPES:
- fleeting    → quick thought, reminder, something to revisit
- literature  → insight from article, book, podcast, or conversation
- permanent   → refined evergreen engineering or leadership principle
- tasks       → concrete action item
- people      → information about a specific person
- meetings    → notes or outcomes from a meeting
- projects    → ideas, status, or decisions about a project

REQUIRED FORMAT for every note:

---
id: <YYYYMMDDHHmm>
title: <Clear concise title>
type: <folder>
tags: [<tag1>, <tag2>]
links: []
---

<Body: 2-5 sentences.>

EXTRA FRONTMATTER FIELDS by type:
- tasks    → status: open   |   due: <YYYY-MM-DD or TBD>
- people   → person: <Full Name>
- meetings → meeting_name: <name>   |   attendees: []   |   date: <YYYY-MM-DD>
- projects → project: <Project Name>

TAGS: #people #process #technical #strategy #meeting #project #decision #risk #feedback #growth #hiring #delivery

RULES:
1. One atomic idea per note. Split if needed.
2. Extract ALL content from images.
3. Use clarification context to enrich the note.
4. Output ONLY notes separated by lines containing only ===
5. No commentary, no explanation outside the note blocks.

RAW NOTES:
"""

PERSONAL_PROMPT = """You are a Zettelkasten assistant organizing personal notes.

All notes go into the "personal" folder.

REQUIRED FORMAT:

---
id: <YYYYMMDDHHmm>
title: <Clear concise title>
type: personal
tags: [<tag1>, <tag2>]
links: []
---

<Body: 2-5 sentences.>

EXTRA FIELDS:
- If a task → status: open   |   due: <YYYY-MM-DD or TBD>
- If about a person → person: <Full Name>

TAGS: #health #family #finance #learning #goals #ideas #travel #reflection #reading #habits

RULES:
1. One idea per note. Split if needed.
2. Extract all content from images.
3. Use clarification context to enrich.
4. Output ONLY notes separated by lines containing only ===

RAW NOTES:
"""

PEOPLE_UPDATE_PROMPT = """You are updating an existing people profile note in a Zettelkasten system.

EXISTING NOTE:
{existing}

NEW OBSERVATION TO ADD (date: {date}):
{new_observation}

Your task:
1. Keep the existing frontmatter exactly as-is (do not change id, title, type, tags).
2. Keep the ## Profile section exactly as-is (the user fills this manually).
3. Append the new observation as a bullet under ## Observations with the date prefix.
4. If the observation contains a task or action item, append it under ## Action Items.
5. Output the COMPLETE updated note — nothing else.

FORMAT for new observation bullet:
- {date}: <concise observation from the new note>

FORMAT for new action item (only if there is one):
- {date}: <action item> → [[tasks/<path>|<title>]] (if a task note was created)
"""

PROJECT_UPDATE_PROMPT = """You are updating an existing project note in a Zettelkasten system.

EXISTING NOTE:
{existing}

NEW UPDATE TO ADD (date: {date}):
{new_update}

Your task:
1. Keep the existing frontmatter exactly as-is.
2. Keep the ## Summary section — update it in place if the new info changes the summary significantly, otherwise leave it.
3. Append the new update as a bullet under ## Updates with the date prefix.
4. If there is a decision, append under ## Decisions.
5. Output the COMPLETE updated note — nothing else.

FORMAT for new update bullet:
- {date}: <concise update>
"""

SUMMARY_PROMPT = """You are summarizing an Engineering Manager's notes for the day.

Write a 2-3 sentence prose summary of the day's key themes and focus areas.
No lists, no headers — just flowing, insightful prose written for the person themselves.

NOTES:
"""


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID

def now_id() -> str:
    return datetime.now().strftime("%Y%m%d%H%M")

def today_str() -> str:
    return datetime.now().strftime("%d-%m-%Y")

def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def mode_label(is_personal: bool) -> str:
    return "🏠 Personal" if is_personal else "💼 Work"

def make_slug(name: str) -> str:
    """Convert a name/title to a safe filename slug."""
    slug = re.sub(r'[^\w\s-]', '', name).strip().lower()
    slug = re.sub(r'[\s_]+', '-', slug)
    return slug[:60]

def obsidian_link(path: str, title: str) -> str:
    """[[folder/file|Title]] — no .md extension."""
    return f"[[{path.replace('.md', '')}|{title}]]"

def extract_prefix(content: str) -> tuple[str, str]:
    """
    Returns (prefix_type, cleaned_content).
    prefix_type: 'people' | 'projects' | 'meetings' | None
    """
    content = content.strip()
    if content.startswith('@'):
        return 'people', content
    if content.startswith('#'):
        return 'projects', content
    if content.startswith('!'):
        return 'meetings', content
    return None, content

def extract_frontmatter_field(note_md: str, field: str) -> str | None:
    match = re.search(rf'^{field}:\s*(.+)$', note_md, re.MULTILINE)
    return match.group(1).strip() if match else None

def parse_notes_output(raw: str) -> list[str]:
    blocks = re.split(r'\n===\n|^===\n', raw.strip(), flags=re.MULTILINE)
    return [b.strip() for b in blocks if b.strip()]

def build_stats(notes: list[dict]) -> str:
    counts: dict[str, int] = {}
    for n in notes:
        t = n.get("type", "fleeting").capitalize()
        counts[t] = counts.get(t, 0) + 1
    return " | ".join(f"{k}: {v}" for k, v in sorted(counts.items()))


# ─────────────────────────────────────────────────────────────────────────────
# GITHUB OPERATIONS
# ─────────────────────────────────────────────────────────────────────────────

def find_existing_file(folder: str, slug: str) -> tuple[str, str, str] | None:
    """
    Search folder for a file whose name contains the slug.
    Returns (path, content, sha) or None.
    """
    try:
        contents = repo.get_contents(folder)
        for f in contents:
            if slug in f.name:
                content = f.decoded_content.decode("utf-8")
                return f.path, content, f.sha
    except GithubException:
        pass
    return None


def upsert_people_note(full_name: str, new_observation: str, task_links: list[str]) -> tuple[str, str, bool]:
    """
    Create or update a people profile note.
    Returns (path, title, was_updated).
    """
    slug    = make_slug(full_name)
    date    = today_iso()
    title   = full_name
    path    = f"people/{slug}.md"

    existing = find_existing_file("people", slug)

    if existing:
        existing_path, existing_content, existing_sha = existing

        # Build task links string
        task_str = ""
        for tl in task_links:
            task_str += f"\n- {date}: {tl}"

        # Ask Claude to update the note
        prompt = PEOPLE_UPDATE_PROMPT.format(
            existing=existing_content,
            date=date,
            new_observation=new_observation,
        )
        if task_links:
            prompt += f"\n\nACTION ITEMS TO ADD:{task_str}"

        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        updated_content = response.content[0].text.strip()
        repo.update_file(
            path=existing_path,
            message=f"people: update {full_name} — {date}",
            content=updated_content,
            sha=existing_sha
        )
        return existing_path, title, True

    else:
        # Create fresh profile
        note_id = now_id()
        task_section = ""
        if task_links:
            task_section = "\n## Action Items\n"
            for tl in task_links:
                task_section += f"- {date}: {tl}\n"

        content = (
            f"---\n"
            f"id: {note_id}\n"
            f"title: {full_name}\n"
            f"type: people\n"
            f"person: {full_name}\n"
            f"tags: [#people]\n"
            f"links: []\n"
            f"---\n\n"
            f"## Profile\n"
            f"- **Role:** *(update as needed)*\n"
            f"- **Team:** *(update as needed)*\n"
            f"- **Started:** *(update as needed)*\n\n"
            f"## Observations\n"
            f"- {date}: {new_observation}\n"
            f"{task_section}"
        )
        repo.create_file(path=path, message=f"people: create {full_name}", content=content)
        return path, title, False


def upsert_project_note(project_name: str, new_update: str, task_links: list[str]) -> tuple[str, str, bool]:
    """
    Create or update a project note.
    Returns (path, title, was_updated).
    """
    slug  = make_slug(project_name)
    date  = today_iso()
    title = project_name
    path  = f"projects/{slug}.md"

    existing = find_existing_file("projects", slug)

    if existing:
        existing_path, existing_content, existing_sha = existing

        prompt = PROJECT_UPDATE_PROMPT.format(
            existing=existing_content,
            date=date,
            new_update=new_update,
        )
        if task_links:
            prompt += f"\n\nRELATED TASKS:\n" + "\n".join(f"- {tl}" for tl in task_links)

        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        updated_content = response.content[0].text.strip()
        repo.update_file(
            path=existing_path,
            message=f"projects: update {project_name} — {date}",
            content=updated_content,
            sha=existing_sha
        )
        return existing_path, title, True

    else:
        note_id = now_id()
        task_section = ""
        if task_links:
            task_section = "\n## Action Items\n"
            for tl in task_links:
                task_section += f"- {date}: {tl}\n"

        content = (
            f"---\n"
            f"id: {note_id}\n"
            f"title: {project_name}\n"
            f"type: projects\n"
            f"project: {project_name}\n"
            f"tags: [#project]\n"
            f"links: []\n"
            f"---\n\n"
            f"## Summary\n"
            f"*(Claude will update this as more context is added)*\n\n"
            f"## Updates\n"
            f"- {date}: {new_update}\n"
            f"{task_section}"
        )
        repo.create_file(path=path, message=f"projects: create {project_name}", content=content)
        return path, title, False


def push_regular_note(note_md: str) -> tuple[str, str, str]:
    """
    Push a regular (non-people, non-project) note to GitHub.
    Returns (path, title, note_type).
    """
    note_type = extract_frontmatter_field(note_md, "type") or "fleeting"
    title     = extract_frontmatter_field(note_md, "title") or "untitled"
    note_id   = extract_frontmatter_field(note_md, "id") or now_id()

    if note_type not in ALL_FOLDERS:
        note_type = "fleeting"

    filename = f"{note_id}-{make_slug(title)}.md"
    path     = f"{note_type}/{filename}"
    repo.create_file(path=path, message=f"add: {title}", content=note_md)
    return path, title, note_type


def push_meeting_note(note_md: str, meeting_name: str) -> tuple[str, str]:
    """
    Push a meeting note. Uses meeting name + date for filename.
    Returns (path, title).
    """
    title    = extract_frontmatter_field(note_md, "title") or f"{meeting_name} {today_iso()}"
    note_id  = extract_frontmatter_field(note_md, "id") or now_id()
    filename = f"{note_id}-{make_slug(meeting_name)}.md"
    path     = f"meetings/{filename}"
    repo.create_file(path=path, message=f"meetings: {meeting_name} {today_iso()}", content=note_md)
    return path, title


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE CALLS
# ─────────────────────────────────────────────────────────────────────────────

async def check_if_needs_clarification(content: str) -> tuple[bool, str]:
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": CLARIFICATION_PROMPT + content}]
        )
        reply = response.content[0].text.strip()
        if reply.startswith("QUESTION:"):
            return True, reply[len("QUESTION:"):].strip()
    except Exception as e:
        logger.error(f"Clarification check error: {e}")
    return False, ""


def build_processing_messages(queue: list[dict], is_personal: bool) -> list:
    prompt  = PERSONAL_PROMPT if is_personal else WORK_PROMPT
    content = [{"type": "text", "text": prompt}]
    for i, note in enumerate(queue):
        content.append({"type": "text", "text": f"\n--- Note {i+1} ---\n"})
        if note["type"] == "text":
            content.append({"type": "text", "text": note["content"]})
        elif note["type"] == "image":
            if note.get("caption"):
                content.append({"type": "text", "text": f"Caption: {note['caption']}"})
            content.append({
                "type":   "image",
                "source": {"type": "base64", "media_type": note["media_type"], "data": note["data"]}
            })
        if note.get("clarification"):
            content.append({"type": "text", "text": f"[Clarification]: {note['clarification']}"})
    return content


def get_daily_summary(notes: list[dict]) -> str:
    if not notes:
        return "Notes captured and organized."
    notes_text = "\n".join(f"- [{n['type']}] {n['title']}" for n in notes)
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": SUMMARY_PROMPT + notes_text}]
        )
        return response.content[0].text.strip()
    except Exception:
        return "Notes processed and organized."


# ─────────────────────────────────────────────────────────────────────────────
# JOURNAL
# ─────────────────────────────────────────────────────────────────────────────

def update_journal(queue_snapshot: list[dict], pushed_notes: list[dict], summary: str, stats: str):
    """Create or append to today's journal note."""
    date_str = today_str()
    path     = f"journal/{date_str}.md"
    now_time = datetime.now().strftime("%H:%M")

    lines = [f"\n## 🕐 Session — {now_time}\n\n"]
    lines.append(f"**Summary:** {summary}\n\n")
    lines.append(f"**Stats:** {stats}\n\n")
    lines.append("### Notes\n\n")

    for i, raw in enumerate(queue_snapshot):
        note_time = raw.get("time", now_time)

        # Raw preview
        if raw["type"] == "text":
            preview = raw["content"][:150] + ("..." if len(raw["content"]) > 150 else "")
        else:
            preview = f"[Image] {raw.get('caption', 'no caption')}"

        lines.append(f"**{note_time}** — {preview}\n")

        if raw.get("clarification"):
            lines.append(f"> 💬 *{raw['clarification']}*\n")

        # All processed notes linked to this raw note
        related = [n for n in pushed_notes if n.get("source_index") == i]
        for pn in related:
            emoji = FOLDER_EMOJI.get(pn["type"], "📝")
            link  = obsidian_link(pn["path"], pn["title"])
            tag   = " *(updated)*" if pn.get("updated") else ""
            lines.append(f"  → {emoji} {link}{tag}\n")

        lines.append("\n")

    section = "".join(lines)

    try:
        existing = repo.get_contents(path)
        current  = existing.decoded_content.decode("utf-8")
        repo.update_file(
            path=path,
            message=f"journal: session {now_time} — {date_str}",
            content=current + section,
            sha=existing.sha
        )
    except GithubException:
        header = (
            f"---\n"
            f"date: {date_str}\n"
            f"type: journal\n"
            f"---\n\n"
            f"# 📓 Journal — {date_str}\n"
        )
        repo.create_file(
            path=path,
            message=f"journal: create {date_str}",
            content=header + section
        )


def log_pending_to_journal(queue_snapshot: list[dict]):
    """Save unprocessed notes to journal without structuring them."""
    date_str = today_str()
    path     = f"journal/{date_str}.md"
    now_time = datetime.now().strftime("%H:%M")

    lines = [f"\n## ⏳ Pending — {now_time}\n\n"]
    lines.append("*Queued but not yet processed.*\n\n")
    for raw in queue_snapshot:
        if raw["type"] == "text":
            preview = raw["content"][:150]
        else:
            preview = f"[Image] {raw.get('caption', '')}"
        lines.append(f"- {preview}\n")

    section = "".join(lines)

    try:
        existing = repo.get_contents(path)
        current  = existing.decoded_content.decode("utf-8")
        repo.update_file(
            path=path,
            message=f"journal: pending {date_str}",
            content=current + section,
            sha=existing.sha
        )
    except GithubException:
        header = f"---\ndate: {date_str}\ntype: journal\n---\n\n# 📓 Journal — {date_str}\n"
        repo.create_file(
            path=path,
            message=f"journal: create {date_str} (pending)",
            content=header + section
        )


# ─────────────────────────────────────────────────────────────────────────────
# PROCESSING PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def process_notes_pipeline(queue_snapshot: list[dict], is_personal: bool) -> list[dict]:
    """
    Full pipeline:
    1. Call Claude to structure all notes
    2. For each structured note:
       - If people (@) → upsert people profile
       - If projects (#) → upsert project note
       - If meetings (!) → push new meeting note
       - Otherwise → push regular note
    3. Return list of pushed note metadata for journal
    """

    # Step 1: Claude structures everything
    messages   = build_processing_messages(queue_snapshot, is_personal)
    response   = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": messages}]
    )
    raw_output = response.content[0].text
    note_blocks = parse_notes_output(raw_output)

    pushed: list[dict] = []

    # Step 2: Separate tasks first so we can link them from people/project notes
    task_notes = []
    other_notes = []
    for i, block in enumerate(note_blocks):
        note_type = extract_frontmatter_field(block, "type") or "fleeting"
        source_i  = i % len(queue_snapshot) if queue_snapshot else 0
        if note_type == "tasks":
            task_notes.append((i, block, source_i))
        else:
            other_notes.append((i, block, source_i))

    # Push tasks first and collect links
    task_links_by_source: dict[int, list[str]] = {}
    for i, block, source_i in task_notes:
        try:
            path, title, note_type = push_regular_note(block)
            link = obsidian_link(path, title)
            task_links_by_source.setdefault(source_i, []).append(link)
            pushed.append({
                "path":         path,
                "title":        title,
                "type":         "tasks",
                "source_index": source_i,
                "updated":      False,
            })
        except Exception as e:
            logger.error(f"Task push failed: {e}")

    # Step 3: Push remaining notes
    for i, block, source_i in other_notes:
        note_type = extract_frontmatter_field(block, "type") or "fleeting"

        # Get task links relevant to this source note
        tlinks = task_links_by_source.get(source_i, [])

        try:
            if note_type == "people":
                person   = extract_frontmatter_field(block, "person") or "Unknown"
                # Get body text (after frontmatter)
                body     = re.split(r'---\s*\n', block, maxsplit=2)[-1].strip()
                path, title, updated = upsert_people_note(person, body, tlinks)
                pushed.append({
                    "path":         path,
                    "title":        title,
                    "type":         "people",
                    "source_index": source_i,
                    "updated":      updated,
                })

            elif note_type == "projects":
                project  = extract_frontmatter_field(block, "project") or "Unknown"
                body     = re.split(r'---\s*\n', block, maxsplit=2)[-1].strip()
                path, title, updated = upsert_project_note(project, body, tlinks)
                pushed.append({
                    "path":         path,
                    "title":        title,
                    "type":         "projects",
                    "source_index": source_i,
                    "updated":      updated,
                })

            elif note_type == "meetings":
                meeting_name = extract_frontmatter_field(block, "meeting_name") or "Meeting"
                path, title  = push_meeting_note(block, meeting_name)
                pushed.append({
                    "path":         path,
                    "title":        title,
                    "type":         "meetings",
                    "source_index": source_i,
                    "updated":      False,
                })

            else:
                path, title, note_type = push_regular_note(block)
                pushed.append({
                    "path":         path,
                    "title":        title,
                    "type":         note_type,
                    "source_index": source_i,
                    "updated":      False,
                })

        except Exception as e:
            logger.error(f"Note push failed ({note_type}): {e}")

    return pushed


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text(
        "👋 *Zettelkasten Bot v5*\n\n"
        "*Prefixes:*\n"
        "`@Full Name` → 👤 People profile (creates or updates)\n"
        "`#Project Name` → 🗂️ Project note (creates or updates)\n"
        "`!Meeting Name` → 🤝 Meeting note (new each time)\n"
        "_(no prefix)_ → Claude picks the best folder\n\n"
        "*Commands:*\n"
        "/process — structure all notes & update journal\n"
        "/pending — save queue to journal without processing\n"
        "/queue — see queued notes\n"
        "/clear — clear the queue\n"
        "/personal — 🏠 Personal mode\n"
        "/work — 💼 Work mode (default)\n"
        "/mode — current mode\n\n"
        f"Mode: {mode_label(personal_mode)}",
        parse_mode="Markdown"
    )


async def cmd_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    global personal_mode
    personal_mode = True
    await update.message.reply_text("🏠 Switched to *Personal mode*.", parse_mode="Markdown")


async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    global personal_mode
    personal_mode = False
    await update.message.reply_text("💼 Switched to *Work mode*.", parse_mode="Markdown")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    await update.message.reply_text(f"Current mode: {mode_label(personal_mode)}")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    count      = len(note_queue)
    clarified  = sum(1 for n in note_queue if n.get("clarification"))
    unanswered = len(pending_questions)
    if count == 0:
        await update.message.reply_text("📭 Queue is empty.")
        return
    await update.message.reply_text(
        f"📬 *{count} note(s)* queued\n"
        f"✅ {clarified} enriched\n"
        f"⏳ {unanswered} awaiting reply\n"
        f"Mode: {mode_label(personal_mode)}",
        parse_mode="Markdown"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    note_queue.clear()
    pending_questions.clear()
    await update.message.reply_text("🗑️ Queue cleared.")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    if not note_queue:
        await update.message.reply_text("📭 Nothing in the queue.")
        return
    try:
        log_pending_to_journal(note_queue.copy())
        count = len(note_queue)
        note_queue.clear()
        pending_questions.clear()
        await update.message.reply_text(
            f"💾 *{count} note(s)* saved to `journal/{today_str()}.md` as pending.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return

    if not note_queue:
        await update.message.reply_text("📭 Nothing to process.")
        return

    if pending_questions:
        await update.message.reply_text(
            f"⚠️ *{len(pending_questions)} unanswered question(s)* — processing anyway.",
            parse_mode="Markdown"
        )

    count = len(note_queue)
    await update.message.reply_text(
        f"⚙️ Processing *{count} note(s)*...",
        parse_mode="Markdown"
    )

    queue_snapshot = note_queue.copy()

    try:
        # Run pipeline
        pushed = process_notes_pipeline(queue_snapshot, personal_mode)

        if not pushed:
            await update.message.reply_text("⚠️ No notes were created. Check your input and try again.")
            return

        # Summary & stats
        summary = get_daily_summary(pushed)
        stats   = build_stats(pushed)

        # Journal
        journal_ok = True
        try:
            update_journal(queue_snapshot, pushed, summary, stats)
        except Exception as e:
            logger.error(f"Journal error: {e}")
            journal_ok = False

        # Clear state
        note_queue.clear()
        pending_questions.clear()

        # Build reply
        lines = [f"✅ *Done!* {len(pushed)} note(s):\n"]
        for n in pushed:
            emoji  = FOLDER_EMOJI.get(n["type"], "📝")
            tag    = " _(updated)_" if n.get("updated") else ""
            lines.append(f"{emoji} `{n['path']}`{tag}")

        lines.append(f"\n📊 {stats}")
        lines.append(f"📓 Journal {'✅' if journal_ok else '⚠️ failed'}: `journal/{today_str()}.md`")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Process error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return

    msg = update.message

    # Reply to clarifying question?
    if msg.reply_to_message and msg.reply_to_message.message_id in pending_questions:
        idx = pending_questions.pop(msg.reply_to_message.message_id)
        if idx < len(note_queue):
            note_queue[idx]["clarification"] = msg.text
            await msg.reply_text("✅ Context added.")
        else:
            await msg.reply_text("⚠️ Couldn't find the original note.")
        return

    # New note
    now_time = datetime.now().strftime("%H:%M")
    note_queue.append({
        "type":          "text",
        "content":       msg.text,
        "clarification": None,
        "time":          now_time,
    })
    idx = len(note_queue) - 1

    # Detect prefix for confirmation message
    prefix_type, _ = extract_prefix(msg.text)
    prefix_labels  = {
        "people":   "👤 People note",
        "projects": "🗂️ Project note",
        "meetings": "🤝 Meeting note",
    }
    prefix_tag = f" _{prefix_labels.get(prefix_type, '')}_" if prefix_type else ""

    # Clarification check (skip for prefixed notes — they're already structured)
    if not prefix_type:
        try:
            needs_q, question = await check_if_needs_clarification(msg.text)
        except Exception as e:
            logger.error(f"Clarification error: {e}")
            needs_q = False

        if needs_q:
            sent = await msg.reply_text(
                f"🤔 *Quick question:*\n\n{question}\n\n_Reply to this message to answer._",
                parse_mode="Markdown"
            )
            pending_questions[sent.message_id] = idx
            return

    await msg.reply_text(
        f"{mode_label(personal_mode)}{prefix_tag} ✅ Queued ({len(note_queue)} total)."
    )


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return

    photo    = update.message.photo[-1]
    file     = await context.bot.get_file(photo.file_id)
    now_time = datetime.now().strftime("%H:%M")

    async with aiohttp.ClientSession() as session:
        async with session.get(file.file_path) as resp:
            image_bytes = await resp.read()

    caption = update.message.caption or ""

    note_queue.append({
        "type":          "image",
        "data":          base64.b64encode(image_bytes).decode("utf-8"),
        "media_type":    "image/jpeg",
        "caption":       caption,
        "clarification": None,
        "time":          now_time,
    })
    idx = len(note_queue) - 1

    if not caption:
        sent = await update.message.reply_text(
            "🖼️ Image received!\n\n"
            "*What's the context?* _(e.g. 'whiteboard from sprint planning')_\n\n"
            "_Reply to this message to answer._",
            parse_mode="Markdown"
        )
        pending_questions[sent.message_id] = idx
    else:
        await update.message.reply_text(
            f"{mode_label(personal_mode)} 🖼️ Image queued ({len(note_queue)} total)."
        )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("personal", cmd_personal))
    app.add_handler(CommandHandler("work",     cmd_work))
    app.add_handler(CommandHandler("mode",     cmd_mode))
    app.add_handler(CommandHandler("queue",    cmd_queue))
    app.add_handler(CommandHandler("clear",    cmd_clear))
    app.add_handler(CommandHandler("pending",  cmd_pending))
    app.add_handler(CommandHandler("process",  cmd_process))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))

    logger.info("Zettelkasten bot v5 running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
