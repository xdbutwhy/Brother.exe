# Brother.exe — Telegram AI Assistant

A Python Telegram bot that combines AI replies with persistent, per-user notes. Built as a personal learning and portfolio project by **xdbutwhy**.

Despite its name, Brother.exe is a Python application, not a packaged Windows executable. The bot's messages are currently in Russian.

## Features

- AI answers through the OpenRouter chat completions API.
- Personal facts saved with `/remember` and included only in that sender's AI request.
- Owner-only commands for shared facts and a canonical answer.
- Quick local replies to greetings and predefined questions.
- Persistent JSON storage with atomic writes and legacy data migration.
- Friendly error messages for timeouts, unavailable models, and API failures.
- Automated tests with mocked Telegram and HTTP calls.

## How it works

Telegram message → local reply or saved canonical answer → otherwise an OpenRouter request with shared facts and the current user's saved notes → Telegram reply.

Owner permissions are checked in Python before changes to shared memory. Telegram sender IDs identify personal records; the entire user database is never included in an AI request. User messages and saved notes sent to AI are processed by OpenRouter and its model provider. JSON memory is local and unencrypted: protect the project folder and avoid saving sensitive information.

## Run on Windows

You need Python, a Telegram bot token from **BotFather**, and an OpenRouter API key. Development used Python 3.14.8; other versions have not been verified. Direct dependencies are pinned in `requirements.txt`.

Open PowerShell in this project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath token.env)) {
    Copy-Item -LiteralPath token.env.example -Destination token.env
}
notepad token.env
```

Fill in `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY`. Leave `OWNER_ID=0` for the first run; this disables owner commands. Keep this file private.

Start the bot:

```powershell
.\.venv\Scripts\python.exe brother.py
```

Send `/id` to your bot, stop it with **Ctrl+C**, enter your numeric Telegram sender ID as `OWNER_ID` in `token.env`, then start it again. A username or group chat ID does not grant owner access.

You can also double-click `start_brother.bat`. It prefers the project's virtual environment and otherwise uses Python from PATH. Run only one instance at a time.

## Commands

| Command | Purpose | Access |
| --- | --- | --- |
| `/start` | Introduction | Everyone |
| `/help` | Commands and memory explanation | Everyone |
| `/id` | Show your Telegram sender ID | Everyone |
| `/status` | Static status message; no API health check | Everyone |
| `/remember <fact>` | Save a personal fact | Everyone |
| `/remember_global <fact>` | Save a shared fact | Owner |
| `/canon <answer>` | Change the shared canonical answer | Owner |

## Try it

1. Send `/start` and `/help`.
2. Send `/remember My cat is Pixel`, then ask what your cat's name is.
3. Send `Где Кот?` to see the default canonical answer: `In the Laniakea Supercluster.`
4. Try `/canon A fictional location.` as the owner, then repeat the question.
5. From another account, try the owner commands to see the permission checks.

Use fictional facts for demonstrations. No live bot credentials or personal memory are included in this repository.

## Tests

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest -v test_brother
```

Tests use temporary memory files and mocked external services. They cover per-user request isolation, permissions, persistence, migration, canonical answers, commands, and API error handling. They do not verify live provider availability or AI answer quality.

## Project files

| File | Role |
| --- | --- |
| `brother.py` | Bot handlers, AI client, and memory storage |
| `test_brother.py` | Automated tests |
| `requirements.txt` | Direct Python dependencies |
| `token.env.example` | Empty credentials template and owner setting |
| `start_brother.bat` | Windows launcher |

`memory.json` is created automatically beside the script. Credentials, memory, backups, local notes, virtual environments, and generated files are excluded by `.gitignore`. This public copy starts with a clean Git history to exclude private data from earlier local commits.

## Current limitations

- Local demonstration project; no hosted service or 24/7 deployment.
- The selected model is `inclusionai/ling-3.0-flash-sante:free` in `ask_ai`; model availability and provider limits may change.
- Saved notes are explicit facts, not conversation history. There is currently no user command to delete them.
- JSON storage supports one running bot process, not concurrent writers.
- Very long AI replies are not split into multiple Telegram messages.
- No claim of production hardening or complete end-to-end multi-account verification.
