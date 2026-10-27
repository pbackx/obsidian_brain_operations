#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import freewrite_ingest
import vault_tool


class TestVaultToolAndFreewrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "TODO" / "Archive").mkdir(parents=True)
        (self.vault / "Home automation" / "TODO").mkdir(parents=True)
        (self.vault / "Freewrite").mkdir(parents=True)
        (self.vault / "Brain_Operations").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_only_picks_up_ai_queue_and_sorts_by_priority(self):
        # 1. Backlog task (must be ignored)
        (self.vault / "TODO" / "Backlog Task.md").write_text(
            "---\ntype: task\nstatus: backlog\ncategory: general\npriority: normal\n---\n\n# Backlog Task\n",
            encoding="utf-8",
        )
        # 2. Normal ai-queue task
        (self.vault / "TODO" / "Normal AI Task.md").write_text(
            "---\ntype: task\nstatus: ai-queue\ncategory: general\npriority: normal\n---\n\n# Normal AI Task\n",
            encoding="utf-8",
        )
        # 3. High-priority ai-queue task in domain folder
        (self.vault / "Home automation" / "TODO" / "Urgent HA Task.md").write_text(
            "---\ntype: task\nstatus: ai-queue\ncategory: home-automation\npriority: high\n---\n\n# Urgent HA Task\n",
            encoding="utf-8",
        )
        # 4. Archived task with status: ai-queue inside TODO/Archive (must be ignored)
        (self.vault / "TODO" / "Archive" / "Old Task.md").write_text(
            "---\ntype: task\nstatus: ai-queue\ncategory: general\npriority: high\n---\n\n# Old Task\n",
            encoding="utf-8",
        )

        tasks = vault_tool.scan_vault(self.vault)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["file"], "Home automation/TODO/Urgent HA Task.md")
        self.assertEqual(tasks[0]["priority"], 12)
        self.assertEqual(tasks[1]["file"], "TODO/Normal AI Task.md")
        self.assertEqual(tasks[1]["priority"], 5)

    def test_update_task_note_in_place_without_duplicate_callouts(self):
        rel_path = "TODO/Test Task.md"
        task_path = self.vault / rel_path
        task_path.write_text(
            "---\ntype: task\nstatus: ai-queue\ncategory: general\npriority: normal\ncreated: 2026-10-27\nupdated: 2026-10-27\n---\n\n"
            "# Test Task\n\n## Notes & Remarks\n- Initial user remark\n",
            encoding="utf-8",
        )

        # First update: needs-clarification
        ok = vault_tool.update_task_note(
            self.vault,
            rel_path,
            new_status="needs-clarification",
            summary_text="Completed phase 1 analysis.",
            question_text="Which option do you prefer?",
        )
        self.assertTrue(ok)
        text1 = task_path.read_text(encoding="utf-8")
        fm1, _, body1 = vault_tool.parse_frontmatter(text1)
        self.assertEqual(fm1["status"], "needs-clarification")
        self.assertEqual(fm1["summary"], "Completed phase 1 analysis.")
        self.assertEqual(fm1["question"], "Which option do you prefer?")
        self.assertIn("> [!question] Latest AI Update", body1)
        self.assertIn("- Initial user remark", body1)

        # Second update: completed (must replace callout and remove stale question from frontmatter)
        ok2 = vault_tool.update_task_note(
            self.vault,
            rel_path,
            new_status="completed",
            summary_text="Implemented Option A.",
        )
        self.assertTrue(ok2)
        text2 = task_path.read_text(encoding="utf-8")
        fm2, _, body2 = vault_tool.parse_frontmatter(text2)
        self.assertEqual(fm2["status"], "completed")
        self.assertEqual(fm2["summary"], "Implemented Option A.")
        self.assertNotIn("question", fm2)
        self.assertEqual(body2.count("Latest AI Update"), 1)
        self.assertIn("> [!success] Latest AI Update", body2)

    def test_archive_tasks_moves_to_todo_archive(self):
        task_path = self.vault / "Home automation" / "TODO" / "Finished Task.md"
        task_path.write_text(
            "---\ntype: task\nstatus: archived\ncategory: home-automation\n---\n\n# Finished Task\n",
            encoding="utf-8",
        )
        moved = vault_tool.archive_tasks(self.vault)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["to"], "TODO/Archive/Finished Task.md")
        self.assertFalse(task_path.exists())
        self.assertTrue((self.vault / "TODO" / "Archive" / "Finished Task.md").exists())

    def test_freewrite_ingest_creates_task_and_handles_followup(self):
        postbox = Path(self.tmp.name) / "postbox"
        (postbox / "B").mkdir(parents=True)
        draft_file = postbox / "B" / "2026-10-27 Brainstorm idea.txt"
        draft_file.write_text("Brainstorm idea\n\nCan you research local voice assistants?", encoding="utf-8")

        results = freewrite_ingest.ingest_directory(
            source_dir=postbox,
            vault_dir=self.vault,
            folders_filter={"B"},
            ai_folders={"B"},
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "ai-queue")
        vault_note = self.vault / results[0]["vault_file"]
        self.assertTrue(vault_note.exists())

        # Verify vault_tool.py scan discovers the Freewrite note
        queued = vault_tool.scan_vault(self.vault)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["file"], results[0]["vault_file"])

        # Simulate agent marking it needs-clarification
        vault_tool.update_task_note(
            self.vault,
            results[0]["vault_file"],
            new_status="needs-clarification",
            summary_text="Evaluated Piper and Whisper.",
            question_text="Do you want to deploy on Proxmox?",
        )
        self.assertEqual(len(vault_tool.scan_vault(self.vault)), 0)

        # Simulate user adding a follow-up paragraph on the Freewrite typewriter
        draft_file.write_text(
            "Brainstorm idea\n\nCan you research local voice assistants?\n\nYes, please deploy on Proxmox.",
            encoding="utf-8",
        )
        results2 = freewrite_ingest.ingest_directory(
            source_dir=postbox,
            vault_dir=self.vault,
            folders_filter={"B"},
            ai_folders={"B"},
        )
        self.assertEqual(len(results2), 1)
        self.assertEqual(results2[0]["action"], "updated_followup")
        # Must be back in ai-queue!
        queued2 = vault_tool.scan_vault(self.vault)
        self.assertEqual(len(queued2), 1)
        updated_text = vault_note.read_text(encoding="utf-8")
        self.assertIn("## Freewrite Follow-Up", updated_text)
        self.assertIn("Yes, please deploy on Proxmox.", updated_text)


if __name__ == "__main__":
    unittest.main()
