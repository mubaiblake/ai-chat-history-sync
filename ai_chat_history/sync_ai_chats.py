#!/usr/bin/env python3
"""Sync local and browser-exported AI chats into an Obsidian-friendly archive.

The archive is intentionally source-separated:

    ai_chat_history/
      markdow_history/
        claude-code/
        codex-local/
        chatgpt-web/
        claude-web/
      inbox/
      raw/
      state/

Local logs are parsed in place. Browser exports can be dropped into the project
root or inbox, and can also be pulled from Downloads with --move-web-exports.
"""

from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


ARCHIVE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ARCHIVE_ROOT.parent
MARKDOWN_HISTORY_DIR = "markdow_history"
MANIFEST_PATH = ARCHIVE_ROOT / "state" / "manifest.json"
PROJECT_MAP_PATH = ARCHIVE_ROOT / "state" / "project_map.json"
DEFAULT_DOWNLOADS = Path.home() / "Downloads"
DEFAULT_CLAUDE_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_CODEX_ROOT = Path.home() / ".codex" / "sessions"
DEFAULT_RECEIVE_HOST = "127.0.0.1"
DEFAULT_RECEIVE_PORT = 8765
DEFAULT_RECEIVE_TOKEN_PATH = ARCHIVE_ROOT / "state" / "receiver_token.txt"

SOURCE_DIRS = {
    "claude-code": "claude-code",
    "codex-local": "codex-local",
    "chatgpt-web": "chatgpt-web",
    "claude-web": "claude-web",
}

LOCAL_SOURCES = {"claude-code", "codex-local"}
WEB_SOURCES = {"chatgpt-web", "claude-web"}
WEB_GLOBAL_DIR = "global"
WEB_EXPORT_PATTERNS = [
    ("chatgpt-web", re.compile(r"(^chatgpt.*\.jsonl?$|^ChatGPT-.*\.jsonl?$)", re.IGNORECASE)),
    ("claude-web", re.compile(r"^claude[-_ ].*\.(jsonl?|md|txt)$", re.IGNORECASE)),
]
RECEIVE_SOURCE_PATHS = {
    "/chatgpt-web": "chatgpt-web",
    "/claude-web": "claude-web",
}


def configure_archive_root(root: Path) -> None:
    """Point all generated archive state at a user-selected root directory."""
    global ARCHIVE_ROOT, PROJECT_ROOT, MANIFEST_PATH, PROJECT_MAP_PATH, DEFAULT_RECEIVE_TOKEN_PATH
    ARCHIVE_ROOT = root.expanduser().resolve()
    PROJECT_ROOT = ARCHIVE_ROOT.parent
    MANIFEST_PATH = ARCHIVE_ROOT / "state" / "manifest.json"
    PROJECT_MAP_PATH = ARCHIVE_ROOT / "state" / "project_map.json"
    DEFAULT_RECEIVE_TOKEN_PATH = ARCHIVE_ROOT / "state" / "receiver_token.txt"


PROJECT_ID_FIELDS = (
    "project_id",
    "project_uuid",
    "workspace_id",
    "gizmo_id",
    "conversation_template_id",
)
PROJECT_NAME_FIELDS = (
    "project_name",
    "project_title",
    "workspace_name",
    "gizmo_name",
    "conversation_template_name",
)

SKIPPED_ROLES = {"system", "developer", "tool"}
KEPT_ROLES = {"user", "assistant"}
SKIPPED_CLAUDE_TYPES = {
    "attachment",
    "control",
    "developer",
    "error",
    "event",
    "file_history_snapshot",
    "file-history-snapshot",
    "hook",
    "meta",
    "summary",
    "system",
    "tool",
    "tool_result",
    "tool_use",
}
SKIPPED_CONTENT_TYPES = {"thinking", "tool_result", "tool_use"}

CONTROL_TEXT_PATTERNS = [
    re.compile(r"<\s*/?\s*system-reminder\b", re.IGNORECASE),
    re.compile(r"<\s*local-command-caveat\b", re.IGNORECASE),
    re.compile(r"<\s*local-command-stdout\b", re.IGNORECASE),
    re.compile(r"<\s*command-name\s*>", re.IGNORECASE),
    re.compile(r"<\s*turn_aborted\s*>", re.IGNORECASE),
    re.compile(r"\b(request|operation|response|turn)\s+(interrupted|aborted|cancelled|canceled)\b", re.IGNORECASE),
    re.compile(r"^\s*\[?(interrupt|interrupted|abort|aborted|cancelled|canceled)\]?\s*$", re.IGNORECASE),
]


@dataclass(frozen=True)
class ChatMessage:
    role: str
    text: str
    timestamp: str | None = None


@dataclass
class Conversation:
    source: str
    conversation_id: str
    title: str
    messages: list[ChatMessage]
    created_at: str | None = None
    updated_at: str | None = None
    project: str | None = None
    raw_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_real_dialogue(self, min_messages: int) -> bool:
        if len(self.messages) < min_messages:
            return False
        roles = {message.role for message in self.messages}
        if not {"user", "assistant"}.issubset(roles):
            return False
        return any(len(message.text.strip()) >= 3 for message in self.messages)


