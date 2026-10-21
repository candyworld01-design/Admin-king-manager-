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
            max_points_per_day INTEGER NOT NULL DEFAULT 50
        );
        CREATE TABLE IF NOT EXISTS scores (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
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
        # One-week default competition for each group is created when first used.
        # No destructive schema migration is needed for a fresh install.
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
            "SELECT user_id,name,points,wins,best_rank,last_active FROM scores "
            "WHERE chat_id=? ORDER BY points DESC, wins DESC, name COLLATE NOCASE ASC LIMIT ?",
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
        "👑 <b>ADMIN KING MANAGER</b>\n\n"
        "Group activity ko fun competition banao — bina daily tasks ke. 😎\n\n"
        "📊 /rank — apni rank\n"
        "🏆 /leaderboard — top members\n"
        "👤 /profile — apna profile\n"
        "⚙️ /setup — owner settings\n"
        "🛡️ /protect — kisi admin ko protect (owner only; reply karke use karo)\n\n"
        "Bot ko group mein add karo, admin banao, phir /setup se settings chalao."
    )
    await update.effective_message.reply_text(msg, parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👑 <b>ADMIN KING MANAGER — COMMANDS</b>\n\n"
        "Members:\n"
        "/rank — apni rank aur leader se gap\n"
        "/leaderboard — current competition top 10\n"
        "/profile — points, wins, title\n"
        "/challenge — current leader ko friendly challenge\n"
        "/rules — scoring aur current duration\n\n"
        "Group owner only:\n"
        "/setup\n"
        "/setduration 1-30\n"
        "/setscore 1-10000\n"
        "/setbonus 0-500\n"
        "/setdailycap 1-10000\n"
        "/encouragement on|off\n"
        "/comeback on|off\n"
        "/winneradmin on|off\n"
        "/rotation on|off\n"
        "/protect (reply to member/admin message)\n"
        "/unprotect (reply to member/admin message)\n"
        "/protected\n\n"
        "Auto-admin: only the top scorer with at least 100 points can be considered; rights are limited.\n"
        "Scoring is based on unique, non-command text messages. Repeated messages are limited."
    )
    await update.effective_message.reply_text(msg, parse_mode="HTML")

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("⚠️ /setup group mein use karo.")
        return
    s = get_settings(update.effective_chat.id)
    end = datetime.fromtimestamp(s["competition_end"], TZ).strftime("%d %b %Y, %I:%M %p")
    protected_count = 0
    with connect() as con:
        protected_count = con.execute(
            "SELECT COUNT(*) FROM protected WHERE chat_id=?", (update.effective_chat.id,)
        ).fetchone()[0]
    text = (
        "👑 <b>ADMIN KING MANAGER — SETUP</b>\n\n"
        f"⏳ Duration: <b>{s['duration_days']} day(s)</b>\n"
        f"⭐ Points per unique message: <b>{s['points_per_message']}</b>\n"
        f"🎁 Winner bonus: <b>{s['winner_bonus']}</b>\n"
        f"🧢 Daily point cap: <b>{s['max_points_per_day']}</b>\n"
        f"🔔 Almost-there alerts: <b>{'ON' if s['encouragement'] else 'OFF'}</b>\n"
        f"📈 Comeback alerts: <b>{'ON' if s['comeback_alerts'] else 'OFF'}</b>\n"
        f"👑 Winner admin promotion: <b>{'ON' if s['winner_admin'] else 'OFF'}</b>\n"
        f"🔄 Admin rotation: <b>{'ON' if s['rotation_enabled'] else 'OFF'}</b>\n"
        f"🛡️ Protected admins: <b>{protected_count}</b>\n"
        f"📅 Competition ends: <b>{end}</b>\n\n"
        "Commands:\n"
        "<code>/setduration 1</code> to <code>/setduration 30</code>\n"
        "<code>/setscore 500</code> (1-10000) • <code>/setbonus 25</code>\n"
        "<code>/setdailycap 10000</code> (max 10000)\n"
        "<code>/encouragement on</code> • <code>/comeback off</code>\n"
        "<code>/winneradmin off</code> • <code>/rotation off</code>\n"
        "👑 Auto-admin: top scorer ko minimum 100 points chahiye; bot sirf limited rights dega.\n\n"
        "🛡️ Protect/unprotect: reply to that person's message with /protect or /unprotect."
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
    if not await owner_only(update, context): return
    if len(context.args) != 1 or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 10000:
        await update.effective_message.reply_text("Use: /setscore 1-10000\nExample: /setscore 500")
        return
    score = int(context.args[0])
    with connect() as con:
        con.execute("UPDATE settings SET points_per_message=? WHERE chat_id=?",
                    (score, update.effective_chat.id))
    await update.effective_message.reply_text(
        f"⭐ Har eligible unique message par {score} point(s). Daily cap alag se /setdailycap se set hota hai."
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

async def rules(update, context):
    if not is_group(update):
        await update.effective_message.reply_text("Group mein use karo.")
        return
    s = get_settings(update.effective_chat.id)
    end = datetime.fromtimestamp(s["competition_end"], TZ).strftime("%d %b, %I:%M %p")
    await update.effective_message.reply_text(
        f"📜 Competition: {s['duration_days']} day(s)\n"
        f"⭐ Unique eligible message = {s['points_per_message']} point(s)\n"
        f"🧢 Daily cap = {s['max_points_per_day']}\n"
        f"⏰ Ends: {end}\n"
        "Repeated text and commands don't earn points. Daily missions nahi hain. 😎"
    )

async def leaderboard(update, context):
    if not is_group(update):
        await update.effective_message.reply_text("Group mein use karo.")
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    rows = get_score_rows(chat_id, 10)
    if not rows or all(r["points"] == 0 for r in rows):
        await update.effective_message.reply_text("🏆 Abhi scores nahi bane. Chat naturally karo—competition shuru hai! 😎")
        return
    leader = max(1, rows[0]["points"])
    medals = ["🥇", "🥈", "🥉"]
    lines = ["👑 <b>ADMIN KING LEADERBOARD</b>", f"⏳ {s['duration_days']}-day competition", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{medals[i-1] if i <= 3 else f'{i}.'} <b>{html.escape(r['name'])}</b> — {r['points']} pts")
    lines += ["", f"🏁 End: {datetime.fromtimestamp(s['competition_end'], TZ).strftime('%d %b %I:%M %p')}"]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

async def rank(update, context):
    if not is_group(update):
        await update.effective_message.reply_text("Group mein use karo.")
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    s = get_settings(chat_id)
    rnk, row = current_rank(chat_id, user.id)
    if not row:
        await update.effective_message.reply_text("Abhi tumhara score 0 hai. Normal chat karo, points aane lagenge. 😎")
        return
    rows = get_score_rows(chat_id, 100000)
    leader = rows[0]["points"] if rows else 0
    gap = max(0, leader - row["points"])
    out = (
        f"👤 <b>{html.escape(clean_name(user))}</b>\n"
        f"🏅 Rank: <b>#{rnk}</b>\n"
        f"⭐ Score: <b>{row['points']}</b>\n"
        f"🏆 Wins: <b>{row['wins']}</b>\n"
        f"📊 {progress_bar(row['points'], leader)}\n"
    )
    if rnk == 1:
        out += "\n👑 Abhi crown tumhare paas hai! Kursi sambhal ke. 😂"
    else:
        out += f"\n🔥 Leader se {gap} points ka gap. Comeback possible hai!"
    await update.effective_message.reply_text(out, parse_mode="HTML")

async def profile(update, context):
    if not is_group(update):
        await update.effective_message.reply_text("Group mein use karo.")
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    rnk, row = current_rank(chat_id, user.id)
    with connect() as con:
        wins = con.execute("SELECT COUNT(*) FROM history WHERE chat_id=? AND user_id=? AND rank=1",
                           (chat_id, user.id)).fetchone()[0]
    title = "🐣 Rookie"
    if wins >= 5: title = "💎 Legend"
    elif wins >= 2: title = "🏆 Champion"
    elif wins == 1: title = "👑 Winner"
    elif row and row["points"] >= 50: title = "🔥 Consistent Banda"
    await update.effective_message.reply_text(
        f"👤 <b>{html.escape(clean_name(user))}</b>\n"
        f"🎖️ Title: {title}\n"
        f"🏅 Current rank: #{rnk if rnk else '—'}\n"
        f"⭐ Current score: {row['points'] if row else 0}\n"
        f"🏆 Wins: {wins}",
        parse_mode="HTML"
    )

async def challenge(update, context):
    if not is_group(update):
        await update.effective_message.reply_text("Group mein use karo.")
        return
    rows = get_score_rows(update.effective_chat.id, 1)
    if not rows:
        await update.effective_message.reply_text("Abhi King decide nahi hua. Pehle score banao! 👑")
        return
    king = rows[0]
    await update.effective_message.reply_text(
        f"⚔️ <b>KING CHALLENGE!</b>\n\n"
        f"👑 Current King: {html.escape(king['name'])}\n"
        f"🔥 Challenger: {html.escape(clean_name(update.effective_user))}\n\n"
        "Challenge accepted! 😈 Leaderboard mein score se faisla hoga.",
        parse_mode="HTML"
    )

async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    text = (msg.text or msg.caption or "").strip()
    if not text or text.startswith("/"):
        return
    # Ignore very short spam-like messages.
    if len(text) < 3:
        return
    s = get_settings(chat.id)
    current = now_ts()
    sig = signature(text)
    # Same user can't score the same normalized text again within 24 hours.
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
            "INSERT OR IGNORE INTO scores(chat_id,user_id,name,points,wins,best_rank,last_active) VALUES(?,?,?,0,0,0,?)",
            (chat.id, user.id, clean_name(user), current),
        )
        con.execute("UPDATE scores SET name=?,last_active=? WHERE chat_id=? AND user_id=?",
                    (clean_name(user), current, chat.id, user.id))
        # Daily point cap; points earned today are derived from history of recent score updates.
        # A separate counter is kept in SQLite to avoid expensive message-history scans.
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_points (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day_key TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,user_id,day_key)
            )
        """)
        day_key = datetime.fromtimestamp(current, TZ).strftime("%Y-%m-%d")
        drow = con.execute("SELECT points FROM daily_points WHERE chat_id=? AND user_id=? AND day_key=?",
                           (chat.id, user.id, day_key)).fetchone()
        earned = drow["points"] if drow else 0
        add = min(s["points_per_message"], max(0, s["max_points_per_day"] - earned))
        if add <= 0:
            return
        con.execute("UPDATE scores SET points=points+? WHERE chat_id=? AND user_id=?",
                    (add, chat.id, user.id))
        con.execute("""
            INSERT INTO daily_points(chat_id,user_id,day_key,points) VALUES(?,?,?,?)
            ON CONFLICT(chat_id,user_id,day_key) DO UPDATE SET points=points+excluded.points
        """, (chat.id, user.id, day_key, add))
    # Keep encouragement low-noise: only check every 10 points and at most one alert every 6h per chat.
    if s["encouragement"] and add > 0:
        last_alert_key = f"last_gap_alert_{chat.id}"
        last_alert = context.application.bot_data.get(last_alert_key, 0)
        if current - last_alert > 6 * 3600:
            rows = get_score_rows(chat.id, 2)
            if len(rows) >= 2 and rows[0]["user_id"] != user.id:
                gap = rows[0]["points"] - rows[1]["points"]
                if 0 < gap <= 5:
                    context.application.bot_data[last_alert_key] = current
                    try:
                        await msg.reply_text(
                            f"👀 OYE {html.escape(clean_name(rows[1]))} BHAI!\n"
                            f"👑 {html.escape(rows[0]['name'])} tumse sirf {gap} point(s) aage hai!\n"
                            "🔥 Crown chheenna hai toh mauka hai! 😂",
                            parse_mode="HTML",
                        )
                    except TelegramError:
                        pass

async def check_competitions(context: ContextTypes.DEFAULT_TYPE):
    """Finish expired rounds, announce winners, rotate only bot-promoted limited admins."""
    with connect() as con:
        chats = con.execute(
            "SELECT chat_id FROM settings WHERE competition_end<=?", (now_ts(),)
        ).fetchall()

    for chatrow in chats:
        chat_id = chatrow["chat_id"]
        s = get_settings(chat_id)
        end = s["competition_end"]

        with connect() as con:
            done = con.execute(
                "SELECT 1 FROM announcements WHERE chat_id=? AND competition_end=?",
                (chat_id, end),
            ).fetchone()
            if done:
                continue

            rows = con.execute(
                "SELECT user_id,name,points,wins FROM scores WHERE chat_id=? "
                "ORDER BY points DESC,wins DESC,name COLLATE NOCASE LIMIT 10",
                (chat_id,),
            ).fetchall()
            con.execute(
                "INSERT INTO announcements(chat_id,competition_end) VALUES(?,?)",
                (chat_id, end),
            )
            for i, row in enumerate(rows, 1):
                con.execute(
                    "INSERT INTO history(chat_id,user_id,period_end,points,rank,name) VALUES(?,?,?,?,?,?)",
                    (chat_id, row["user_id"], end, row["points"], i, row["name"]),
                )

            if rows and rows[0]["points"] > 0:
                con.execute(
                    "UPDATE scores SET wins=wins+1 WHERE chat_id=? AND user_id=?",
                    (chat_id, rows[0]["user_id"]),
                )
                winner_id = rows[0]["user_id"]
                winner_name = rows[0]["name"]
                winner_points = rows[0]["points"]
            else:
                winner_id, winner_name, winner_points = None, "No winner yet", 0

            days = s["duration_days"]
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
                "⏰ <b>ROUND OVER!</b>\n\nIs round mein score nahi bana. "
                "Agla round ab shuru! 😎"
            )
        else:
            announcement = (
                "🏆━━━━━━━━━━━━━━🏆\n\n"
                "🎉 <b>CONGRATULATIONS!</b> 🎉\n\n"
                f"👑 <b>{html.escape(winner_name)}</b> is period ke VIJETA hain!\n"
                f"⭐ Winning score: <b>{winner_points}</b>\n\n"
                "🥈 Aur baaki legends — agla round tumhara ho sakta hai! 😂\n"
                "🏆━━━━━━━━━━━━━━🏆\n\n"
                f"⏳ Naya {days}-day round ab shuru ho gaya!"
            )
        try:
            await context.bot.send_message(chat_id=chat_id, text=announcement, parse_mode="HTML")
        except TelegramError as exc:
            log.warning("Could not announce for %s: %s", chat_id, exc)

        # First rotate out the previous winner that THIS bot promoted.
        # Never alter protected people or admins appointed outside this bot.
        rotation_blocked = False
        with connect() as con:
            previous = con.execute(
                "SELECT user_id,name FROM promoted_winners WHERE chat_id=?", (chat_id,)
            ).fetchone()

        if previous:
            previous_id = previous["user_id"]
            previous_name = previous["name"]
            if await user_is_protected(chat_id, previous_id):
                rotation_blocked = True
                try:
                    await context.bot.send_message(
                        chat_id,
                        f"🛡️ {html.escape(previous_name)} protected hain, isliye bot unhe demote nahi karega. "
                        "Is round mein kisi naye winner ko admin nahi banaya gaya.",
                        parse_mode="HTML",
                    )
                except TelegramError:
                    pass
            else:
                try:
                    await context.bot.promote_chat_member(
                        chat_id=chat_id,
                        user_id=previous_id,
                        can_manage_chat=False,
                        can_delete_messages=False,
                        can_manage_video_chats=False,
                        can_restrict_members=False,
                        can_promote_members=False,
                        can_change_info=False,
                        can_invite_users=False,
                        can_pin_messages=False,
                        can_post_messages=False,
                        can_edit_messages=False,
                        can_post_stories=False,
                        can_edit_stories=False,
                        can_delete_stories=False,
                        can_manage_topics=False,
                    )
                    with connect() as con:
                        con.execute("DELETE FROM promoted_winners WHERE chat_id=?", (chat_id,))
                except TelegramError as exc:
                    rotation_blocked = True
                    log.warning("Could not demote previous bot-promoted winner in %s: %s", chat_id, exc)
                    try:
                        await context.bot.send_message(
                            chat_id,
                            "⚠️ Pichhle bot-promoted winner ko demote nahi kar paya, isliye safety ke liye "
                            "naye winner ko admin nahi banaya. Group owner bot permissions check kare.",
                        )
                    except TelegramError:
                        pass

        # Only the single top scorer can be promoted, and only with at least 100 points.
        if s["winner_admin"] and winner_id and winner_points >= 100 and not rotation_blocked:
            try:
                if await user_is_protected(chat_id, winner_id):
                    await context.bot.send_message(
                        chat_id,
                        f"🛡️ {html.escape(winner_name)} protected hain; automatic promotion skip ki gayi.",
                        parse_mode="HTML",
                    )
                else:
                    member = await context.bot.get_chat_member(chat_id, winner_id)
                    if member.status in ("administrator", "creator"):
                        # Do not overwrite an admin's existing privileges; those may have been
                        # granted manually or by another bot and are outside this bot's tracking.
                        await context.bot.send_message(
                            chat_id,
                            f"⚠️ {html.escape(winner_name)} pehle se admin/owner hain. Existing rights safe rakhne ke liye "
                            "bot ne unke permissions change nahi kiye. Group owner manually rights check kare.",
                            parse_mode="HTML",
                        )
                    else:
                        await context.bot.promote_chat_member(
                            chat_id=chat_id,
                            user_id=winner_id,
                            can_manage_chat=True,
                            can_delete_messages=False,
                            can_manage_video_chats=False,
                            can_restrict_members=False,
                            can_promote_members=False,
                            can_change_info=False,
                            can_invite_users=False,
                            can_pin_messages=False,
                            can_post_messages=False,
                            can_edit_messages=False,
                            can_post_stories=False,
                            can_edit_stories=False,
                            can_delete_stories=False,
                            can_manage_topics=False,
                        )
                        with connect() as con:
                            con.execute(
                                "INSERT OR REPLACE INTO promoted_winners(chat_id,user_id,name,promoted_at) "
                                "VALUES(?,?,?,?)",
                                (chat_id, winner_id, winner_name, now_ts()),
                            )
                        await context.bot.send_message(
                            chat_id,
                            f"👑 {html.escape(winner_name)} ko limited admin access diya gaya. "
                            "Add admins, ban members, delete messages, pin messages aur settings ke rights OFF hain.",
                            parse_mode="HTML",
                        )
            except TelegramError as exc:
                log.warning("Winner promotion failed in %s: %s", chat_id, exc)
                try:
                    await context.bot.send_message(
                        chat_id,
                        "⚠️ Winner ko limited admin nahi banaya ja saka. Bot ke admin permissions check karo.",
                    )
                except TelegramError:
                    pass
        elif s["winner_admin"] and winner_id and winner_points < 100:
            try:
                await context.bot.send_message(
                    chat_id,
                    f"ℹ️ Winner admin banne ke liye minimum 100 points chahiye. Is baar score {winner_points} tha; promotion nahi hui.",
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
