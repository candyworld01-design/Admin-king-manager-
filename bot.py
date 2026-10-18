import os
import re
import sqlite3
import hashlib
import time
import logging

from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIGURATION ----------------

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "admin_manager.db")
TZ = ZoneInfo("Asia/Kolkata")
DUPLICATE_WINDOW = 24 * 60 * 60

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ---------------- DATABASE ----------------

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            min_score INTEGER NOT NULL DEFAULT 100,
            min_activity INTEGER NOT NULL DEFAULT 10
        );

        CREATE TABLE IF NOT EXISTS scores (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            week TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id, week)
        );

        CREATE TABLE IF NOT EXISTS seen_messages (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            signature TEXT NOT NULL,
            seen_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS seen_lookup
        ON seen_messages(chat_id, user_id, signature, seen_at);

        CREATE TABLE IF NOT EXISTS managed_admins (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            low_weeks INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        );
        """)


def current_week():
    now = datetime.now(TZ)
    monday = now.date().toordinal() - now.weekday()
    return datetime.fromordinal(monday).date().isoformat()


def ensure_settings(chat_id):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO settings(chat_id) VALUES(?)",
            (chat_id,),
        )


def get_settings(chat_id):
    ensure_settings(chat_id)
    with db() as con:
        return con.execute(
            "SELECT * FROM settings WHERE chat_id=?",
            (chat_id,),
        ).fetchone()


def get_score(chat_id, user_id):
    with db() as con:
        row = con.execute("""
            SELECT points FROM scores
            WHERE chat_id=? AND user_id=? AND week=?
        """, (chat_id, user_id, current_week())).fetchone()
        return row["points"] if row else 0


# ---------------- PERMISSION CHECK ----------------

async def is_owner(update, context):
    if not update.effective_chat or not update.effective_user:
        return False

    member = await context.bot.get_chat_member(
        update.effective_chat.id,
        update.effective_user.id,
    )
    return member.status == "creator"


async def owner_only(update, context):
    if not await is_owner(update, context):
        await update.effective_message.reply_text(
            "⛔ Sirf group OWNER ye setting change kar sakta hai."
        )
        return False
    return True


# ---------------- START / SETUP ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👑 Admin Manager Bot online hai!\n\n"
        "Mujhe group mein admin banao aur message visibility "
        "enable karo.\n\n"
        "/setup - Group setup\n"
        "/rules - Current rules\n"
        "/leaderboard - Weekly ranking\n"
        "/myrank - Apna score"
    )


async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if not chat or chat.type not in ("group", "supergroup"):
        return

    if not await owner_only(update, context):
        return

    ensure_settings(chat.id)

    await update.effective_message.reply_text(
        "✅ Setup complete!\n\n"
        "Default minimum winner score: 100\n"
        "Default minimum weekly admin activity: 10\n\n"
        "Owner commands:\n"
        "/setscore 100\n"
        "/setactivity 10\n"
        "/rules"
    )


# ---------------- OWNER SETTINGS ----------------

async def setscore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if not chat or chat.type not in ("group", "supergroup"):
        return

    if not await owner_only(update, context):
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: /setscore 100\n"
            "Score 1 se 1000000 ke beech hona chahiye."
        )
        return

    value = int(context.args[0])

    if not 1 <= value <= 1_000_000:
        await update.effective_message.reply_text("Invalid score.")
        return

    ensure_settings(chat.id)

    with db() as con:
        con.execute(
            "UPDATE settings SET min_score=? WHERE chat_id=?",
            (value, chat.id),
        )

    await update.effective_message.reply_text(
        f"👑 Minimum weekly winner score ab {value} hai."
    )


async def setactivity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if not chat or chat.type not in ("group", "supergroup"):
        return

    if not await owner_only(update, context):
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: /setactivity 10"
        )
        return

    value = int(context.args[0])

    if not 0 <= value <= 100_000:
        await update.effective_message.reply_text("Invalid value.")
        return

    ensure_settings(chat.id)

    with db() as con:
        con.execute(
            "UPDATE settings SET min_activity=? WHERE chat_id=?",
            (value, chat.id),
        )

    await update.effective_message.reply_text(
        f"✅ Admin retention ke liye weekly minimum "
        f"activity ab {value} points hai."
    )


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if not chat or chat.type not in ("group", "supergroup"):
        return

    s = get_settings(chat.id)

    await update.effective_message.reply_text(
        "📋 GROUP RULES\n\n"
        f"👑 Minimum winner score: {s['min_score']}\n"
        f"📊 Minimum admin activity: {s['min_activity']}\n"
        "💬 Unique message: +1 point\n"
        "🚫 Duplicate within 24 hours: 0 points\n"
        "🏆 Weekly winner selection: Sunday\n"
        "🔄 Two consecutive low-activity weeks: demotion check"
    )


# ---------------- MESSAGE SCORING ----------------

def make_signature(message):
    text = message.text or message.caption

    if text is not None:
        text = text.casefold().strip()
        text = re.sub(r"[!?.…,;:]+", "", text)
        text = re.sub(r"\s+", " ", text)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    media = None

    if message.sticker:
        media = message.sticker.file_unique_id
    elif message.photo:
        media = message.photo[-1].file_unique_id
    elif message.video:
        media = message.video.file_unique_id
    elif message.document:
        media = message.document.file_unique_id
    elif message.animation:
        media = message.animation.file_unique_id
    elif message.voice:
        media = message.voice.file_unique_id
    elif message.audio:
        media = message.audio.file_unique_id
    elif message.video_note:
        media = message.video_note.file_unique_id

    if media:
        return hashlib.sha256(
            f"media:{media}".encode("utf-8")
        ).hexdigest()

    # Messages without identifiable content are not scored.
    return None


async def track_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if user.is_bot or message.sender_chat:
        return

    # Commands are not scored.
    if message.text and message.text.startswith("/"):
        return

    signature = make_signature(message)

    if not signature:
        return

    ensure_settings(chat.id)

    now = int(time.time())
    week = current_week()
    cutoff = now - DUPLICATE_WINDOW

    with db() as con:
        # Remove expired duplicate records.
        con.execute(
            "DELETE FROM seen_messages WHERE seen_at < ?",
            (cutoff,),
        )

        duplicate = con.execute("""
            SELECT 1 FROM seen_messages
            WHERE chat_id=? AND user_id=?
              AND signature=? AND seen_at>=?
            LIMIT 1
        """, (chat.id, user.id, signature, cutoff)).fetchone()

        con.execute("""
            INSERT INTO seen_messages
            (chat_id, user_id, signature, seen_at)
            VALUES (?, ?, ?, ?)
        """, (chat.id, user.id, signature, now))

        if duplicate:
            return

        con.execute("""
            INSERT INTO scores(chat_id, user_id, week, points)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id, week)
            DO UPDATE SET points=points+1
        """, (chat.id, user.id, week))


# ---------------- LEADERBOARD ----------------

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if not chat or chat.type not in ("group", "supergroup"):
        return

    with db() as con:
        rows = con.execute("""
            SELECT user_id, points FROM scores
            WHERE chat_id=? AND week=?
            ORDER BY points DESC
            LIMIT 10
        """, (chat.id, current_week())).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "Abhi is week koi scored message nahi hai. 💬"
        )
        return

    lines = ["🏆 WEEKLY LEADERBOARD\n"]

    for i, row in enumerate(rows, start=1):
        try:
            member = await context.bot.get_chat_member(
                chat.id, row["user_id"]
            )
            name = member.user.full_name
        except Exception:
            name = f"User {row['user_id']}"

        lines.append(f"{i}. {name} — {row['points']} points")

    await update.effective_message.reply_text("\n".join(lines))


async def myrank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    score = get_score(chat.id, user.id)
    s = get_settings(chat.id)

    with db() as con:
        row = con.execute("""
            SELECT COUNT(*) AS ahead FROM scores
            WHERE chat_id=? AND week=? AND points>?
        """, (chat.id, current_week(), score)).fetchone()

    rank = row["ahead"] + 1

    await update.effective_message.reply_text(
        f"👤 {user.full_name}\n"
        f"⭐ Weekly score: {score}\n"
        f"🏆 Rank: {rank}\n"
        f"🎯 Winner eligibility score: {s['min_score']}"
    )


# ---------------- WEEKLY ADMIN MANAGEMENT ----------------

async def weekly_review(context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        chats = con.execute(
            "SELECT * FROM settings"
        ).fetchall()

    week = current_week()

    for setting in chats:
        chat_id = setting["chat_id"]
        min_score = setting["min_score"]
        min_activity = setting["min_activity"]

        try:
            with db() as con:
                admins = con.execute("""
                    SELECT user_id, low_weeks FROM managed_admins
                    WHERE chat_id=?
                """, (chat_id,)).fetchall()

            # Review only admins appointed by this bot.
            for admin in admins:
                user_id = admin["user_id"]

                with db() as con:
                    row = con.execute("""
                        SELECT points FROM scores
                        WHERE chat_id=? AND user_id=? AND week=?
                    """, (chat_id, user_id, week)).fetchone()

                activity = row["points"] if row else 0

                if activity < min_activity:
                    low_weeks = admin["low_weeks"] + 1
                else:
                    low_weeks = 0

                if low_weeks >= 2:
                    try:
                        member = await context.bot.get_chat_member(
                            chat_id, user_id
                        )

                        if member.status == "administrator":
                            await context.bot.promote_chat_member(
                                chat_id=chat_id,
                                user_id=user_id,
                                can_manage_chat=False,
                                can_delete_messages=False,
                                can_manage_video_chats=False,
                                can_restrict_members=False,
                                can_promote_members=False,
                                can_change_info=False,
                                can_invite_users=False,
                                can_pin_messages=False,
                                can_manage_topics=False,
                            )

                        with db() as con:
                            con.execute("""
                                DELETE FROM managed_admins
                                WHERE chat_id=? AND user_id=?
                            """, (chat_id, user_id))

                        await context.bot.send_message(
                            chat_id,
                            f"🔄 Admin activity requirement poori "
                            f"na hone par user {user_id} ka admin "
                            f"status remove karne ka review complete hua."
                        )
                    except Exception:
                        log.exception("Admin demotion failed")

                else:
                    with db() as con:
                        con.execute("""
                            UPDATE managed_admins
                            SET low_weeks=?
                            WHERE chat_id=? AND user_id=?
                        """, (low_weeks, chat_id, user_id))

            # Find highest-scoring eligible member.
            with db() as con:
                winner = con.execute("""
                    SELECT user_id, points FROM scores
                    WHERE chat_id=? AND week=? AND points>=?
                    ORDER BY points DESC, user_id ASC
                    LIMIT 1
                """, (chat_id, week, min_score)).fetchone()

            if winner:
                user_id = winner["user_id"]

                member = await context.bot.get_chat_member(
                    chat_id, user_id
                )

                if member.status == "member":
                    # Only grant the configured basic admin rights.
                    await context.bot.promote_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        can_manage_chat=True,
                        can_delete_messages=True,
                        can_invite_users=True,
                        can_pin_messages=True,
                        can_restrict_members=False,
                        can_promote_members=False,
                        can_change_info=False,
                        can_manage_video_chats=False,
                        can_manage_topics=False,
                    )

                    with db() as con:
                        con.execute("""
                            INSERT OR IGNORE INTO managed_admins
                            (chat_id, user_id, low_weeks)
                            VALUES (?, ?, 0)
                        """, (chat_id, user_id))

                    await context.bot.send_message(
                        chat_id,
                        f"👑 Weekly winner promoted!\n"
                        f"User ID: {user_id}\n"
                        f"Score: {winner['points']}\n"
                        f"Congratulations! 🎉"
                    )

        except Exception:
            log.exception("Weekly review failed for chat %s", chat_id)


# ---------------- MAIN ----------------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing!")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setup", setup))
    app.add_handler(CommandHandler("setscore", setscore))
    app.add_handler(CommandHandler("setactivity", setactivity))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("myrank", myrank))

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.StatusUpdate.ALL,
            track_message,
        )
    )

    app.job_queue.run_daily(
        weekly_review,
        time=dt_time(hour=23, minute=59, tzinfo=TZ),
        days=(0,),
        name="weekly_admin_review",
    )

    print("Admin Manager Bot is running!")
    app.run_polling()


if __name__ == "__main__":
    main()
