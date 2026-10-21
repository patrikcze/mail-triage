"""./triage apply and undo with a fake mail program that has folders. All data is synthetic."""

from dataclasses import replace
import io
from contextlib import redirect_stdout
import unittest
from unittest import mock

from helpers import FakeConnector, FakeRouter, TempHome, default_config, eml

from mail_triage import actions, cli, config as configmod
from mail_triage.connectors.base import ConnectorError, Mailbox, Mover
from mail_triage.engine import Engine, ScanOptions
from mail_triage.store import Store


SALE = [eml("Deals <deals@gadgets.example.com>", f"Big sale #{i}", "Huge discount today, shop now!",
            List_Unsubscribe="<mailto:u@gadgets.example.com>") for i in range(3)]
PERSONAL = eml("Mom <mom@family.example.org>", "Sunday lunch", "Are you coming on Sunday?")
INBOX = Mailbox("acct", "INBOX")


class FakeMail(FakeConnector):
    """Mail program with folders. A moved message gets a new native id, as in Apple Mail."""

    def __init__(self, sources):
        super().__init__(sources)
        self.boxes = {"INBOX": list(self.infos)}
        self.created = []

    def list(self, mailbox):
        if mailbox.name not in self.boxes:
            raise ConnectorError("no such mailbox")
        return list(self.boxes[mailbox.name])

    def ensure_folder(self, account, folder):
        if folder in self.boxes:
            return False
        self.boxes[folder] = []
        self.created.append(folder)
        return True

    def move(self, mailbox, items, folder):
        if folder not in self.boxes:
            raise ConnectorError("no such mailbox")
        moved = []
        for native_id, message_id in items:
            info = next((i for i in self.boxes[mailbox.name] if i.native_id == native_id), None)
            if info is None or info.message_id != message_id:
                continue
            self.boxes[mailbox.name].remove(info)
            self.boxes[folder].append(replace(info, native_id=f"{folder}#{native_id}"))
            moved.append(native_id)
        return moved

    def subjects(self, name):
        return sorted(info.subject for info in self.boxes.get(name, []))


