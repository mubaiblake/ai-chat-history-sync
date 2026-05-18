# AI Chat History Sync

Collect AI chat histories from local tools and browser exports into a local,
Obsidian-friendly Markdown archive.

This repository is the archive and organizer. It is intentionally separate from
the ChatGPT web userscript:

```text
chatgpt-exporter-auto-sync
  ChatGPT web -> local JSON receiver

ai-chat-history-sync
  local logs / web JSON exports -> raw archive -> Markdown -> manifest
```

## Sources

Current importers:

- Claude Code local sessions from `~/.claude/projects`
- Codex local sessions from `~/.codex/sessions`
- ChatGPT web JSON / JSONL exports
- Claude web Markdown / JSON / JSONL / TXT exports

ChatGPT web full-auto sync is handled through the companion submodule:

```text
vendor/chatgpt-exporter-auto-sync
```

## Folder Layout

Choose one archive root, for example:

```text
~/AI Chat History
```

The syncer manages this structure inside it:

```text
AI Chat History/
  inbox/
    chatgpt-web/
    claude-web/
  raw/
    chatgpt-web/
    claude-web/
  markdow_history/
    claude-code/
    codex-local/
    chatgpt-web/
    claude-web/
  state/
    manifest.json
    project_map.json
```

The `markdow_history` spelling is preserved for compatibility with the current
pipeline.

## Quick Start

Clone with submodules:

```bash
git clone --recurse-submodules https://github.com/baileup/ai-chat-history-sync.git
cd ai-chat-history-sync
```

Initialize the archive:

```bash
python3 ai_chat_history/sync_ai_chats.py init --archive-root "$HOME/AI Chat History"
```

Run one sync:

```bash
python3 ai_chat_history/sync_ai_chats.py sync \
  --archive-root "$HOME/AI Chat History"
```

Run continuously:

```bash
python3 ai_chat_history/sync_ai_chats.py watch \
  --archive-root "$HOME/AI Chat History" \
  --interval 60
```

## ChatGPT Web Auto-Sync

Install the companion userscript receiver from this repository:

```bash
python3 ai_chat_history/install_receiver_launchagent.py \
  --archive-root "$HOME/AI Chat History"
```

Then install the userscript from:

```text
vendor/chatgpt-exporter-auto-sync/dist/chatgpt.user.js
```

Daily use:

1. open `chatgpt.com`;
2. open the exporter dialog;
3. enable `Auto sync JSON to local`;
4. keep the ChatGPT tab open.

The userscript sends JSON to the receiver, and this project imports it into raw
JSON plus Markdown.

## Submodule Setup

If you cloned without submodules:

```bash
git submodule update --init --recursive
```

If you publish under a different GitHub username, edit `.gitmodules` and replace:

```text
https://github.com/baileup/chatgpt-exporter-auto-sync.git
```

with your actual repository URL.

## Project Folder Names

Web project names are configured in:

```text
AI Chat History/state/project_map.json
```

The syncer auto-adds missing project IDs. You can edit `name` and `dir` to choose
human-readable folders.

## Tests

```bash
python3 -m unittest ai_chat_history.tests.test_sync_ai_chats -v
python3 -m py_compile ai_chat_history/sync_ai_chats.py ai_chat_history/install_receiver_launchagent.py
```

## Privacy

Do not commit these folders:

```text
inbox/
raw/
markdow_history/
state/
```

They contain your private chat history, logs, and local sync state. The included
`.gitignore` excludes them by default.
