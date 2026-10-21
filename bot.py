"""
ADMIN KING MANAGER — simple, configurable Telegram group competition bot
Python: 3.11+
Hosting: Render Web Service
Required Render environment variables:
  BOT_TOKEN = Telegram bot token
  OWNER_ID  = (optional) Telegram numeric user ID allowed to manage settings.
               If omitted, only the Telegram group creator can change settings.

Important:
- Add the bot to your group and promote it to admin.
- For activity tracking, disable privacy mode in @BotFather OR make the bot admin.
- For winner promotion, the bot must have "Add new admins" / can_promote_members.
- SQLite data survives ordinary restarts, but Render's ephemeral disk may not survive
  redeploys/replacements unless a persistent disk is attached.
"""

import os
import re
import html
import time
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask, jsonify
from telegram import Update, ChatMemberAdministrator, ChatMemberOwner
from telegram.constants import ChatType
from telegram.error import TelegramError, BadRequest, Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("admin-king-manager")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None
TZ = ZoneInfo("Asia/Kolkata")
DB_PATH = os.getenv("DB_PATH", "admin_king_manager.db")

web = Flask(__name__)

@web.get("/")
def home():
    return "Admin King Manager is alive!", 200

@web.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Admin King Manager"}), 200

def web_server():
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)

def now_ts():
    return int(time.time())