class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.folder = self.home.__enter__()
        self.store = Store(self.folder / "history.sqlite")
        self.mail = FakeMail(SALE + [PERSONAL])

    def tearDown(self):
        self.store.close()
        self.home.__exit__(None, None, None)

    def scan(self, config=None, mode="new"):
        Engine(config or default_config(), self.store, self.mail, FakeRouter, out=lambda _: None).scan(ScanOptions(mode=mode))

    def plan(self, config=None, mailbox=INBOX):
        return actions.plan(config or default_config(), self.store, self.mail, mailbox)

    def apply(self, config=None):
        plan = self.plan(config)
        return plan, actions.apply(self.store, self.mail, [plan], run_id=1, out=lambda _: None)

    def test_fake_is_a_mover_and_mail_sources_without_moving_are_not(self):
        self.assertIsInstance(self.mail, Mover)
        self.assertNotIsInstance(FakeConnector([]), Mover)

    def test_junk_moves_and_personal_stays(self):
        self.scan()
        plan = self.plan()
        self.assertEqual(list(plan.by_folder()), ["Laya/Promotions"])
        self.assertEqual(len(plan.moves), 3)
        self.assertEqual(plan.stay, {"personal": 1})

    def test_apply_creates_folder_once_moves_and_journals(self):
        self.scan()
        _, counts = self.apply()
        self.assertEqual((counts["moved"], counts["folders_created"]), (3, 1))
        self.assertEqual(self.mail.created, ["Laya/Promotions"])
        self.assertEqual(self.mail.subjects("INBOX"), ["Sunday lunch"])
        self.assertEqual(len(self.mail.subjects("Laya/Promotions")), 3)
        statuses = [row["status"] for row in self.store.db.execute("SELECT status FROM actions")]
        self.assertEqual(statuses, ["applied"] * 3)
        self.assertEqual(self.plan().moves, [])          # idempotent: nothing left to move

    def test_existing_folder_is_reused(self):
        self.mail.boxes["Laya/Promotions"] = []
        self.scan()
        _, counts = self.apply()
        self.assertEqual((counts["moved"], counts["folders_created"], self.mail.created), (3, 0, []))

    def test_undo_moves_back_and_apply_respects_it(self):
        self.scan()
        self.apply()
        counts = actions.undo(self.store, self.mail)
        self.assertEqual(counts["moved_back"], 3)
        self.assertEqual(len(self.mail.subjects("INBOX")), 4)
        plan = self.plan()
        self.assertEqual((plan.moves, plan.skipped[actions.MOVED_BEFORE]), ([], 3))
        self.assertEqual(actions.undo(self.store, self.mail)["moved_back"], 0)

    def test_redecided_after_undo_moves_again(self):
        self.scan()
        self.apply()
        actions.undo(self.store, self.mail)
        self.store.db.execute("UPDATE actions SET created_at = '2000-01-01T00:00:00+00:00'")
        self.scan(mode="all")
        self.assertEqual(len(self.plan().moves), 3)

    def test_keep_in_place_overrides_a_folder(self):
        base = default_config()
        cats = tuple(replace(c, folder="Laya/Personal") if c.name == "personal" else c for c in base.categories)
        config = default_config(categories=cats)
        self.scan(config)
        self.assertEqual(self.plan(config).stay, {"personal": 1})
        moving = default_config(categories=cats, actions={**base.actions, "keep_in_place": []})
        self.assertIn("Laya/Personal", self.plan(moving).by_folder())

    def test_review_goes_to_the_review_folder(self):
        self.store.teach("domain", "gadgets.example.com", "review")
        self.scan()
        self.assertEqual(list(self.plan().by_folder()), ["Laya/Review"])

    def test_stale_and_undecided_mail_is_not_moved(self):
        self.assertEqual(self.plan().skipped[actions.NOT_DECIDED], 4)
        self.scan()
        changed = default_config(instructions={**default_config().instructions, "kind": "Which type?"})
        plan = self.plan(changed)
        self.assertEqual((plan.moves, plan.skipped[actions.STALE]), ([], 4))

    def test_mail_the_user_moved_elsewhere_is_left_alone(self):
        self.scan()
        self.mail.boxes["Projects"] = self.mail.boxes.pop("INBOX")
        plan = self.plan(mailbox=Mailbox("acct", "Projects"))
        self.assertEqual((plan.moves, plan.skipped[actions.ELSEWHERE]), ([], 3))

    def test_changed_message_is_not_moved(self):
        self.scan()
        plan = self.plan()
        first = self.mail.boxes["INBOX"][0]
        self.mail.boxes["INBOX"][0] = replace(first, message_id="<other@test.example>")
        counts = actions.apply(self.store, self.mail, [plan], run_id=1, out=lambda _: None)
        self.assertEqual((counts["moved"], counts["not_moved"]), (2, 1))
        self.assertIn(first.subject, self.mail.subjects("INBOX"))
        failed = self.store.db.execute("SELECT COUNT(*) FROM actions WHERE status = 'failed'").fetchone()[0]
        self.assertEqual(failed, 1)

    def test_mail_without_message_id_is_not_moved(self):
        self.mail.infos = [replace(i, message_id="") for i in self.mail.infos]
        self.mail.boxes["INBOX"] = list(self.mail.infos)
        self.scan()
        plan = self.plan()
        self.assertEqual((plan.moves, plan.skipped[actions.NO_ID]), ([], 3))

    def test_interrupted_move_is_cleaned_up_by_undo(self):
        self.scan()
        move = self.plan().moves[0]
        self.store.journal(move.key, "INBOX", move.folder, run_id=1)
        self.store.commit()
        self.mail.boxes["Laya/Promotions"] = []
        self.assertEqual(actions.undo(self.store, self.mail)["moved_back"], 0)
        status = self.store.db.execute("SELECT status FROM actions").fetchone()[0]
        self.assertEqual(status, "failed")

    def test_mail_program_error_stops_and_marks_failed(self):
        self.scan()
        plan = self.plan()
        with mock.patch.object(self.mail, "move", side_effect=ConnectorError("Mail returned error 1")):
            with self.assertRaises(ConnectorError):
                actions.apply(self.store, self.mail, [plan], run_id=1, out=lambda _: None)
        statuses = {row[0] for row in self.store.db.execute("SELECT status FROM actions")}
        self.assertEqual(statuses, {"failed"})
        self.assertEqual(len(self.mail.subjects("INBOX")), 4)