def normalize_role(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_")


def is_control_text(text: str) -> bool:
    stripped = text.strip()
    return any(pattern.search(stripped) for pattern in CONTROL_TEXT_PATTERNS)


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_datetime(value: Any) -> str | None:
    parsed = parse_datetime(value)
    if not parsed:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def first_datetime(messages: list[ChatMessage]) -> str | None:
    for message in messages:
        if message.timestamp:
            return format_datetime(message.timestamp)
    return None


def last_datetime(messages: list[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if message.timestamp:
            return format_datetime(message.timestamp)
    return None


def slugify_filename(text: str, max_length: int = 64) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    collapsed = re.sub(r"\s+", "-", first_line)
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", collapsed)
    safe = safe.strip(" .-_，。！？、；：")
    if len(safe) > max_length:
        safe = safe[:max_length].strip(" .-_，。！？、；：")
    return safe or "untitled"


def slugify_dirname(text: str, max_length: int = 80) -> str:
    safe = slugify_filename(text, max_length=max_length)
    return safe or "untitled"


def short_id(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "", value)
    if len(clean) >= 8:
        return clean[:10]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_project_metadata(raw: dict[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    project_value = raw.get("project")
    if isinstance(project_value, dict):
        metadata["project_id"] = first_string(
            project_value.get("id"),
            project_value.get("uuid"),
            project_value.get("project_id"),
        ) or ""
        metadata["project_name"] = first_string(
            project_value.get("name"),
            project_value.get("title"),
            project_value.get("project_name"),
        ) or ""
    elif isinstance(project_value, str) and project_value.strip():
        metadata["project_id"] = project_value.strip()

    project_id = first_string(*(raw.get(field) for field in PROJECT_ID_FIELDS))
    project_name = first_string(*(raw.get(field) for field in PROJECT_NAME_FIELDS))
    if project_id:
        metadata["project_id"] = project_id
    if project_name:
        metadata["project_name"] = project_name
    return {key: value for key, value in metadata.items() if value}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(data)


def read_json_or_jsonl(path: Path) -> list[Any]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        rows: list[Any] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL record: {exc}") from exc
        return rows
    return parsed if isinstance(parsed, list) else [parsed]


def iter_files(root: Path, suffixes: set[str]) -> Iterable[Path]:
    if not root.exists():
        return
    if root.is_file():
        if root.suffix.lower() in suffixes:
            yield root
        return
    for candidate in sorted(root.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() in suffixes:
            yield candidate


def extract_text_parts(content: Any) -> Iterable[str]:
    if isinstance(content, str):
        if content.strip() and not is_control_text(content):
            yield content
        return
    if isinstance(content, list):
        for item in content:
            yield from extract_text_parts(item)
        return
    if not isinstance(content, dict):
        return

    content_type = normalize_role(content.get("type"))
    if content_type in SKIPPED_CONTENT_TYPES:
        return
    text = content.get("text")
    if isinstance(text, str) and text.strip() and not is_control_text(text):
        yield text
        return
    parts = content.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str) and part.strip() and not is_control_text(part):
                yield part


def claude_record_to_message(record: Any) -> ChatMessage | None:
    if not isinstance(record, dict):
        return None
    if record.get("isMeta") or record.get("is_meta") or record.get("interrupted") or record.get("isInterrupted"):
        return None
    record_type = normalize_role(record.get("type"))
    if record_type in SKIPPED_CLAUDE_TYPES:
        return None

    message = record.get("message")
    if isinstance(message, dict):
        role = normalize_role(message.get("role") or record.get("role") or record.get("type"))
        content = message.get("content")
    else:
        role = normalize_role(record.get("role") or record.get("type"))
        content = record.get("content") or record.get("text")

    if role not in KEPT_ROLES:
        return None

    text = "\n\n".join(part.strip() for part in extract_text_parts(content) if part.strip()).strip()
    if not text or is_control_text(text):
        return None
    return ChatMessage(role=role, text=text, timestamp=format_datetime(record.get("timestamp") or record.get("created_at")))


def parse_claude_code_file(path: Path) -> Conversation | None:
    records = read_json_or_jsonl(path)
    messages = [message for record in records if (message := claude_record_to_message(record))]
    metadata = next((record for record in records if isinstance(record, dict) and record.get("sessionId")), {})
    conversation_id = str(metadata.get("sessionId") or path.stem)
    project = metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else None
    title = first_user_title(messages) or path.stem
    return Conversation(
        source="claude-code",
        conversation_id=conversation_id,
        title=title,
        messages=messages,
        created_at=first_datetime(messages),
        updated_at=last_datetime(messages),
        project=project,
        raw_path=str(path),
        metadata={"source_file": str(path)},
    )


def codex_record_to_message(record: Any) -> ChatMessage | None:
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if event_type == "user_message":
        role = "user"
        text = payload.get("message")
    elif event_type == "agent_message":
        role = "assistant"
        text = payload.get("message")
    else:
        return None
    if not isinstance(text, str) or not text.strip() or is_control_text(text):
        return None
    return ChatMessage(role=role, text=text.strip(), timestamp=format_datetime(record.get("timestamp")))


def parse_codex_file(path: Path) -> Conversation | None:
    records = read_json_or_jsonl(path)
    messages = [message for record in records if (message := codex_record_to_message(record))]
    session_meta = next(
        (
            record.get("payload")
            for record in records
            if isinstance(record, dict) and record.get("type") == "session_meta" and isinstance(record.get("payload"), dict)
        ),
        {},
    )
    conversation_id = str(session_meta.get("id") or path.stem)
    project = session_meta.get("cwd") if isinstance(session_meta.get("cwd"), str) else None
    title = first_user_title(messages) or path.stem
    return Conversation(
        source="codex-local",
        conversation_id=conversation_id,
        title=title,
        messages=messages,
        created_at=format_datetime(session_meta.get("timestamp")) or first_datetime(messages),
        updated_at=last_datetime(messages),
        project=project,
        raw_path=str(path),
        metadata={"source_file": str(path), "originator": session_meta.get("originator")},
    )


def merge_conversations(existing: Conversation, incoming: Conversation) -> Conversation:
    seen: set[tuple[str, str, str | None]] = set()
    messages: list[ChatMessage] = []
    for message in existing.messages + incoming.messages:
        key = (message.role, message.text, message.timestamp)
        if key in seen:
            continue
        seen.add(key)
        messages.append(message)
    messages.sort(key=lambda message: parse_datetime(message.timestamp) or datetime.min.replace(tzinfo=timezone.utc))

    created_values = [value for value in [existing.created_at, incoming.created_at, first_datetime(messages)] if value]
    updated_values = [value for value in [existing.updated_at, incoming.updated_at, last_datetime(messages)] if value]
    created_at = min(created_values, key=lambda value: parse_datetime(value) or datetime.max.replace(tzinfo=timezone.utc)) if created_values else None
    updated_at = max(updated_values, key=lambda value: parse_datetime(value) or datetime.min.replace(tzinfo=timezone.utc)) if updated_values else None

    raw_paths = []
    for value in [existing.raw_path, incoming.raw_path]:
        if value and value not in raw_paths:
            raw_paths.append(value)

    existing.metadata.setdefault("source_files", [])
    if isinstance(existing.metadata["source_files"], list):
        for value in raw_paths:
            if value not in existing.metadata["source_files"]:
                existing.metadata["source_files"].append(value)

    existing.messages = messages
    existing.title = first_user_title(messages) or existing.title or incoming.title
    existing.created_at = created_at
    existing.updated_at = updated_at
    existing.project = existing.project or incoming.project
    existing.raw_path = "; ".join(raw_paths) if raw_paths else existing.raw_path
    return existing


def write_grouped_conversations(
    conversations: Iterable[Conversation],
    manifest: dict[str, Any],
    min_messages: int,
    rewrite_paths: bool = False,
    project_map: dict[str, Any] | None = None,
) -> dict[str, int]:
    stats = new_stats()
    grouped: dict[str, Conversation] = {}
    for conversation in conversations:
        key = conversation_key(conversation)
        if key in grouped:
            merge_conversations(grouped[key], conversation)
        else:
            grouped[key] = conversation
    for conversation in grouped.values():
        stats[write_conversation(conversation, manifest, min_messages, rewrite_paths, project_map)] += 1
    return stats


def first_user_title(messages: list[ChatMessage]) -> str | None:
    for message in messages:
        if message.role == "user":
            return slugify_filename(message.text, max_length=48).replace("-", " ")
    return None


def chatgpt_content_to_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if isinstance(parts, list):
        texts = [part for part in parts if isinstance(part, str) and part.strip()]
        return "\n\n".join(texts).strip()
    text = content.get("text")
    return text.strip() if isinstance(text, str) else ""


def parse_chatgpt_conversation(raw: dict[str, Any], raw_path: Path) -> Conversation | None:
    mapping = raw.get("mapping")
    if not isinstance(mapping, dict):
        return None

    ordered_nodes: list[dict[str, Any]] = []
    current_node = raw.get("current_node")
    if isinstance(current_node, str) and current_node in mapping:
        node_id: str | None = current_node
        seen: set[str] = set()
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            node = mapping[node_id]
            if isinstance(node, dict):
                ordered_nodes.append(node)
                parent = node.get("parent")
                node_id = parent if isinstance(parent, str) else None
            else:
                node_id = None
        ordered_nodes.reverse()
    else:
        ordered_nodes = [node for node in mapping.values() if isinstance(node, dict)]
        ordered_nodes.sort(key=lambda node: node.get("message", {}).get("create_time") or 0)

    messages: list[ChatMessage] = []
    for node in ordered_nodes:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        role = normalize_role(author.get("role") if isinstance(author, dict) else "")
        if role not in KEPT_ROLES:
            continue
        text = chatgpt_content_to_text(message.get("content"))
        if not text or is_control_text(text):
            continue
        messages.append(ChatMessage(role=role, text=text, timestamp=format_datetime(message.get("create_time"))))

    conversation_id = str(raw.get("id") or raw.get("conversation_id") or stable_json_hash(raw))
    title = str(raw.get("title") or first_user_title(messages) or conversation_id)
    project_metadata = extract_project_metadata(raw)
    return Conversation(
        source="chatgpt-web",
        conversation_id=conversation_id,
        title=title,
        messages=messages,
        created_at=format_datetime(raw.get("create_time")) or first_datetime(messages),
        updated_at=format_datetime(raw.get("update_time")) or last_datetime(messages),
        project=project_metadata.get("project_name") or project_metadata.get("project_id"),
        raw_path=str(raw_path),
        metadata={
            "source_file": str(raw_path),
            "project_id": project_metadata.get("project_id"),
            "project_name": project_metadata.get("project_name"),
            "conversation_template_id": raw.get("conversation_template_id"),
            "gizmo_id": raw.get("gizmo_id"),
            "gizmo_type": raw.get("gizmo_type"),
        },
    )


def generic_web_json_to_conversation(raw: Any, source: str, raw_path: Path) -> Conversation | None:
    if not isinstance(raw, dict):
        return None
    candidates = raw.get("messages") or raw.get("chat_messages") or raw.get("conversation")
    if not isinstance(candidates, list):
        return None
    messages: list[ChatMessage] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        role = normalize_role(item.get("role") or item.get("sender") or item.get("author"))
        if role in {"human"}:
            role = "user"
        if role not in KEPT_ROLES:
            continue
        text = item.get("text") or item.get("content") or item.get("message")
        if isinstance(text, dict):
            text = "\n\n".join(extract_text_parts(text))
        if not isinstance(text, str) or not text.strip() or is_control_text(text):
            continue
        messages.append(ChatMessage(role=role, text=text.strip(), timestamp=format_datetime(item.get("created_at") or item.get("timestamp"))))
    conversation_id = str(raw.get("id") or raw.get("uuid") or raw.get("conversation_id") or stable_json_hash(raw))
    title = str(raw.get("title") or first_user_title(messages) or raw_path.stem)
    project_metadata = extract_project_metadata(raw)
    return Conversation(
        source=source,
        conversation_id=conversation_id,
        title=title,
        messages=messages,
        created_at=format_datetime(raw.get("created_at")) or first_datetime(messages),
        updated_at=format_datetime(raw.get("updated_at")) or last_datetime(messages),
        project=project_metadata.get("project_name") or project_metadata.get("project_id"),
        raw_path=str(raw_path),
        metadata={
            "source_file": str(raw_path),
            "project_id": project_metadata.get("project_id"),
            "project_name": project_metadata.get("project_name"),
        },
    )


def tavern_jsonl_to_conversation(rows: list[Any], source: str, raw_path: Path) -> Conversation | None:
    messages: list[ChatMessage] = []
    for row in rows:
        if not isinstance(row, dict) or "mes" not in row:
            continue
        text = row.get("mes")
        if not isinstance(text, str) or not text.strip() or is_control_text(text):
            continue
        is_user = row.get("is_user")
        if isinstance(is_user, bool):
            role = "user" if is_user else "assistant"
        else:
            role = normalize_role(row.get("role") or row.get("name"))
            if role in {"you", "human"}:
                role = "user"
            elif role not in KEPT_ROLES:
                role = "assistant"
        messages.append(ChatMessage(role=role, text=text.strip(), timestamp=format_datetime(row.get("send_date") or row.get("timestamp"))))

    if not messages:
        return None

    file_hash = stable_json_hash(rows)
    return Conversation(
        source=source,
        conversation_id=file_hash,
        title=first_user_title(messages) or raw_path.stem,
        messages=messages,
        created_at=first_datetime(messages),
        updated_at=last_datetime(messages),
        raw_path=str(raw_path),
        metadata={"source_file": str(raw_path), "import_mode": "tavern_jsonl"},
    )


def markdown_file_to_conversation(path: Path, source: str) -> Conversation:
    text = path.read_text(encoding="utf-8")
    file_hash = sha256_bytes(text.encode("utf-8"))
    title = path.stem
    messages = [ChatMessage(role="user", text=text, timestamp=format_datetime(path.stat().st_mtime))]
    return Conversation(
        source=source,
        conversation_id=file_hash,
        title=title,
        messages=messages,
        created_at=format_datetime(path.stat().st_mtime),
        updated_at=format_datetime(path.stat().st_mtime),
        raw_path=str(path),
        metadata={"source_file": str(path), "import_mode": "markdown_passthrough"},
    )


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {"version": 1, "items": {}, "raw_files": {}}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = MANIFEST_PATH.with_suffix(".corrupt.json")
        shutil.copy2(MANIFEST_PATH, backup)
        return {"version": 1, "items": {}, "raw_files": {}, "corrupt_backup": str(backup)}


def save_manifest(manifest: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def default_project_map() -> dict[str, Any]:
    return {
        "chatgpt-web": {
            "global_dir": WEB_GLOBAL_DIR,
            "projects": {},
        },
        "claude-web": {
            "global_dir": WEB_GLOBAL_DIR,
            "projects": {},
        },
    }


def load_project_map() -> dict[str, Any]:
    if not PROJECT_MAP_PATH.exists():
        return default_project_map()
    try:
        loaded = json.loads(PROJECT_MAP_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = PROJECT_MAP_PATH.with_suffix(".corrupt.json")
        shutil.copy2(PROJECT_MAP_PATH, backup)
        loaded = default_project_map()
        loaded["corrupt_backup"] = str(backup)
    if not isinstance(loaded, dict):
        return default_project_map()
    for source, defaults in default_project_map().items():
        source_map = loaded.setdefault(source, {})
        if not isinstance(source_map, dict):
            loaded[source] = defaults
            continue
        source_map.setdefault("global_dir", defaults["global_dir"])
        projects = source_map.setdefault("projects", {})
        if not isinstance(projects, dict):
            source_map["projects"] = {}
    return loaded


def save_project_map(project_map: dict[str, Any]) -> None:
    PROJECT_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_MAP_PATH.write_text(json.dumps(project_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def project_id_for(conversation: Conversation) -> str | None:
    project_id = conversation.metadata.get("project_id")
    if isinstance(project_id, str) and project_id.strip():
        return project_id.strip()
    if conversation.source in WEB_SOURCES and conversation.project:
        return conversation.project
    return None


def project_name_for(conversation: Conversation) -> str | None:
    project_name = conversation.metadata.get("project_name")
    if isinstance(project_name, str) and project_name.strip():
        return project_name.strip()
    if conversation.source in WEB_SOURCES and conversation.project and conversation.project != project_id_for(conversation):
        return conversation.project
    return None


def normalize_project_entry(entry: Any, project_id: str, fallback_name: str | None) -> dict[str, str]:
    if isinstance(entry, str):
        return {"name": entry, "dir": slugify_dirname(entry or project_id)}
    if isinstance(entry, dict):
        name = first_string(entry.get("name"), fallback_name) or ""
        dirname = first_string(entry.get("dir"), entry.get("dirname"), entry.get("folder"))
        if name and dirname == project_id:
            dirname = name
        return {"name": name, "dir": slugify_dirname(dirname or name or project_id)}
    name = fallback_name or ""
    return {"name": name, "dir": slugify_dirname(name or project_id)}


def project_config_for(conversation: Conversation, project_map: dict[str, Any] | None) -> dict[str, str]:
    source_map = (project_map or {}).get(conversation.source, {})
    if not isinstance(source_map, dict):
        source_map = {}
    project_id = project_id_for(conversation)
    if not project_id:
        dirname = first_string(source_map.get("global_dir")) or WEB_GLOBAL_DIR
        return {"id": "", "name": "全局聊天", "dir": slugify_dirname(dirname)}

    projects = source_map.setdefault("projects", {})
    if not isinstance(projects, dict):
        source_map["projects"] = {}
        projects = source_map["projects"]
    fallback_name = project_name_for(conversation)
    normalized = normalize_project_entry(projects.get(project_id), project_id, fallback_name)
    projects[project_id] = normalized
    return {"id": project_id, "name": normalized.get("name", ""), "dir": normalized["dir"]}


def migrate_manifest_markdown_paths(manifest: dict[str, Any]) -> int:
    """Move legacy root-level Markdown outputs into markdow_history."""
    migrated = 0
    items = manifest.setdefault("items", {})
    if not isinstance(items, dict):
        return migrated

    for item in items.values():
        if not isinstance(item, dict):
            continue
        current = item.get("markdown_path")
        if not isinstance(current, str) or not current:
            continue
        normalized = normalize_markdown_path(current)
        if normalized == current:
            continue

        old_path = (ARCHIVE_ROOT / current).resolve()
        new_path = (ARCHIVE_ROOT / normalized).resolve()
        if new_path.exists():
            item["markdown_path"] = normalized
            migrated += 1
            continue
        if not old_path.exists() or not is_under(old_path, ARCHIVE_ROOT):
            continue

        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_path), str(new_path))
        item["markdown_path"] = normalized
        migrated += 1
    return migrated


def conversation_key(conversation: Conversation) -> str:
    return f"{conversation.source}:{conversation.conversation_id}"


def conversation_hash(conversation: Conversation, project_map: dict[str, Any] | None = None) -> str:
    project_config = project_config_for(conversation, project_map) if conversation.source in WEB_SOURCES else {}
    payload = {
        "source": conversation.source,
        "conversation_id": conversation.conversation_id,
        "title": conversation.title,
        "messages": [message.__dict__ for message in conversation.messages],
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "project": conversation.project,
    }
    if conversation.source in WEB_SOURCES:
        payload["project_config"] = project_config
    return stable_json_hash(payload)


def markdown_parent(conversation: Conversation, project_map: dict[str, Any] | None = None) -> Path:
    dt = parse_datetime(conversation.created_at) or parse_datetime(conversation.updated_at) or datetime.now(timezone.utc)
    month = dt.strftime("%Y-%m")
    parts = [MARKDOWN_HISTORY_DIR, SOURCE_DIRS[conversation.source]]
    if conversation.source in WEB_SOURCES:
        parts.append(project_config_for(conversation, project_map)["dir"])
    parts.append(month)
    return ARCHIVE_ROOT.joinpath(*parts)


def existing_path_matches_layout(conversation: Conversation, existing: str, project_map: dict[str, Any] | None = None) -> bool:
    existing_path = (ARCHIVE_ROOT / normalize_markdown_path(existing)).resolve()
    return existing_path.parent == markdown_parent(conversation, project_map).resolve()


def build_markdown_path(
    conversation: Conversation,
    existing: str | None = None,
    project_map: dict[str, Any] | None = None,
) -> Path:
    if existing and existing_path_matches_layout(conversation, existing, project_map):
        return (ARCHIVE_ROOT / normalize_markdown_path(existing)).resolve()
    dt = parse_datetime(conversation.created_at) or parse_datetime(conversation.updated_at) or datetime.now(timezone.utc)
    day = dt.strftime("%Y-%m-%d")
    title = slugify_filename(conversation.title or first_user_title(conversation.messages) or "untitled")
    filename = f"{day}-{title}--{short_id(conversation.conversation_id)}.md"
    return markdown_parent(conversation, project_map) / filename


def normalize_markdown_path(value: str) -> str:
    """Map legacy root-level source Markdown paths into markdow_history."""
    path = Path(value)
    parts = path.parts
    if not parts:
        return value
    if parts[0] == MARKDOWN_HISTORY_DIR:
        return value
    if parts[0] in SOURCE_DIRS.values():
        return str(Path(MARKDOWN_HISTORY_DIR, *parts))
    return value


def yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    return json.dumps(value, ensure_ascii=False)


def render_markdown(conversation: Conversation, project_map: dict[str, Any] | None = None) -> str:
    project_config = project_config_for(conversation, project_map) if conversation.source in WEB_SOURCES else {}
    project_id = project_config.get("id", "")
    project_name = project_config.get("name", "") if project_id else ""
    project_dir = project_config.get("dir", "") if conversation.source in WEB_SOURCES else ""
    project_display = project_name or project_id or conversation.project
    lines = [
        "---",
        f"source: {yaml_scalar(conversation.source)}",
        f"conversation_id: {yaml_scalar(conversation.conversation_id)}",
        f"title: {yaml_scalar(conversation.title)}",
        f"created_at: {yaml_scalar(conversation.created_at)}",
        f"updated_at: {yaml_scalar(conversation.updated_at)}",
        f"project: {yaml_scalar(project_display)}",
        f"project_id: {yaml_scalar(project_id)}",
        f"project_name: {yaml_scalar(project_name)}",
        f"project_dir: {yaml_scalar(project_dir)}",
        f"message_count: {len(conversation.messages)}",
        "tags:",
        "  - ai-chat",
        f"  - {conversation.source}",
    ]
    if conversation.source in WEB_SOURCES:
        lines.append("  - web-project" if project_id else "  - web-global")
    lines.extend(["---", "", f"# {conversation.title}", ""])
    if project_display:
        lines.extend([f"- Project: `{project_display}`", ""])
    if project_id and project_name:
        lines.extend([f"- Project ID: `{project_id}`", ""])
    if conversation.raw_path:
        lines.extend([f"- Raw source: `{conversation.raw_path}`", ""])

    for message in conversation.messages:
        heading = "User" if message.role == "user" else "Assistant"
        lines.append(f"## {heading}")
        if message.timestamp:
            lines.extend([f"_Timestamp: {message.timestamp}_", ""])
        lines.extend([message.text.strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def write_conversation(
    conversation: Conversation,
    manifest: dict[str, Any],
    min_messages: int,
    rewrite_paths: bool = False,
    project_map: dict[str, Any] | None = None,
) -> str:
    if not conversation.has_real_dialogue(min_messages):
        return "skipped_empty"

    key = conversation_key(conversation)
    items = manifest.setdefault("items", {})
    existing_item = items.get(key, {}) if isinstance(items.get(key), dict) else {}
    project_config = project_config_for(conversation, project_map) if conversation.source in WEB_SOURCES else {}
    content_hash = conversation_hash(conversation, project_map)
    existing_path = existing_item.get("markdown_path")
    target = build_markdown_path(conversation, None if rewrite_paths else existing_path, project_map)
    old_target = (ARCHIVE_ROOT / existing_path).resolve() if existing_path else None
    path_changed = bool(old_target and old_target != target.resolve())

    if existing_item.get("content_hash") == content_hash and existing_path and not path_changed:
        return "unchanged"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(conversation, project_map), encoding="utf-8")
    if path_changed and old_target and old_target.exists() and is_under(old_target, ARCHIVE_ROOT):
        old_target.unlink()

    items[key] = {
        "source": conversation.source,
        "conversation_id": conversation.conversation_id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "project": conversation.project,
        "project_id": project_config.get("id", ""),
        "project_name": project_config.get("name", "") if project_config.get("id") else "",
        "project_dir": project_config.get("dir", "") if conversation.source in WEB_SOURCES else "",
        "message_count": len(conversation.messages),
        "content_hash": content_hash,
        "markdown_path": str(target.relative_to(ARCHIVE_ROOT)),
        "raw_path": conversation.raw_path,
        "last_synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    return "updated" if existing_item else "created"


def ensure_dirs() -> None:
    for dirname in SOURCE_DIRS.values():
        (ARCHIVE_ROOT / MARKDOWN_HISTORY_DIR / dirname).mkdir(parents=True, exist_ok=True)
    for source in WEB_SOURCES:
        (ARCHIVE_ROOT / "inbox" / source).mkdir(parents=True, exist_ok=True)
        (ARCHIVE_ROOT / "inbox" / source / "processed").mkdir(parents=True, exist_ok=True)
        (ARCHIVE_ROOT / "raw" / source).mkdir(parents=True, exist_ok=True)
    (ARCHIVE_ROOT / "state").mkdir(parents=True, exist_ok=True)
    if not PROJECT_MAP_PATH.exists():
        save_project_map(default_project_map())


def archive_raw_file(path: Path, source: str, manifest: dict[str, Any]) -> Path:
    data = path.read_bytes()
    file_hash = sha256_bytes(data)
    raw_files = manifest.setdefault("raw_files", {})
    existing = raw_files.get(file_hash)
    if existing:
        return ARCHIVE_ROOT / existing
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = ARCHIVE_ROOT / "raw" / source / f"{stamp}-{file_hash[:12]}{path.suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    raw_files[file_hash] = str(target.relative_to(ARCHIVE_ROOT))
    return target


def move_web_exports_from_directory(
    source_dir: Path,
    manifest: dict[str, Any],
    inbox_root: Path | None = None,
    label: str = "web-exports",
) -> list[Path]:
    moved: list[Path] = []
    if not source_dir.exists():
        return moved
    try:
        candidates = sorted(source_dir.iterdir())
    except PermissionError as exc:
        print(f"[{label}] cannot read directory {source_dir}: {exc}", file=sys.stderr, flush=True)
        return moved

    destination_root = inbox_root or (ARCHIVE_ROOT / "inbox")
    for path in candidates:
        if not path.is_file():
            continue
        for source, pattern in WEB_EXPORT_PATTERNS:
            if not pattern.search(path.name):
                continue
            file_hash = sha256_bytes(path.read_bytes())
            if file_hash in manifest.setdefault("raw_files", {}):
                target = destination_root / source / "processed" / path.name
            else:
                target = destination_root / source / path.name
            target = unique_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved.append(target)
            break
    return moved


def move_web_exports_from_drop_root(drop_root: Path, manifest: dict[str, Any]) -> list[Path]:
    return move_web_exports_from_directory(drop_root, manifest, label="web-drop-root")


def move_web_exports_from_downloads(downloads: Path, manifest: dict[str, Any]) -> list[Path]:
    return move_web_exports_from_directory(downloads, manifest, label="web-downloads")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique path for {path}")


def sanitize_received_filename(filename: str, source: str) -> str:
    name = Path(filename or "").name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "-", name).strip(" .-")
    if not name:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{source}-received-{stamp}.json"
    if Path(name).suffix.lower() not in {".json", ".jsonl", ".md", ".txt"}:
        name += ".json"
    return name


def write_received_web_export(source: str, payload: dict[str, Any]) -> Path:
    if source not in WEB_SOURCES:
        raise ValueError(f"unsupported source: {source}")
    ensure_dirs()
    filename = sanitize_received_filename(str(payload.get("filename") or ""), source)
    if "content" in payload:
        content = payload["content"]
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
    elif "conversations" in payload:
        content = json.dumps(payload["conversations"], ensure_ascii=False)
    else:
        raise ValueError("payload must include content or conversations")
    target = unique_path(ARCHIVE_ROOT / "inbox" / source / filename)
    target.write_text(content, encoding="utf-8")
    return target


def receiver_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    if args.no_token:
        return ""
    if args.token_file is None:
        return ""
    token_path = args.token_file.expanduser()
    if token_path.exists():
        return token_path.read_text(encoding="utf-8").strip()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    token_path.write_text(token + "\n", encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return token


def timestamp_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value * 1000)
    if isinstance(value, str):
        dt = parse_datetime(value)
        if dt:
            return int(dt.timestamp() * 1000)
    return 0


def chatgpt_index_record_from_raw(raw: dict[str, Any], raw_path: Path) -> tuple[str, dict[str, Any]] | None:
    conversation_id = raw.get("id") or raw.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    updated_at = format_datetime(raw.get("update_time") or raw.get("updated_at") or raw.get("modified_at"))
    created_at = format_datetime(raw.get("create_time") or raw.get("created_at"))
    updated_ms = timestamp_ms(updated_at or created_at)
    return conversation_id, {
        "conversation_id": conversation_id,
        "title": raw.get("title") if isinstance(raw.get("title"), str) else "",
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "updated_ms": updated_ms,
        "raw_path": str(raw_path),
    }


def build_web_source_index(source: str) -> dict[str, dict[str, Any]]:
    if source not in WEB_SOURCES:
        raise ValueError(f"unsupported source: {source}")

    records: dict[str, dict[str, Any]] = {}
    manifest = load_manifest()
    items = manifest.get("items", {})
    if isinstance(items, dict):
        for item in items.values():
            if not isinstance(item, dict) or item.get("source") != source:
                continue
            conversation_id = item.get("conversation_id")
            if not isinstance(conversation_id, str) or not conversation_id:
                continue
            records[conversation_id] = {
                "conversation_id": conversation_id,
                "title": item.get("title", ""),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at") or item.get("created_at"),
                "updated_ms": timestamp_ms(item.get("updated_at") or item.get("created_at")),
                "markdown_path": item.get("markdown_path", ""),
                "raw_path": item.get("raw_path", ""),
            }

    # Manifest is the fast path. Scanning raw JSON keeps the index truthful if a
    # user moves in old source files before rebuilding Markdown.
    for root in (ARCHIVE_ROOT / "raw" / source, ARCHIVE_ROOT / "inbox" / source / "processed"):
        if not root.exists():
            continue
        for path in iter_files(root, {".json", ".jsonl"}):
            try:
                rows = read_json_or_jsonl(path)
            except Exception:
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                record = chatgpt_index_record_from_raw(row, path) if source == "chatgpt-web" else None
                if not record:
                    continue
                conversation_id, raw_record = record
                existing = records.get(conversation_id)
                if not existing or int(raw_record.get("updated_ms") or 0) > int(existing.get("updated_ms") or 0):
                    records[conversation_id] = {**existing, **raw_record} if existing else raw_record
    return records


def run_receive_server(args: argparse.Namespace) -> int:
    ensure_dirs()
    token = receiver_token(args)
    receive_stats = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "received_files": 0,
        "received_conversations": 0,
        "errors": 0,
        "last_received_at": None,
        "last_path": None,
    }

    class Receiver(BaseHTTPRequestHandler):
        server_version = "AIChatHistoryReceiver/1.0"

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Local-Sync-Token")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            super().end_headers()

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if not self.authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            if path == "/ping":
                self.send_json(200, {"ok": True, "archive_root": str(ARCHIVE_ROOT), "stats": receive_stats})
                return
            if path == "/chatgpt-web/index":
                records = build_web_source_index("chatgpt-web")
                self.send_json(200, {
                    "ok": True,
                    "source": "chatgpt-web",
                    "archive_root": str(ARCHIVE_ROOT),
                    "records": records,
                    "count": len(records),
                })
                return
            self.send_json(404, {"ok": False, "error": "unknown endpoint"})

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.end_headers()

        def do_POST(self) -> None:
            if not self.authorized():
                receive_stats["errors"] += 1
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            source = RECEIVE_SOURCE_PATHS.get(urlparse(self.path).path)
            if not source:
                self.send_json(404, {"ok": False, "error": "unknown endpoint"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                target = write_received_web_export(source, payload)
                receive_stats["received_files"] += 1
                receive_stats["received_conversations"] += len(payload.get("conversations") or [])
                receive_stats["last_received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                receive_stats["last_path"] = str(target)
                stats = None
                if args.sync_after_receive:
                    sync_args = argparse.Namespace(
                        sources=[source],
                        claude_root=args.claude_root,
                        codex_root=args.codex_root,
                        web_drop_root=args.web_drop_root,
                        downloads=args.downloads,
                        move_web_exports=False,
                        reimport_web_raw=False,
                        min_messages=args.min_messages,
                        rewrite_paths=False,
                    )
                    stats = sync_once(sync_args)
                self.send_json(200, {"ok": True, "path": str(target), "stats": stats})
            except Exception as exc:  # noqa: BLE001 - return local receiver failures to the caller
                receive_stats["errors"] += 1
                self.send_json(400, {"ok": False, "error": str(exc)})

        def authorized(self) -> bool:
            if not token:
                return True
            given = self.headers.get("X-Local-Sync-Token", "")
            return secrets.compare_digest(given, token)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[receiver] {self.address_string()} - {format % args}", flush=True)

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((args.host, args.port), Receiver)
    print(f"Receiving web exports at http://{args.host}:{args.port}/chatgpt-web", flush=True)
    print(f"Archive root: {ARCHIVE_ROOT}", flush=True)
    if token:
        print(f"Receiver token: {token}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped receiver.")
    finally:
        server.server_close()
    return 0


def sync_claude_code(
    root: Path,
    manifest: dict[str, Any],
    min_messages: int,
    rewrite_paths: bool = False,
    project_map: dict[str, Any] | None = None,
) -> dict[str, int]:
    conversations: list[Conversation] = []
    stats = new_stats()
    for path in iter_files(root, {".jsonl", ".json"}):
        try:
            conversation = parse_claude_code_file(path)
            if conversation:
                conversations.append(conversation)
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"[claude-code] failed: {path}: {exc}", file=sys.stderr)
    grouped_stats = write_grouped_conversations(conversations, manifest, min_messages, rewrite_paths, project_map)
    for key, value in grouped_stats.items():
        stats[key] += value
    return stats


def sync_codex_local(
    root: Path,
    manifest: dict[str, Any],
    min_messages: int,
    rewrite_paths: bool = False,
    project_map: dict[str, Any] | None = None,
) -> dict[str, int]:
    conversations: list[Conversation] = []
    stats = new_stats()
    for path in iter_files(root, {".jsonl"}):
        try:
            conversation = parse_codex_file(path)
            if conversation:
                conversations.append(conversation)
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"[codex-local] failed: {path}: {exc}", file=sys.stderr)
    grouped_stats = write_grouped_conversations(conversations, manifest, min_messages, rewrite_paths, project_map)
    for key, value in grouped_stats.items():
        stats[key] += value
    return stats


def sync_chatgpt_web(
    manifest: dict[str, Any],
    min_messages: int,
    rewrite_paths: bool = False,
    project_map: dict[str, Any] | None = None,
) -> dict[str, int]:
    stats = new_stats()
    for path in iter_files(ARCHIVE_ROOT / "inbox" / "chatgpt-web", {".json", ".jsonl"}):
        if "processed" in path.parts:
            continue
        try:
            raw_archive = archive_raw_file(path, "chatgpt-web", manifest)
            rows = read_json_or_jsonl(path)
            tavern_conversation = tavern_jsonl_to_conversation(rows, "chatgpt-web", raw_archive)
            if tavern_conversation:
                stats[write_conversation(tavern_conversation, manifest, min_messages, rewrite_paths, project_map)] += 1
            else:
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    conversation = parse_chatgpt_conversation(raw, raw_archive)
                    if conversation:
                        stats[write_conversation(conversation, manifest, min_messages, rewrite_paths, project_map)] += 1
            processed = unique_path(ARCHIVE_ROOT / "inbox" / "chatgpt-web" / "processed" / path.name)
            processed.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(processed))
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"[chatgpt-web] failed: {path}: {exc}", file=sys.stderr)
    return stats


def sync_archived_chatgpt_web(
    manifest: dict[str, Any],
    min_messages: int,
    rewrite_paths: bool = False,
    project_map: dict[str, Any] | None = None,
) -> dict[str, int]:
    stats = new_stats()
    for path in iter_files(ARCHIVE_ROOT / "raw" / "chatgpt-web", {".json", ".jsonl"}):
        try:
            rows = read_json_or_jsonl(path)
            tavern_conversation = tavern_jsonl_to_conversation(rows, "chatgpt-web", path)
            if tavern_conversation:
                stats[write_conversation(tavern_conversation, manifest, min_messages, rewrite_paths, project_map)] += 1
            else:
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    conversation = parse_chatgpt_conversation(raw, path)
                    if conversation:
                        stats[write_conversation(conversation, manifest, min_messages, rewrite_paths, project_map)] += 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"[chatgpt-web raw] failed: {path}: {exc}", file=sys.stderr)
    return stats


def sync_claude_web(
    manifest: dict[str, Any],
    min_messages: int,
    rewrite_paths: bool = False,
    project_map: dict[str, Any] | None = None,
) -> dict[str, int]:
    stats = new_stats()
    root = ARCHIVE_ROOT / "inbox" / "claude-web"
    for path in iter_files(root, {".json", ".jsonl", ".md", ".txt"}):
        if "processed" in path.parts:
            continue
        try:
            raw_archive = archive_raw_file(path, "claude-web", manifest)
            conversations: list[Conversation] = []
            if path.suffix.lower() in {".json", ".jsonl"}:
                rows = read_json_or_jsonl(path)
                tavern_conversation = tavern_jsonl_to_conversation(rows, "claude-web", raw_archive)
                if tavern_conversation:
                    conversations.append(tavern_conversation)
                else:
                    for row in rows:
                        conversation = generic_web_json_to_conversation(row, "claude-web", raw_archive)
                        if conversation:
                            conversations.append(conversation)
            else:
                conversation = markdown_file_to_conversation(path, "claude-web")
                conversation.raw_path = str(raw_archive)
                conversations.append(conversation)
            for conversation in conversations:
                stats[write_conversation(conversation, manifest, min_messages, rewrite_paths, project_map)] += 1
            processed = unique_path(root / "processed" / path.name)
            processed.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(processed))
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"[claude-web] failed: {path}: {exc}", file=sys.stderr)
    return stats


def sync_archived_claude_web(
    manifest: dict[str, Any],
    min_messages: int,
    rewrite_paths: bool = False,
    project_map: dict[str, Any] | None = None,
) -> dict[str, int]:
    stats = new_stats()
    for path in iter_files(ARCHIVE_ROOT / "raw" / "claude-web", {".json", ".jsonl", ".md", ".txt"}):
        try:
            conversations: list[Conversation] = []
            if path.suffix.lower() in {".json", ".jsonl"}:
                rows = read_json_or_jsonl(path)
                tavern_conversation = tavern_jsonl_to_conversation(rows, "claude-web", path)
                if tavern_conversation:
                    conversations.append(tavern_conversation)
                else:
                    for row in rows:
                        conversation = generic_web_json_to_conversation(row, "claude-web", path)
                        if conversation:
                            conversations.append(conversation)
            else:
                conversations.append(markdown_file_to_conversation(path, "claude-web"))
            for conversation in conversations:
                stats[write_conversation(conversation, manifest, min_messages, rewrite_paths, project_map)] += 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"[claude-web raw] failed: {path}: {exc}", file=sys.stderr)
    return stats


def new_stats() -> dict[str, int]:
    return {"created": 0, "updated": 0, "unchanged": 0, "skipped_empty": 0, "errors": 0}


def merge_stats(target: dict[str, int], source_name: str, source_stats: dict[str, int]) -> None:
    target[source_name] = source_stats


def print_stats(stats: dict[str, Any]) -> None:
    print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True), flush=True)


