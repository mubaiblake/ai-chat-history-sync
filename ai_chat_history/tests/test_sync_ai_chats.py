import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import ai_chat_history.sync_ai_chats as sync_mod
from ai_chat_history.sync_ai_chats import (
    ChatMessage,
    Conversation,
    build_web_source_index,
    build_markdown_path,
    configure_archive_root,
    migrate_manifest_markdown_paths,
    move_web_exports_from_directory,
    normalize_markdown_path,
    project_config_for,
    parse_chatgpt_conversation,
    parse_claude_code_file,
    parse_codex_file,
    tavern_jsonl_to_conversation,
)


class SyncAiChatsTests(unittest.TestCase):
    def test_chatgpt_current_path_extracts_dialogue(self):
        raw = {
            "id": "conv-1",
            "title": "Test Chat",
            "create_time": 1778475481.0,
            "update_time": 1778475581.0,
            "current_node": "a2",
            "mapping": {
                "root": {"id": "root", "parent": None, "message": None},
                "u1": {
                    "id": "u1",
                    "parent": "root",
                    "message": {
                        "author": {"role": "user"},
                        "create_time": 1778475482.0,
                        "content": {"content_type": "text", "parts": ["hello"]},
                    },
                },
                "a2": {
                    "id": "a2",
                    "parent": "u1",
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 1778475483.0,
                        "content": {"content_type": "text", "parts": ["hi"]},
                    },
                },
            },
        }

        conversation = parse_chatgpt_conversation(raw, Path("raw.json"))

        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.conversation_id, "conv-1")
        self.assertEqual([message.role for message in conversation.messages], ["user", "assistant"])
        self.assertEqual([message.text for message in conversation.messages], ["hello", "hi"])

    def test_claude_filters_command_only_records(self):
        rows = [
            {
                "type": "user",
                "message": {"role": "user", "content": "<command-name>/clear</command-name>"},
                "sessionId": "s1",
                "timestamp": "2026-05-11T00:00:00Z",
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "真实问题"},
                "sessionId": "s1",
                "timestamp": "2026-05-11T00:00:01Z",
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "真实回答"},
                "sessionId": "s1",
                "timestamp": "2026-05-11T00:00:02Z",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "s1.jsonl"
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            conversation = parse_claude_code_file(path)

        self.assertEqual([message.text for message in conversation.messages], ["真实问题", "真实回答"])
        self.assertTrue(conversation.has_real_dialogue(min_messages=2))

    def test_codex_reads_response_items_only(self):
        rows = [
            {"type": "session_meta", "payload": {"id": "c1", "timestamp": "2026-05-11T00:00:00Z", "cwd": "/tmp/project"}},
            {
                "timestamp": "2026-05-11T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "internal"}],
                },
            },
            {
                "timestamp": "2026-05-11T00:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "question",
                },
            },
            {
                "timestamp": "2026-05-11T00:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "answer",
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "c1.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            conversation = parse_codex_file(path)

        self.assertEqual(conversation.conversation_id, "c1")
        self.assertEqual([message.text for message in conversation.messages], ["question", "answer"])

    def test_empty_conversation_is_not_real_dialogue(self):
        conversation = Conversation(
            source="claude-code",
            conversation_id="empty",
            title="empty",
            messages=[ChatMessage(role="user", text="only one side")],
        )

        self.assertFalse(conversation.has_real_dialogue(min_messages=2))

    def test_markdown_paths_live_under_markdow_history(self):
        conversation = Conversation(
            source="chatgpt-web",
            conversation_id="conv-1",
            title="hello",
            created_at="2026-05-11T00:00:00Z",
            messages=[
                ChatMessage(role="user", text="hello"),
                ChatMessage(role="assistant", text="hi"),
            ],
        )

        path = build_markdown_path(conversation)

        self.assertIn("markdow_history/chatgpt-web/global/2026-05", path.as_posix())

    def test_chatgpt_project_path_uses_project_map_dir(self):
        raw = {
            "id": "conv-1",
            "title": "Project Chat",
            "create_time": 1778475481.0,
            "conversation_template_id": "g-p-123",
            "gizmo_id": "g-p-123",
            "current_node": "a2",
            "mapping": {
                "u1": {
                    "id": "u1",
                    "parent": None,
                    "message": {
                        "author": {"role": "user"},
                        "create_time": 1778475482.0,
                        "content": {"content_type": "text", "parts": ["hello"]},
                    },
                },
                "a2": {
                    "id": "a2",
                    "parent": "u1",
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 1778475483.0,
                        "content": {"content_type": "text", "parts": ["hi"]},
                    },
                },
            },
        }
        project_map = {
            "chatgpt-web": {
                "global_dir": "global",
                "projects": {"g-p-123": {"name": "My Project", "dir": "custom-project"}},
            }
        }

        conversation = parse_chatgpt_conversation(raw, Path("raw.json"))
        path = build_markdown_path(conversation, project_map=project_map)

        self.assertEqual(conversation.metadata["project_id"], "g-p-123")
        self.assertIn("markdow_history/chatgpt-web/custom-project/2026-05", path.as_posix())

    def test_project_map_name_replaces_default_id_dir(self):
        conversation = Conversation(
            source="chatgpt-web",
            conversation_id="conv-1",
            title="hello",
            created_at="2026-05-11T00:00:00Z",
            messages=[
                ChatMessage(role="user", text="hello"),
                ChatMessage(role="assistant", text="hi"),
            ],
            project="g-p-123",
            metadata={"project_id": "g-p-123"},
        )
        project_map = {
            "chatgpt-web": {
                "global_dir": "global",
                "projects": {"g-p-123": {"name": "开发工作", "dir": "g-p-123"}},
            }
        }

        config = project_config_for(conversation, project_map)

        self.assertEqual(config["name"], "开发工作")
        self.assertEqual(config["dir"], "开发工作")

    def test_legacy_source_paths_are_mapped_to_markdow_history(self):
        self.assertEqual(
            normalize_markdown_path("claude-code/2026-05/chat.md"),
            "markdow_history/claude-code/2026-05/chat.md",
        )
        self.assertEqual(
            normalize_markdown_path("markdow_history/codex-local/2026-05/chat.md"),
            "markdow_history/codex-local/2026-05/chat.md",
        )

    def test_manifest_migration_updates_legacy_paths(self):
        manifest = {
            "items": {
                "chatgpt-web:conv-1": {
                    "markdown_path": "chatgpt-web/2026-05/chat.md",
                }
            }
        }

        migrated = migrate_manifest_markdown_paths(manifest)

        self.assertEqual(migrated, 0)
        self.assertEqual(
            manifest["items"]["chatgpt-web:conv-1"]["markdown_path"],
            "chatgpt-web/2026-05/chat.md",
        )

    def test_dropped_chatgpt_export_moves_to_inbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            drop_root = root / "drop"
            inbox_root = root / "inbox"
            drop_root.mkdir()
            export = drop_root / "chatgpt-export.json"
            export.write_text(json.dumps([{"id": "conv-1"}]), encoding="utf-8")

            moved = move_web_exports_from_directory(drop_root, {"raw_files": {}}, inbox_root=inbox_root)

            self.assertEqual(moved, [inbox_root / "chatgpt-web" / "chatgpt-export.json"])
            self.assertFalse(export.exists())
            self.assertTrue((inbox_root / "chatgpt-web" / "chatgpt-export.json").exists())

    def test_dropped_chatgpt_jsonl_export_moves_to_inbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            drop_root = root / "drop"
            inbox_root = root / "inbox"
            drop_root.mkdir()
            export = drop_root / "ChatGPT-Test.tavern.jsonl"
            export.write_text(json.dumps({"user_name": "You"}) + "\n", encoding="utf-8")

            moved = move_web_exports_from_directory(drop_root, {"raw_files": {}}, inbox_root=inbox_root)

            self.assertEqual(moved, [inbox_root / "chatgpt-web" / "ChatGPT-Test.tavern.jsonl"])
            self.assertFalse(export.exists())
            self.assertTrue((inbox_root / "chatgpt-web" / "ChatGPT-Test.tavern.jsonl").exists())

    def test_duplicate_dropped_chatgpt_export_moves_to_processed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            drop_root = root / "drop"
            inbox_root = root / "inbox"
            drop_root.mkdir()
            export = drop_root / "ChatGPT-Test.json"
            content = json.dumps([{"id": "conv-1"}]).encode("utf-8")
            export.write_bytes(content)
            file_hash = hashlib.sha256(content).hexdigest()

            moved = move_web_exports_from_directory(drop_root, {"raw_files": {file_hash: "raw/chatgpt-web/existing.json"}}, inbox_root=inbox_root)

            self.assertEqual(moved, [inbox_root / "chatgpt-web" / "processed" / "ChatGPT-Test.json"])
            self.assertFalse(export.exists())
            self.assertTrue((inbox_root / "chatgpt-web" / "processed" / "ChatGPT-Test.json").exists())

    def test_repo_claude_md_is_not_treated_as_web_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            drop_root = root / "drop"
            inbox_root = root / "inbox"
            drop_root.mkdir()
            guide = drop_root / "CLAUDE.md"
            guide.write_text("# instructions\n", encoding="utf-8")

            moved = move_web_exports_from_directory(drop_root, {"raw_files": {}}, inbox_root=inbox_root)

            self.assertEqual(moved, [])
            self.assertTrue(guide.exists())

    def test_tavern_jsonl_rows_parse_as_one_conversation(self):
        rows = [
            {"user_name": "You", "character_name": "Assistant"},
            {"name": "You", "is_user": True, "send_date": 1778544125.93, "mes": "第一个问题"},
            {"name": "Assistant", "is_user": False, "send_date": 1778544126.82, "mes": "第一个回答"},
            {"name": "You", "is_user": True, "send_date": 1778544581.30, "mes": "第二个问题"},
        ]

        conversation = tavern_jsonl_to_conversation(rows, "chatgpt-web", Path("ChatGPT-Test.tavern.jsonl"))

        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.source, "chatgpt-web")
        self.assertEqual(conversation.title, "第一个问题")
        self.assertEqual([message.role for message in conversation.messages], ["user", "assistant", "user"])
        self.assertEqual([message.text for message in conversation.messages], ["第一个问题", "第一个回答", "第二个问题"])
        self.assertEqual(conversation.metadata["import_mode"], "tavern_jsonl")

    def test_received_chatgpt_payload_writes_to_inbox(self):
        old_archive_root = sync_mod.ARCHIVE_ROOT
        old_project_map_path = sync_mod.PROJECT_MAP_PATH
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                sync_mod.ARCHIVE_ROOT = root
                sync_mod.PROJECT_MAP_PATH = root / "state" / "project_map.json"
                target = sync_mod.write_received_web_export(
                    "chatgpt-web",
                    {
                        "filename": "../bad/name?.json",
                        "conversations": [{"id": "conv-1"}],
                    },
                )

                self.assertEqual(target.parent, root / "inbox" / "chatgpt-web")
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), [{"id": "conv-1"}])
                self.assertNotIn("..", target.name)
        finally:
            sync_mod.ARCHIVE_ROOT = old_archive_root
            sync_mod.PROJECT_MAP_PATH = old_project_map_path

    def test_web_source_index_reads_manifest_and_raw_json(self):
        old_archive_root = sync_mod.ARCHIVE_ROOT
        old_project_root = sync_mod.PROJECT_ROOT
        old_manifest_path = sync_mod.MANIFEST_PATH
        old_project_map_path = sync_mod.PROJECT_MAP_PATH
        old_token_path = sync_mod.DEFAULT_RECEIVE_TOKEN_PATH
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                configure_archive_root(root)
                (root / "state").mkdir(parents=True)
                (root / "raw" / "chatgpt-web").mkdir(parents=True)
                sync_mod.save_manifest({
                    "version": 1,
                    "raw_files": {},
                    "items": {
                        "chatgpt-web:conv-1": {
                            "source": "chatgpt-web",
                            "conversation_id": "conv-1",
                            "title": "From manifest",
                            "updated_at": "2026-05-18T00:00:00Z",
                            "markdown_path": "markdow_history/chatgpt-web/global/2026-05/conv-1.md",
                        }
                    },
                })
                raw_path = root / "raw" / "chatgpt-web" / "batch.json"
                raw_path.write_text(json.dumps([
                    {
                        "id": "conv-2",
                        "title": "From raw",
                        "create_time": 1779050000,
                        "update_time": 1779050100,
                    }
                ]), encoding="utf-8")

                index = build_web_source_index("chatgpt-web")

                self.assertEqual(index["conv-1"]["title"], "From manifest")
                self.assertEqual(index["conv-2"]["title"], "From raw")
                self.assertGreater(index["conv-2"]["updated_ms"], 0)
        finally:
            sync_mod.ARCHIVE_ROOT = old_archive_root
            sync_mod.PROJECT_ROOT = old_project_root
            sync_mod.MANIFEST_PATH = old_manifest_path
            sync_mod.PROJECT_MAP_PATH = old_project_map_path
            sync_mod.DEFAULT_RECEIVE_TOKEN_PATH = old_token_path


if __name__ == "__main__":
    unittest.main()