class ConfigGuardTest(unittest.TestCase):
    def load(self, text):
        with TempHome() as home:
            path = home / "config.toml"
            path.write_text(text)
            return configmod.load(path)

    def test_moving_is_off_by_default_and_keeps_personal_mail(self):
        config = default_config()
        self.assertFalse(config.actions["enabled"])
        self.assertEqual(config.actions["keep_in_place"], ["personal", "work"])
        folders = actions.destinations(config)
        for name in ("personal", "work", "transactional", "notification"):
            self.assertEqual(folders[name], "", name)
        self.assertEqual(folders["review"], "Laya/Review")

    def test_enabled_refuses_unsafe_folders(self):
        self.assertTrue(self.load("[actions]\nenabled = true\n").actions["enabled"])
        for folder in ("Trash", "Other/Promo", "Laya/../Trash"):
            text = (f'[actions]\nenabled = true\n[policy]\nreview_folder = "{folder}"\n')
            with self.assertRaises(configmod.ConfigError, msg=folder):
                self.load(text)
        with self.assertRaises(configmod.ConfigError):
            self.load("[actions]\nenabled = \"yes\"\n")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.folder = self.home.__enter__()
        self.mail = FakeMail(SALE + [PERSONAL])
        store = Store(self.folder / "history.sqlite")
        Engine(default_config(), store, self.mail, FakeRouter, out=lambda _: None).scan(ScanOptions())
        store.close()

    def tearDown(self):
        self.home.__exit__(None, None, None)

    def run_cli(self, *argv, enabled=True):
        path = self.folder / "config.toml"
        path.write_text(f"[actions]\nenabled = {'true' if enabled else 'false'}\n")
        out = io.StringIO()
        with mock.patch.object(cli, "_connector", return_value=self.mail), \
                mock.patch.object(cli.privacy, "install_network_guard"), redirect_stdout(out):
            code = cli.main(["--config", str(path), *argv])
        return code, out.getvalue()

    def test_dry_run_and_disabled_never_move(self):
        code, out = self.run_cli("apply", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Laya/Promotions", out)
        self.assertEqual(self.run_cli("apply", "--yes", enabled=False)[0], 1)
        self.assertEqual(len(self.mail.subjects("INBOX")), 4)

    def test_without_confirmation_nothing_moves(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(self.run_cli("apply")[0], 1)
        self.assertEqual(len(self.mail.subjects("INBOX")), 4)

    def test_limit_moves_only_a_few(self):
        code, out = self.run_cli("apply", "--yes", "--limit", "1")
        self.assertIn("Moved 1 emails", out)
        self.assertEqual(len(self.mail.subjects("INBOX")), 3)
        self.assertIn("Moved 2 emails", self.run_cli("apply", "--yes")[1])

    def test_apply_then_undo(self):
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("Moved 3 emails", out)
        self.assertEqual(self.mail.subjects("INBOX"), ["Sunday lunch"])
        code, out = self.run_cli("undo", "--yes")
        self.assertIn("Moved 3 emails back", out)
        self.assertEqual(len(self.mail.subjects("INBOX")), 4)

    def test_sources_that_cannot_move_are_refused(self):
        path = self.folder / "config.toml"
        path.write_text("[actions]\nenabled = true\n")
        out = io.StringIO()
        with mock.patch.object(cli, "_connector", return_value=FakeConnector([])), redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(path), "apply", "--yes"]), 1)
        self.assertIn("cannot move mail", out.getvalue())


if __name__ == "__main__":
    unittest.main()
