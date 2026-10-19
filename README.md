# Admin King Manager — replacement package

## Files
- `bot.py` — complete bot code
- `requirements.txt` — Python dependencies

## Render settings
- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Environment variable: `BOT_TOKEN` = token from @BotFather
- Optional: `OWNER_ID` = your Telegram numeric user ID (trusted settings owner)
- Optional: `DB_PATH` = database path, default `admin_king_manager.db`

## Setup
1. Stop the old Render service before starting this version, so two polling processes do not fight.
2. Back up your old code and database first.
3. Replace `bot.py` and `requirements.txt` in GitHub with these files.
4. Deploy on Render and check Logs for “Telegram connected”.
5. Add the bot to the target group and make it an administrator.
6. Run `/setup` in the group.

## Main commands
- `/setup`
- `/setduration 1` to `/setduration 30` (starts a fresh round and clears current-round scores)
- `/setscore 2`
- `/setbonus 20`
- `/setdailycap 50`
- `/encouragement on` or `off`
- `/comeback on` or `off`
- `/winneradmin on` or `off` (OFF by default)
- `/rotation on` or `off` (OFF by default)
- Reply to a member's message with `/protect` or `/unprotect`
- `/protected`
- Members: `/rank`, `/leaderboard`, `/profile`, `/challenge`, `/rules`

## Security notes
- Only the Telegram group creator (or `OWNER_ID`, if configured) can change settings.
- Protected users are skipped by automated winner promotion/demotion logic.
- Winner admin promotion is OFF by default and gives only limited permissions when enabled.
- The bot does not automatically demote existing admins in this version; `rotation` is a reserved setting for the future. This avoids accidentally removing existing admins.
- For tracking ordinary group messages, disable privacy mode in @BotFather or keep the bot as an administrator.
- The bot cannot reliably infer whether a message is “meaningful”; repeated exact/normalized messages are blocked for scoring for 24 hours.
- Render's local filesystem may be ephemeral. Use a persistent disk if you need the SQLite database to survive redeploys/replacements.