def sync_once(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    manifest = load_manifest()
    project_map = load_project_map()
    stats: dict[str, Any] = {}
    migrated = migrate_manifest_markdown_paths(manifest)
    if migrated:
        stats["migrated_markdown_paths"] = migrated

    moved_from_drop_root = move_web_exports_from_drop_root(args.web_drop_root.expanduser(), manifest)
    stats["moved_web_exports_from_drop_root"] = len(moved_from_drop_root)

    if args.move_web_exports:
        moved = move_web_exports_from_downloads(args.downloads.expanduser(), manifest)
        stats["moved_web_exports"] = len(moved)

    if "claude-code" in args.sources:
        merge_stats(stats, "claude-code", sync_claude_code(args.claude_root.expanduser(), manifest, args.min_messages, args.rewrite_paths, project_map))
    if "codex-local" in args.sources:
        merge_stats(stats, "codex-local", sync_codex_local(args.codex_root.expanduser(), manifest, args.min_messages, args.rewrite_paths, project_map))
    if "chatgpt-web" in args.sources:
        merge_stats(stats, "chatgpt-web", sync_chatgpt_web(manifest, args.min_messages, args.rewrite_paths, project_map))
        if args.reimport_web_raw:
            merge_stats(stats, "chatgpt-web-raw", sync_archived_chatgpt_web(manifest, args.min_messages, args.rewrite_paths, project_map))
    if "claude-web" in args.sources:
        merge_stats(stats, "claude-web", sync_claude_web(manifest, args.min_messages, args.rewrite_paths, project_map))
        if args.reimport_web_raw:
            merge_stats(stats, "claude-web-raw", sync_archived_claude_web(manifest, args.min_messages, args.rewrite_paths, project_map))

    save_manifest(manifest)
    save_project_map(project_map)
    return stats


def parse_sources(value: str) -> list[str]:
    if value == "all":
        return ["claude-code", "codex-local", "chatgpt-web", "claude-web"]
    sources = [part.strip() for part in value.split(",") if part.strip()]
    unknown = sorted(set(sources) - set(SOURCE_DIRS))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown source(s): {', '.join(unknown)}")
    return sources


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync AI chat histories into ai_chat_history.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT, help="Directory where inbox/raw/state/markdow_history are stored.")
        subparser.add_argument("--sources", type=parse_sources, default=parse_sources("all"), help="all or comma list: claude-code,codex-local,chatgpt-web,claude-web")
        subparser.add_argument("--claude-root", type=Path, default=DEFAULT_CLAUDE_ROOT, help="Claude Code projects root.")
        subparser.add_argument("--codex-root", type=Path, default=DEFAULT_CODEX_ROOT, help="Codex sessions root.")
        subparser.add_argument("--web-drop-root", type=Path, default=PROJECT_ROOT, help="Directory to scan for newly dropped ChatGPT/Claude web exports.")
        subparser.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOADS, help="Browser Downloads directory.")
        subparser.add_argument("--move-web-exports", action="store_true", help="Move ChatGPT/Claude web exports from Downloads into inbox before syncing.")
        subparser.add_argument("--reimport-web-raw", action="store_true", help="Re-read archived raw web exports to rebuild Markdown paths and project metadata.")
        subparser.add_argument("--min-messages", type=int, default=2, help="Minimum user+assistant messages required before writing Markdown.")
        subparser.add_argument("--rewrite-paths", action="store_true", help="Recompute Markdown filenames from the current title and remove the old generated path.")

    sync_parser = subparsers.add_parser("sync", help="Run one sync pass.")
    add_common(sync_parser)

    watch_parser = subparsers.add_parser("watch", help="Poll and sync continuously.")
    add_common(watch_parser)
    watch_parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds.")

    init_parser = subparsers.add_parser("init", help="Create archive directories only.")
    add_common(init_parser)

    receive_parser = subparsers.add_parser("receive-chatgpt", help="Run a local HTTP receiver for ChatGPT web auto-sync.")
    add_common(receive_parser)
    receive_parser.add_argument("--host", default=DEFAULT_RECEIVE_HOST, help="Receiver host.")
    receive_parser.add_argument("--port", type=int, default=DEFAULT_RECEIVE_PORT, help="Receiver port.")
    receive_parser.add_argument("--token", default="", help="Shared secret required in X-Local-Sync-Token.")
    receive_parser.add_argument("--token-file", type=Path, default=None, help="Optional path to read or create the receiver token.")
    receive_parser.add_argument("--no-token", action="store_true", help="Disable receiver token checks.")
    receive_parser.add_argument("--sync-after-receive", action="store_true", help="Run one import pass after each received export.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if hasattr(args, "archive_root"):
        configure_archive_root(args.archive_root)
    if args.command == "init":
        ensure_dirs()
        save_project_map(load_project_map())
        print(f"Initialized {ARCHIVE_ROOT}")
        return 0
    if args.command == "sync":
        print_stats(sync_once(args))
        return 0
    if args.command == "watch":
        ensure_dirs()
        print(f"Watching every {args.interval}s. Press Ctrl-C to stop.", flush=True)
        while True:
            try:
                print_stats(sync_once(args))
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("Stopped.")
                return 0
    if args.command == "receive-chatgpt":
        return run_receive_server(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