def connect():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            duration_days INTEGER NOT NULL DEFAULT 7,
            points_per_message INTEGER NOT NULL DEFAULT 1,
            winner_bonus INTEGER NOT NULL DEFAULT 20,
            competition_start INTEGER NOT NULL,
            competition_end INTEGER NOT NULL,
            encouragement INTEGER NOT NULL DEFAULT 1,
            comeback_alerts INTEGER NOT NULL DEFAULT 1,
            winner_admin INTEGER NOT NULL DEFAULT 0,
            rotation_enabled INTEGER NOT NULL DEFAULT 0,
            max_points_per_day INTEGER NOT NULL DEFAULT 50,
            min_messages INTEGER NOT NULL DEFAULT 500
        );
        CREATE TABLE IF NOT EXISTS scores (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            messages INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            best_rank INTEGER NOT NULL DEFAULT 0,
            last_active INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS protected (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            added_by INTEGER NOT NULL,
            added_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS seen_messages (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            signature TEXT NOT NULL,
            seen_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS announcements (
            chat_id INTEGER NOT NULL,
            competition_end INTEGER NOT NULL,
            PRIMARY KEY (chat_id, competition_end)
        );
        CREATE TABLE IF NOT EXISTS history (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            period_end INTEGER NOT NULL,
            points INTEGER NOT NULL,
            messages INTEGER NOT NULL DEFAULT 0,
            rank INTEGER NOT NULL,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promoted_winners (
            chat_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            promoted_at INTEGER NOT NULL
        );
        """)
        # Backwards-compatible migrations for databases created by earlier versions.
        settings_cols = {row[1] for row in con.execute("PRAGMA table_info(settings)")}
        if "min_messages" not in settings_cols:
            con.execute("ALTER TABLE settings ADD COLUMN min_messages INTEGER NOT NULL DEFAULT 500")
        score_cols = {row[1] for row in con.execute("PRAGMA table_info(scores)")}
        if "messages" not in score_cols:
            con.execute("ALTER TABLE scores ADD COLUMN messages INTEGER NOT NULL DEFAULT 0")
        history_cols = {row[1] for row in con.execute("PRAGMA table_info(history)")}
        if "messages" not in history_cols:
            con.execute("ALTER TABLE history ADD COLUMN messages INTEGER NOT NULL DEFAULT 0")
    log.info("Database initialized at %s", DB_PATH)

def ensure_settings(chat_id):
    now = now_ts()
    with connect() as con:
        row = con.execute("SELECT chat_id FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            con.execute(
                "INSERT INTO settings(chat_id, competition_start, competition_end) VALUES(?,?,?)",
                (chat_id, now, now + 7 * 86400),
            )

def get_settings(chat_id):
    ensure_settings(chat_id)
    with connect() as con:
        return con.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()

def is_group(update):
    chat = update.effective_chat
    return bool(chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))

async def is_group_creator(update, context):
    """Owner_ID may be configured as an additional trusted owner; otherwise Telegram creator only."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if OWNER_ID is not None and user.id == OWNER_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status == "creator"
    except TelegramError:
        return False

async def owner_only(update, context):
    if not is_group(update):
        await update.effective_message.reply_text("⚠️ Ye command group mein use karo.")
        return False
    if not await is_group_creator(update, context):
        await update.effective_message.reply_text(
            "🔒 Ye setting sirf group owner ke liye hai. Normal admins isse change nahi kar sakte."
        )
        return False
    return True

async def user_is_protected(chat_id, user_id):
    with connect() as con:
        return con.execute(
            "SELECT 1 FROM protected WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone() is not None

def clean_name(user):
    name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    return name or (f"@{user.username}" if user.username else str(user.id))

def get_score_rows(chat_id, limit=10):
    with connect() as con:
        return con.execute(
            "SELECT user_id,name,points,messages,wins,best_rank,last_active FROM scores "
            "WHERE chat_id=? ORDER BY messages DESC, wins DESC, name COLLATE NOCASE ASC LIMIT ?",
            (chat_id, limit),
        ).fetchall()

def current_rank(chat_id, user_id):
    rows = get_score_rows(chat_id, 100000)
    for i, row in enumerate(rows, 1):
        if row["user_id"] == user_id:
            return i, row
    return None, None

def progress_bar(points, leader_points):
    if leader_points <= 0:
        return "░░░░░░░░░░"
    filled = max(0, min(10, int(points / leader_points * 10)))
    return "█" * filled + "░" * (10 - filled)

def signature(text):
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👑 <b>ADMIN KING MANAGER</b> 👑\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔥 <b>GROUP KA CROWN, ACTIVITY SE JEETO!</b> 🔥\n\n"
        "📩 Har eligible, unique text message count hoga.\n"
        "🏆 Round ke end par sabse zyada messages wala eligible member King banega.\n"
        "🎯 Minimum message target owner set karega — default <b>500 messages</b>.\n"
        "👑 Winner ko admin banana optional hai; owner ise ON/OFF kar sakta hai.\n\n"
        "<b>PLAYER COMMANDS</b>\n"
        "📊 /rank — apni rank aur leader se gap\n"
        "🏆 /leaderboard — top 10 players\n"
        "🪪 /profile — apna record aur wins\n"
        "⚔️ /challenge — current King ko challenge\n"
        "📜 /rules — competition rules\n\n"
        "⚙️ Group owner: /setup\n"
        "🛡️ Bot ko group mein admin banao aur privacy mode OFF rakho, taaki messages count ho sakein.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "😎 <i>Spam nahi, natural chat karo. Crown tumhara bhi ho sakta hai!</i>"
    )
    await update.effective_message.reply_text(msg, parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👑 <b>ADMIN KING MANAGER — COMMAND CENTER</b>\n\n"
        "<b>🎮 Members</b>\n"
        "/rank — apni rank aur leader se gap\n"
        "/leaderboard — current round ke top 10\n"
        "/profile — messages, wins aur title\n"
        "/challenge — current leader ko friendly challenge\n"
        "/rules — current competition rules\n\n"
        "<b>⚙️ Group owner only</b>\n"
        "/setup — settings dekho\n"
        "/setduration 7 — round 1–30 days\n"
        "/setscore 500 — minimum eligible messages\n"
        "/winneradmin on — winner ko limited admin banana\n"
        "/rotation on — bot-promoted King ko next round mein rotate karna\n"
        "/encouragement on|off — close race alerts\n"
        "/comeback on|off — comeback alerts\n"
        "/protect — reply karke member/admin protect karo\n"
        "/unprotect — protection hatao\n"
        "/protected — protected list\n\n"
        "📌 <b>Winner rule:</b> round ke end par minimum target poora karne walon mein sabse zyada eligible messages wala member winner hoga. Target kisi ne poora nahi kiya toh koi winner nahi.\n"
        "🧹 Same text ko 24 ghante ke andar dobara bhejne par count nahi milega; commands aur 3 characters se chhote messages bhi count nahi hote."
    )
    await update.effective_message.reply_text(msg, parse_mode="HTML")

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("⚠️ /setup group mein use karo.")
        return
    s = get_settings(update.effective_chat.id)
    end = datetime.fromtimestamp(s["competition_end"], TZ).strftime("%d %b %Y, %I:%M %p")
    with connect() as con:
        protected_count = con.execute(
            "SELECT COUNT(*) FROM protected WHERE chat_id=?", (update.effective_chat.id,)
        ).fetchone()[0]
    text = (
        "👑 <b>ADMIN KING MANAGER — CONTROL ROOM</b> 👑\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📩 Minimum messages: <b>{s['min_messages']}</b>\n"
        f"⏳ Round duration: <b>{s['duration_days']} day(s)</b>\n"
        f"👑 Winner auto-admin: <b>{'ON ✅' if s['winner_admin'] else 'OFF ⛔'}</b>\n"
        f"🔄 Admin rotation: <b>{'ON ✅' if s['rotation_enabled'] else 'OFF ⛔'}</b>\n"
        f"🔔 Close-race alerts: <b>{'ON' if s['encouragement'] else 'OFF'}</b>\n"
        f"📈 Comeback alerts: <b>{'ON' if s['comeback_alerts'] else 'OFF'}</b>\n"
        f"🛡️ Protected members: <b>{protected_count}</b>\n"
        f"📅 Round ends: <b>{end}</b>\n\n"
        "<b>QUICK SETTINGS</b>\n"
        "🎯 <code>/setscore 500</code> — minimum message target\n"
        "⏳ <code>/setduration 7</code> — start a fresh 7-day round\n"
        "👑 <code>/winneradmin on</code> — winner ko limited admin rights\n"
        "🔄 <code>/rotation on</code> — next round mein bot-promoted admin rotate\n"
        "🔔 <code>/encouragement off</code> / <code>/comeback off</code>\n\n"
        "⚠️ Bot ko admin banao. Auto-admin ke liye uske paas <b>Add New Admins</b> permission honi chahiye.\n"
        "🛡️ Protect/unprotect karne ke liye member ke message par reply karo.\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")

async def setduration(update, context):
    if not await owner_only(update, context): return
    if len(context.args) != 1 or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 30:
        await update.effective_message.reply_text("Use: /setduration 1-30\nExample: /setduration 7")
        return
    days = int(context.args[0]); chat_id = update.effective_chat.id; now = now_ts()
    with connect() as con:
        con.execute(
            "UPDATE settings SET duration_days=?, competition_start=?, competition_end=? WHERE chat_id=?",
            (days, now, now + days * 86400, chat_id),
        )
        # New duration starts a fresh round to avoid mixing periods.
        con.execute("DELETE FROM scores WHERE chat_id=?", (chat_id,))
        con.execute("DELETE FROM seen_messages WHERE chat_id=?", (chat_id,))
    await update.effective_message.reply_text(
        f"⏳ Done! Competition ab <b>{days} din</b> ka hai.\nNaya round abhi start ho gaya! 🔥",
        parse_mode="HTML"
    )

async def setscore(update, context):
    if not await owner_only(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 100000:
        await update.effective_message.reply_text(
            "Use: /setscore 1-100000\nExample: /setscore 500"
        )
        return
    minimum = int(context.args[0])
    with connect() as con:
        con.execute(
            "UPDATE settings SET min_messages=? WHERE chat_id=?",
            (minimum, update.effective_chat.id),
        )
    await update.effective_message.reply_text(
        f"🎯 Minimum winner target set: <b>{minimum} eligible messages</b>.\n"
        "Ab round ke end par itne messages se kam wale winner nahi ban sakte. 👑",
        parse_mode="HTML",
    )

async def setbonus(update, context):
    if not await owner_only(update, context): return
    if len(context.args) != 1 or not context.args[0].isdigit() or not 0 <= int(context.args[0]) <= 500:
        await update.effective_message.reply_text("Use: /setbonus 0-500")
        return
    with connect() as con:
        con.execute("UPDATE settings SET winner_bonus=? WHERE chat_id=?",
                    (int(context.args[0]), update.effective_chat.id))
    await update.effective_message.reply_text(f"🎁 Winner bonus set: {int(context.args[0])} points.")

async def setdailycap(update, context):
    if not await owner_only(update, context): return
    if len(context.args) != 1 or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 10000:
        await update.effective_message.reply_text("Use: /setdailycap 1-10000")
        return
    cap = int(context.args[0])
    with connect() as con:
        con.execute("UPDATE settings SET max_points_per_day=? WHERE chat_id=?",
                    (cap, update.effective_chat.id))
    await update.effective_message.reply_text(f"🧢 Daily cap set to {cap} points.")

async def toggle_setting(update, context, column, label):
    if not await owner_only(update, context): return
    if len(context.args) != 1 or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text(f"Use: /{update.effective_message.text.split()[0][1:]} on|off")
        return
    value = 1 if context.args[0].lower() == "on" else 0
    with connect() as con:
        con.execute(f"UPDATE settings SET {column}=? WHERE chat_id=?", (value, update.effective_chat.id))
    await update.effective_message.reply_text(f"{label}: {'ON ✅' if value else 'OFF ⛔'}")

async def encouragement(update, context):
    await toggle_setting(update, context, "encouragement", "Almost-there alerts")

async def comeback(update, context):
    await toggle_setting(update, context, "comeback_alerts", "Comeback alerts")

async def winneradmin(update, context):
    await toggle_setting(update, context, "winner_admin", "Winner admin promotion")

async def rotation(update, context):
    await toggle_setting(update, context, "rotation_enabled", "Automatic admin rotation")

async def protect(update, context):
    if not await owner_only(update, context): return
    msg = update.effective_message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("🛡️ Kisi admin/member ke message par reply karke /protect bhejo.")
        return
    target = msg.reply_to_message.from_user
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO protected(chat_id,user_id,name,added_by,added_at) VALUES(?,?,?,?,?)",
            (update.effective_chat.id, target.id, clean_name(target), update.effective_user.id, now_ts()),
        )
    await msg.reply_text(f"🛡️ <b>{html.escape(clean_name(target))}</b> protected hai.\nBot use automatically demote nahi karega.", parse_mode="HTML")

async def unprotect(update, context):
    if not await owner_only(update, context): return
    msg = update.effective_message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("Reply to that member's message with /unprotect.")
        return
    target = msg.reply_to_message.from_user
    with connect() as con:
        con.execute("DELETE FROM protected WHERE chat_id=? AND user_id=?",
                    (update.effective_chat.id, target.id))
    await msg.reply_text(f"Protection removed for {html.escape(clean_name(target))}.", parse_mode="HTML")

async def protected_list(update, context):
    if not await owner_only(update, context): return
    with connect() as con:
        rows = con.execute("SELECT user_id,name FROM protected WHERE chat_id=? ORDER BY name",
                           (update.effective_chat.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("🛡️ Abhi protected list khaali hai.")
        return
    text = "🛡️ <b>PROTECTED MEMBERS</b>\n\n" + "\n".join(
        f"• {html.escape(r['name'])} — <code>{r['user_id']}</code>" for r in rows
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("Group mein use karo.")
        return
    s = get_settings(update.effective_chat.id)
    end = datetime.fromtimestamp(s["competition_end"], TZ).strftime("%d %b, %I:%M %p")
    await update.effective_message.reply_text(
        "📜 <b>ADMIN KING — RULEBOOK</b> 📜\n\n"
        f"🎯 Minimum target: <b>{s['min_messages']} eligible messages</b>\n"
        f"⏳ Round length: <b>{s['duration_days']} days</b>\n"
        f"📅 Round ends: <b>{end}</b>\n\n"
        "🏆 Winner: minimum target poora karne walon mein sabse zyada messages wala member.\n"
        "🚫 Same text 24 hours mein repeat hua toh dobara count nahi hoga.\n"
        "🚫 Commands aur 3 characters se chhote messages count nahi honge.\n"
        "👑 Winner ko admin banana group owner ke toggle par depend karta hai.\n"
        "😎 Fair play — natural chat karo, spam mat karo!",
        parse_mode="HTML",
    )

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("⚠️ Group mein use karo.")
        return
    chat_id = update.effective_chat.id
    settings = get_settings(chat_id)
    rows = get_score_rows(chat_id, 10)
    if not rows or all(r["messages"] == 0 for r in rows):
        await update.effective_message.reply_text(
            "🏆 Abhi leaderboard khaali hai! Natural chat shuru karo aur crown ke liye race lagao. 👑"
        )
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        "👑 <b>ADMIN KING LEADERBOARD</b> 👑",
        "━━━━━━━━━━━━━━━━━━",
        f"🎯 Winner target: {settings['min_messages']} messages",
        f"⏳ {settings['duration_days']}-day round",
        "",
    ]
    for i, row in enumerate(rows, 1):
        marker = medals[i-1] if i <= 3 else f"{i}️⃣"
        eligible = " ✅" if row["messages"] >= settings["min_messages"] else ""
        lines.append(f"{marker} <b>{html.escape(row['name'])}</b> — {row['messages']} msgs{eligible}")
    lines += ["", f"🏁 Ends: {datetime.fromtimestamp(settings['competition_end'], TZ).strftime('%d %b %I:%M %p')}",
              "✅ = minimum target complete", "━━━━━━━━━━━━━━━━━━"]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

async def rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("⚠️ Group mein use karo.")
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    settings = get_settings(chat_id)
    rank_no, row = current_rank(chat_id, user.id)
    if not row:
        await update.effective_message.reply_text(
            "👀 Abhi tumhara message count 0 hai. Normal chat karo — race abhi baaki hai! 😎"
        )
        return
    rows = get_score_rows(chat_id, 100000)
    leader_messages = rows[0]["messages"] if rows else 0
    gap = max(0, leader_messages - row["messages"])
    target_left = max(0, settings["min_messages"] - row["messages"])
    out = (
        f"👤 <b>{html.escape(clean_name(user))}</b>\n"
        f"🏅 Rank: <b>#{rank_no}</b>\n"
        f"📩 Eligible messages: <b>{row['messages']}</b>\n"
        f"🎯 Target remaining: <b>{target_left}</b>\n"
        f"🏆 Wins: <b>{row['wins']}</b>\n"
        f"📊 {progress_bar(row['messages'], max(1, leader_messages))}\n"
    )
    if rank_no == 1:
        out += "\n👑 Abhi tum leaderboard ke King ho! Crown sambhal ke. 😂"
    else:
        out += f"\n🔥 Leader se {gap} messages ka gap hai. Comeback possible!"
    await update.effective_message.reply_text(out, parse_mode="HTML")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("⚠️ Group mein use karo.")
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    rank_no, row = current_rank(chat_id, user.id)
    with connect() as con:
        wins = con.execute(
            "SELECT COUNT(*) FROM history WHERE chat_id=? AND user_id=? AND rank=1",
            (chat_id, user.id),
        ).fetchone()[0]
    title = "🐣 Rookie"
    if wins >= 5:
        title = "💎 Admin King Legend"
    elif wins >= 2:
        title = "🏆 Crown Champion"
    elif wins == 1:
        title = "👑 Former King"
    elif row and row["messages"] >= 100:
        title = "🔥 Active Player"
    await update.effective_message.reply_text(
        f"🪪 <b>PLAYER PROFILE</b>\n\n"
        f"👤 Name: <b>{html.escape(clean_name(user))}</b>\n"
        f"🎖️ Title: {title}\n"
        f"🏅 Current rank: <b>#{rank_no if rank_no else '—'}</b>\n"
        f"📩 Current messages: <b>{row['messages'] if row else 0}</b>\n"
        f"🏆 Round wins: <b>{wins}</b>",
        parse_mode="HTML",
    )

async def challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("⚠️ Group mein use karo.")
        return
    rows = get_score_rows(update.effective_chat.id, 1)
    if not rows:
        await update.effective_message.reply_text("👑 Abhi King decide nahi hua. Pehle race shuru karo!")
        return
    king = rows[0]
    await update.effective_message.reply_text(
        "⚔️ <b>KING CHALLENGE!</b> ⚔️\n\n"
        f"👑 Current Leader: <b>{html.escape(king['name'])}</b>\n"
        f"📩 Leader messages: <b>{king['messages']}</b>\n"
        f"🔥 Challenger: <b>{html.escape(clean_name(update.effective_user))}</b>\n\n"
        "Chat naturally, climb the leaderboard, and claim the crown! 😈",
        parse_mode="HTML",
    )

async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Count eligible unique messages; repeated same text is ignored for 24 hours."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    text = (msg.text or msg.caption or "").strip()
    if not text or text.startswith("/") or len(text) < 3:
        return

    current = now_ts()
    sig = signature(text)
    with connect() as con:
        con.execute("DELETE FROM seen_messages WHERE seen_at < ?", (current - 86400,))
        duplicate = con.execute(
            "SELECT 1 FROM seen_messages WHERE chat_id=? AND user_id=? AND signature=? AND seen_at>?",
            (chat.id, user.id, sig, current - 86400),
        ).fetchone()
        if duplicate:
            return
        con.execute(
            "INSERT INTO seen_messages(chat_id,user_id,signature,seen_at) VALUES(?,?,?,?)",
            (chat.id, user.id, sig, current),
        )
        con.execute(
            "INSERT OR IGNORE INTO scores(chat_id,user_id,name,points,messages,wins,best_rank,last_active) "
            "VALUES(?,?,?,0,0,0,0,?)",
            (chat.id, user.id, clean_name(user), current),
        )
        con.execute(
            "UPDATE scores SET name=?,last_active=?,messages=messages+1,points=points+1 "
            "WHERE chat_id=? AND user_id=?",
            (clean_name(user), current, chat.id, user.id),
        )
        settings = con.execute("SELECT * FROM settings WHERE chat_id=?", (chat.id,)).fetchone()

    # Optional, low-frequency nudge when the top two are close.
    if settings and settings["encouragement"]:
        last_alert_key = f"last_gap_alert_{chat.id}"
        last_alert = context.application.bot_data.get(last_alert_key, 0)
        if current - last_alert > 6 * 3600:
            rows = get_score_rows(chat.id, 2)
            if len(rows) >= 2:
                gap = rows[0]["messages"] - rows[1]["messages"]
                if 0 < gap <= 5:
                    context.application.bot_data[last_alert_key] = current
                    try:
                        await msg.reply_text(
                            f"👀 <b>RACE ALERT!</b>\n"
                            f"👑 {html.escape(rows[0]['name'])} bas {gap} message(s) aage hain!\n"
                            "🔥 Crown ke liye race tight hai! 😂",
                            parse_mode="HTML",
                        )
                    except TelegramError:
                        pass

async def check_competitions(context: ContextTypes.DEFAULT_TYPE):
    """Close expired rounds, choose one message-count winner, and optionally rotate bot-promoted admins."""
    with connect() as con:
        chats = con.execute(
            "SELECT chat_id FROM settings WHERE competition_end<=?", (now_ts(),)
        ).fetchall()

    for chatrow in chats:
        chat_id = chatrow["chat_id"]
        settings = get_settings(chat_id)
        end = settings["competition_end"]
        with connect() as con:
            already_done = con.execute(
                "SELECT 1 FROM announcements WHERE chat_id=? AND competition_end=?", (chat_id, end)
            ).fetchone()
            if already_done:
                continue
            all_rows = con.execute(
                "SELECT user_id,name,points,messages,wins FROM scores WHERE chat_id=? "
                "ORDER BY messages DESC,wins DESC,name COLLATE NOCASE ASC",
                (chat_id,),
            ).fetchall()
            con.execute(
                "INSERT INTO announcements(chat_id,competition_end) VALUES(?,?)", (chat_id, end)
            )
            for i, row in enumerate(all_rows, 1):
                con.execute(
                    "INSERT INTO history(chat_id,user_id,period_end,points,messages,rank,name) VALUES(?,?,?,?,?,?,?)",
                    (chat_id, row["user_id"], end, row["points"], row["messages"], i, row["name"]),
                )

            min_messages = settings["min_messages"]
            eligible = next((r for r in all_rows if r["messages"] >= min_messages), None)
            if eligible:
                winner_id = eligible["user_id"]
                winner_name = eligible["name"]
                winner_messages = eligible["messages"]
                con.execute("UPDATE scores SET wins=wins+1 WHERE chat_id=? AND user_id=?", (chat_id, winner_id))
            else:
                winner_id, winner_name, winner_messages = None, "No winner", 0

            days = settings["duration_days"]
            new_start = max(end, now_ts())
            new_end = new_start + days * 86400
            con.execute(
                "UPDATE settings SET competition_start=?,competition_end=? WHERE chat_id=?",
                (new_start, new_end, chat_id),
            )
            con.execute("DELETE FROM scores WHERE chat_id=?", (chat_id,))
            con.execute("DELETE FROM seen_messages WHERE chat_id=?", (chat_id,))

        if not winner_id:
            announcement = (
                "⏰ <b>ROUND OVER!</b> ⏰\n\n"
                f"Is round mein kisi ne minimum <b>{min_messages} eligible messages</b> ka target complete nahi kiya.\n"
                "👑 Is baar koi winner nahi — agle round mein try karo!\n\n"
                f"🔥 Naya {days}-day round shuru ho gaya."
            )
        else:
            announcement = (
                "🏆━━━━━━━━━━━━━━━━━━🏆\n\n"
                "🎉 <b>THE NEW ADMIN KING!</b> 🎉\n\n"
                f"👑 <b>{html.escape(winner_name)}</b> ne crown jeet liya!\n"
                f"📩 Winning count: <b>{winner_messages} eligible messages</b>\n"
                f"🎯 Minimum target: <b>{min_messages}</b> — COMPLETE ✅\n\n"
                "🥈 Baaki players, agla crown tumhara ho sakta hai! 😈\n"
                "🏆━━━━━━━━━━━━━━━━━━🏆\n"
                f"⏳ Naya {days}-day round ab shuru ho gaya!"
            )
        try:
            await context.bot.send_message(chat_id=chat_id, text=announcement, parse_mode="HTML")
        except TelegramError as exc:
            log.warning("Could not announce for %s: %s", chat_id, exc)

        # Demote only the previous winner that this bot itself promoted.
        rotation_blocked = False
        with connect() as con:
            previous = con.execute(
                "SELECT user_id,name FROM promoted_winners WHERE chat_id=?", (chat_id,)
            ).fetchone()

        if previous:
            previous_id, previous_name = previous["user_id"], previous["name"]
            if await user_is_protected(chat_id, previous_id):
                rotation_blocked = True
                try:
                    await context.bot.send_message(
                        chat_id,
                        f"🛡️ {html.escape(previous_name)} protected hain; bot unhe demote nahi karega. Safety ke liye naye winner ko admin nahi banaya.",
                        parse_mode="HTML",
                    )
                except TelegramError:
                    pass
            else:
                try:
                    await context.bot.promote_chat_member(
                        chat_id=chat_id, user_id=previous_id,
                        can_manage_chat=False, can_delete_messages=False,
                        can_manage_video_chats=False, can_restrict_members=False,
                        can_promote_members=False, can_change_info=False,
                        can_invite_users=False, can_pin_messages=False,
                        can_post_messages=False, can_edit_messages=False,
                        can_post_stories=False, can_edit_stories=False,
                        can_delete_stories=False, can_manage_topics=False,
                    )
                    with connect() as con:
                        con.execute("DELETE FROM promoted_winners WHERE chat_id=?", (chat_id,))
                except TelegramError as exc:
                    rotation_blocked = True
                    log.warning("Could not demote previous winner in %s: %s", chat_id, exc)
                    try:
                        await context.bot.send_message(
                            chat_id,
                            "⚠️ Pichhle bot-promoted winner ko demote nahi kar paya. Safety ke liye naye winner ko admin nahi banaya; bot permissions check karo.",
                        )
                    except TelegramError:
                        pass

        if settings["winner_admin"] and winner_id and not rotation_blocked:
            try:
                if await user_is_protected(chat_id, winner_id):
                    await context.bot.send_message(
                        chat_id, f"🛡️ {html.escape(winner_name)} protected hain; auto-promotion skip hui.", parse_mode="HTML"
                    )
                else:
                    member = await context.bot.get_chat_member(chat_id, winner_id)
                    if member.status in ("administrator", "creator"):
                        await context.bot.send_message(
                            chat_id,
                            f"⚠️ {html.escape(winner_name)} pehle se admin/owner hain. Existing permissions change nahi kiye.",
                            parse_mode="HTML",
                        )
                    else:
                        await context.bot.promote_chat_member(
                            chat_id=chat_id, user_id=winner_id,
                            can_manage_chat=True, can_delete_messages=False,
                            can_manage_video_chats=False, can_restrict_members=False,
                            can_promote_members=False, can_change_info=False,
                            can_invite_users=False, can_pin_messages=False,
                            can_post_messages=False, can_edit_messages=False,
                            can_post_stories=False, can_edit_stories=False,
                            can_delete_stories=False, can_manage_topics=False,
                        )
                        with connect() as con:
                            con.execute(
                                "INSERT OR REPLACE INTO promoted_winners(chat_id,user_id,name,promoted_at) VALUES(?,?,?,?)",
                                (chat_id, winner_id, winner_name, now_ts()),
                            )
                        await context.bot.send_message(
                            chat_id,
                            f"👑 <b>{html.escape(winner_name)}</b> ko limited admin access diya gaya!\n"
                            "🔒 Ban members, delete messages, pin messages aur add admins permissions OFF hain.",
                            parse_mode="HTML",
                        )
            except TelegramError as exc:
                log.warning("Winner promotion failed in %s: %s", chat_id, exc)
                try:
                    await context.bot.send_message(
                        chat_id, "⚠️ Winner ko admin nahi bana saka. Bot ke admin permissions check karo."
                    )
                except TelegramError:
                    pass

async def post_init(application: Application):
    log.info("Starting Telegram bot connection check...")
    await application.bot.delete_webhook(drop_pending_updates=False)
    me = await application.bot.get_me()
    log.info("Telegram connected as @%s (id=%s)", me.username, me.id)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled bot error: %r", context.error)

def main():
    init_db()
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN missing. Add it in Render → Environment.")
    Thread(target=web_server, daemon=True, name="health-server").start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setup", setup))
    app.add_handler(CommandHandler("setduration", setduration))
    app.add_handler(CommandHandler("setscore", setscore))
    app.add_handler(CommandHandler("setbonus", setbonus))
    app.add_handler(CommandHandler("setdailycap", setdailycap))
    app.add_handler(CommandHandler("encouragement", encouragement))
    app.add_handler(CommandHandler("comeback", comeback))
    app.add_handler(CommandHandler("winneradmin", winneradmin))
    app.add_handler(CommandHandler("rotation", rotation))
    app.add_handler(CommandHandler("protect", protect))
    app.add_handler(CommandHandler("unprotect", unprotect))
    app.add_handler(CommandHandler("protected", protected_list))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("rank", rank))
    app.add_handler(CommandHandler("myrank", rank))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("challenge", challenge))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_message))
    app.add_error_handler(error_handler)

    # JobQueue comes with python-telegram-bot[job-queue].
    app.job_queue.run_repeating(check_competitions, interval=60, first=15, name="competition-check")
    log.info("Admin King Manager started.")
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
