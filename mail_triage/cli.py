"""Command line: ./triage <command>. Run ./triage help for the list."""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from . import __version__, actions, config as configmod, privacy, provision, report
from .backends import BackendError, create
from .config import ConfigError
from .connectors.base import ConnectorError, Mover
from .engine import Engine, ScanOptions
from .store import Store


def _connector(args):
    if args.folder:
        from .connectors.emlfolder import EmlFolderConnector
        return EmlFolderConnector(Path(args.folder))
    if getattr(args, "source", "applemail") == "outlook":
        from .connectors.outlook import create_outlook
        return create_outlook()
    from .connectors.applemail import AppleMailConnector
    return AppleMailConnector()


def _open(path: Path) -> None:
    opener = {"darwin": ["open"], "win32": ["cmd", "/c", "start", ""]}.get(sys.platform, ["xdg-open"])
    subprocess.run([*opener, str(path)], check=False)


def _ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes", "a", "ano")


# -- commands ----------------------------------------------------------------------------------

def cmd_setup(args) -> int:
    home = privacy.private_dir(configmod.home())
    target = home / "config.toml"
    if not target.exists():
        shutil.copyfile(configmod.DEFAULT_FILE, target)
        privacy.private_file(target)
        print(f"Created your settings file: {target}")
    else:
        print(f"Settings file: {target} (kept as is)")
    config = configmod.load(target)
    backend = args.backend or config.model["backend"]
    if backend == "auto":
        backend = "mlx" if provision.mlx_supported() is None else "systemone"

    if backend == "mlx":
        mlx = config.model["mlx"]
        aliases = [a for a in (mlx["english"], mlx.get("multilingual", "")) if a]
        unknown = [a for a in aliases if a not in provision.MODELS]
        if unknown:
            print(f"Unknown MLX model {unknown[0]!r}; known: {', '.join(provision.MODELS)}")
            return 1
        missing = [a for a in aliases if provision.find_installed(a) is None] + (
            ["runtime"] if provision.find_runtime() is None else [])
        if missing:
            print("\nLaya MLX (runs on this Mac's GPU) needs a one-time download from Hugging Face:")
            print("  runtime ≈ 300 MB" if "runtime" in missing else "", *(f"  {a} ≈ 700-850 MB" for a in missing if a != "runtime"), sep="\n")
            print("Only model files are downloaded; no email data is involved.")
            if not _ask("Download now?", args.yes):
                print("Skipped. Run ./triage setup again when ready, or use a llama.cpp server (see README).")
                return 1
            try:
                provision.install(aliases, args.base_python)
            except (provision.SetupError, subprocess.CalledProcessError, OSError) as exc:
                print(f"Setup failed: {exc}")
                return 1
        print("✓ Laya MLX is installed")
    else:
        url = config.model["systemone"]["url"]
        print("\nThis computer will use a local llama.cpp (or Ollama) server. Start it in another window, e.g.:")
        print(f"  llama serve -hf ggml-org/Laya-GGUF:Q8_0 --host 127.0.0.1 --port {url.rsplit(':', 1)[-1].strip('/')}")
        print(f"and set  backend = \"systemone\"  in {target} if it is not already.")

    privacy.install_network_guard()      # Python/model traffic stays local; mail clients sync independently
    try:
        router = create(configmod.load(target))
        print(f"✓ model ready: {router.label}")
        router.close()
    except BackendError as exc:
        print(f"! model not ready yet: {exc}")
    if args.source == "outlook" and not args.folder:
        try:
            boxes = _connector(args).mailboxes()
            print(f"✓ outlook: discovery works ({len(boxes)} inboxes); body access not tested")
        except ConnectorError as exc:
            print(f"! {exc}")
        print("\nNext: ./triage scan --source outlook")
        return 0
    if getattr(args, "skip_mail_check", False):
        return 0
    if sys.platform == "darwin" and not args.folder:
        try:
            boxes = _connector(args).mailboxes()
            print(f"✓ Apple Mail access works ({len(boxes)} inboxes)")
        except ConnectorError as exc:
            print(f"! {exc}")
    print("\nNext: ./triage scan   (or double-click Triage.command)")
    return 0


def cmd_doctor(args) -> int:
    privacy.install_network_guard()
    ok = True
    print(f"mail-triage {__version__} · Python {platform.python_version()} · {platform.system()} {platform.machine()}")
    print(f"provider: {'emlfolder' if args.folder else args.source}; OS: {platform.system()}")
    home = configmod.home()
    print(f"data folder: {home}")
    try:
        config = configmod.load(Path(args.config) if args.config else None)
        print(f"✓ settings valid ({config.path or 'built-in defaults'}), fingerprint {config.fingerprint()}")
        print(f"  {len(config.categories)} categories, {len(config.rules)} rules, backend = {config.model['backend']}, "
              f"console = {config.privacy['console']}")
    except ConfigError as exc:
        print(f"✗ settings: {exc}")
        return 1
    try:
        router = create(config)
        print(f"✓ model: {router.label}")
        router.close()
    except BackendError as exc:
        ok = False
        print(f"✗ model: {exc}")
    if args.source == "outlook" and not args.folder:
        try:
            ready, lines = _connector(args).diagnose(accounts=args.account or (), read_sample=args.probe_read)
            for line in lines:
                print(f"  {line}")
            ok = ok and ready
        except ConnectorError as exc:
            ok = False
            print(f"✗ adapter/read capability: unavailable: {exc}")
    elif sys.platform == "darwin" or args.folder:
        source = "mail folder" if args.folder else "Apple Mail"
        try:
            print(f"✓ {source}: {len(_connector(args).mailboxes())} mailboxes reachable")
        except ConnectorError as exc:
            ok = False
            print(f"✗ {source}: {exc}")
    db = home / "history.sqlite"
    if db.exists():
        store = Store(db)
        try:
            counts = store.counts()
        finally:
            store.close()
        print(f"✓ history: {counts.get('keep', 0)} for you, {counts.get('review', 0)} review, {counts.get('junk', 0)} junk; "
              f"{counts['senders']} senders learned, {counts['teachings']} teachings")
    print("✓ Python/model network guard active: loopback only; external mail clients may sync normally")
    return 0 if ok else 1


def _store() -> Store:
    return Store(privacy.private_dir(configmod.home()) / "history.sqlite")


def cmd_scan(args) -> int:
    privacy.install_network_guard()
    config = configmod.load(Path(args.config) if args.config else None)
    if args.console:
        config = configmod.Config(**{**config.__dict__, "privacy": {**config.privacy, "console": args.console}})
    connector = _connector(args)
    store = _store()
    mode = "all" if args.rescan else "changed" if args.rescan_changed else "new"
    engine = Engine(config, store, connector, create)
    try:
        counts = engine.scan(ScanOptions(tuple(args.account or ()), args.mailbox, args.limit, mode))
        decided = counts["keep"] + counts["review"] + counts["junk"]
        print(f"\nDecided {decided} new: {counts['keep']} for you · {counts['review']} to review · {counts['junk']} junk"
              f"  (model {counts['by_model']}, memory {counts['by_memory']}, rules {counts['by_rule'] + counts['by_teach']};"
              f" already known {counts['skipped']}{', errors ' + str(counts['errors']) if counts['errors'] else ''})")
        if not args.no_report:
            html_path, _ = report.write(store, config, configmod.home() / "reports",
                                        (engine.accounts or args.account) if args.account else None)
            print(f"Report: {html_path}")
            if args.open:
                _open(html_path)
        return 0
    finally:
        store.commit()
        store.close()


def cmd_report(args) -> int:
    privacy.install_network_guard()
    config = configmod.load(Path(args.config) if args.config else None)
    store = _store()
    html_path, csv_path = report.write(store, config, configmod.home() / "reports", args.account)
    print(f"Report: {html_path}\nCSV:    {csv_path}")
    if args.open:
        _open(html_path)
    return 0


def cmd_categories(args) -> int:
    config = configmod.load(Path(args.config) if args.config else None)
    folders = actions.destinations(config)
    print(f"{'category':14} {'action':7} {'apply moves to':22} description")
    for c in config.categories:
        print(f"{c.name:14} {c.action:7} {folders[c.name] or '(stays where it is)':22} {c.description}")
    print(f"{'review':14} {'review':7} {folders['review']:22} model is unsure (likely junk)")
    print(f"\nMoving is {'on' if config.actions['enabled'] else 'off'} ([actions] enabled); "
          f"never moved: {', '.join(config.actions['keep_in_place']) or 'none'}")
    return 0


def cmd_teach(args) -> int:
    privacy.install_network_guard()
    config = configmod.load(Path(args.config) if args.config else None)
    store = _store()
    if args.what == "list":
        rows = store.teachings()
        for row in rows:
            print(f"{row['id']:4}  {row['kind']:6} {row['value']:40} → {row['category']}")
        print(f"{len(rows)} teachings" if rows else "Nothing taught yet.")
        return 0
    if args.what == "remove":
        if not args.value or not args.value.isdigit():
            print("usage: ./triage teach remove ID   (see ./triage teach list)")
            return 1
        print("removed" if store.unteach(int(args.value)) else "no such teaching")
        return 0
    if not args.value or not args.category:
        print("usage: ./triage teach sender news@shop.example.cz promotion   |   ./triage teach domain shop.example.cz promotion")
        return 1
    if args.category not in config.names() + ["review"]:
        print(f"unknown category {args.category!r}; choose from: {', '.join(config.names() + ['review'])}")
        return 1
    redo = store.teach(args.what, args.value, args.category)
    print(f"From now on mail from {args.what} {args.value} is {args.category}. "
          f"{redo} already decided emails will be re-decided on the next scan.")
    return 0


def cmd_forget(args) -> int:
    home = configmod.home()
    if args.all:
        if not _ask(f"Delete ALL history and reports in {home}? Your emails are not touched.", args.yes):
            print("Nothing deleted.")
            return 1
        # decisions.json: history file of the 0.x prototype.
        for name in ("history.sqlite", "history.sqlite-wal", "history.sqlite-shm", "decisions.json"):
            (home / name).unlink(missing_ok=True)
        shutil.rmtree(home / "reports", ignore_errors=True)
        print("History and reports deleted. Settings and downloaded models were kept.")
        return 0
    store = _store()
    if args.sender:
        print(f"Forgot {store.forget_sender(args.sender)} emails from {args.sender}.")
    elif args.older_than:
        print(f"Forgot {store.purge_older(args.older_than)} emails not seen for {args.older_than} days.")
        store.vacuum()
    else:
        print("usage: ./triage forget --sender ADDRESS | --older-than DAYS | --all")
        return 1
    return 0


def _mover(args):
    connector = _connector(args)
    if not isinstance(connector, Mover):
        raise ConnectorError(f"the {connector.name} source cannot move mail; moving works with Apple Mail "
                             "(Outlook: see docs/OUTLOOK_ADDIN.md › Limits)")
    return connector


def cmd_apply(args) -> int:
    """Show the move plan, then (after confirmation) move decided mail into its folders."""
    privacy.install_network_guard()
    config = configmod.load(Path(args.config) if args.config else None)
    console = args.console or config.privacy["console"]
    connector = _mover(args)
    store = _store()
    try:
        plans = [actions.plan(config, store, connector, mailbox)
                 for mailbox in connector.mailboxes(tuple(args.account or ()), args.mailbox)]
        if args.limit is not None:
            actions.limit(plans, args.limit)
        actions.show(plans, console)
        total = sum(len(p.moves) for p in plans)
        folders = {(p.mailbox.account, folder) for p in plans for folder in p.by_folder()}
        if not total:
            print("\nNothing to move.")
            return 0
        if args.dry_run:
            print(f"\nDry run: {total} emails would move into {len(folders)} folders. Nothing was moved.")
            return 0
        if not config.actions["enabled"]:
            print(f"\nMoving is switched off. To allow it, set  enabled = true  under [actions] in "
                  f"{config.path or configmod.home() / 'config.toml'}, then run ./triage apply again.")
            return 1
        if not _ask(f"\nMove {total} emails into {len(folders)} folders? Missing folders are created; "
                    "nothing is deleted.", args.yes):
            print("Nothing moved.")
            return 1
        run = store.start_run(f"apply:{connector.name}")
        counts = Counter()
        try:
            counts = actions.apply(store, connector, plans, run)
        finally:
            store.finish_run(run, dict(counts))
        notes = [f"created {counts['folders_created']} folders"] if counts["folders_created"] else []
        if counts["not_moved"]:
            notes.append(f"{counts['not_moved']} changed or gone meanwhile (not moved)")
        print(f"\nMoved {counts['moved']} emails{'; ' + ', '.join(notes) if notes else ''}.  Undo: ./triage undo")
        return 0
    finally:
        store.commit()
        store.close()


def cmd_undo(args) -> int:
    """Move the emails of the last apply (or of every apply with --all) back where they came from."""
    privacy.install_network_guard()
    connector = _mover(args)
    store = _store()
    try:
        pending = store.moves_to_undo(connector.name, args.account, args.all)
        if not pending:
            print("Nothing to undo.")
            return 0
        scope = "all applies" if args.all else "the last apply"
        if not _ask(f"Move {len(pending)} emails from {scope} back to the mailbox they came from?", args.yes):
            print("Nothing moved.")
            return 1
        counts = actions.undo(store, connector, args.account, args.all)
        gone = counts["not_found"]
        print(f"Moved {counts['moved_back']} emails back"
              + (f"; {gone} were no longer in their folder (left where they are)." if gone else "."))
        return 0
    finally:
        store.commit()
        store.close()


def cmd_start(args) -> int:
    """Guided run for double-click use: setup if needed, scan, open the report."""
    print("Mail Triage — sorts your inbox into 'for you' and junk, entirely on this computer.\n")
    home = configmod.home()
    config_file = home / "config.toml"
    needs_setup = not config_file.exists()
    if not needs_setup:
        try:
            create(configmod.load(config_file)).close()
        except (BackendError, ConfigError):
            needs_setup = True
    if needs_setup:
        args.backend = None
        args.base_python = None
        if cmd_setup(args) != 0:
            return 1
    args.no_report, args.open = False, True
    for name in ("account", "mailbox", "limit", "rescan", "rescan_changed", "console"):
        setattr(args, name, getattr(args, name, None))
    return cmd_scan(args)


def _model_ready() -> bool:
    config_file = configmod.home() / "config.toml"
    if not config_file.exists():
        return False
    try:
        create(configmod.load(config_file)).close()
        return True
    except (BackendError, ConfigError):
        return False


def cmd_addin(args) -> int:
    """Outlook add-in: install/run the localhost server that the add-in in Outlook talks to."""
    from .addin import certs, service
    try:
        return _addin(args)
    except (service.ServiceError, certs.CertError) as exc:
        print(f"\n✗ {exc}")
        return 1


def _addin(args) -> int:
    from .addin import certs, service
    from .addin.server import STATIC, serve

    config = Path(args.config) if args.config else None
    if args.action == "run":
        return serve(args.verbose, config)
    if args.action == "status":
        installed = service.plist_path().exists()
        print(f"background service: {'installed' if installed else 'not installed'}"
              f"{', loaded' if installed and service.loaded() else ''}")
        print(f"certificate: {'trusted' if certs.is_trusted() else 'not trusted / unknown'}")
        print(f"server: {'running' if service.healthy() else 'not responding'} ({service.ORIGIN})")
        print(f"add-in manifest: {STATIC / 'manifest.xml'}")
        print(f"log (no message content): {service.log_path()}")
        return 0
    if args.action == "stop":
        service.stop()
        print("Stopped. It starts again at the next login, or with  ./triage addin start")
        return 0
    if args.action == "start":
        service.start()
        print("Started." if service.wait_until_healthy() else f"Started, but not responding yet; see {service.log_path()}")
        return 0
    if args.action == "uninstall":
        service.uninstall()
        certs.remove()
        print("Removed the background service and the localhost certificate.")
        print("In Outlook: Get Add-ins → My add-ins → Mail Triage → Remove.")
        return 0

    # install
    print("Mail Triage for Outlook — sets up the local helper that the Outlook add-in talks to.\n")
    if not _model_ready():
        args.backend = None
        args.base_python = None
        args.source, args.folder, args.skip_mail_check = "applemail", None, True
        if cmd_setup(args) != 0:
            return 1
    try:
        certs.ensure()
    except (certs.CertError, OSError) as exc:
        print(f"\n✗ certificate: {exc}")
        return 1
    if not service.supported():
        print("\nAutomatic background start is macOS-only for now. Start the helper with:  ./triage addin run")
        return 0
    service.install(config)
    if not service.wait_until_healthy():
        print(f"\n✗ the helper did not start; see {service.log_path()}")
        return 1
    manifest = STATIC / "manifest.xml"
    print("✓ The helper runs in the background and starts automatically when you log in.\n")
    print("Last step, once per mailbox — add the add-in to Outlook:")
    print("  1. In Outlook: Get Add-ins (or Apps) → My add-ins → Custom add-ins → Add a custom add-in → Add from file")
    print(f"  2. Choose this file: {manifest}")
    print("  3. Accept the warning. Then select messages (or open one) and choose Mail Triage.")
    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", "-R", str(manifest)], check=False)
        subprocess.run(["open", "https://aka.ms/olksideload"], check=False)
    return 0


# -- parser ------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="triage", description="Local email triage with Laya decision models.")
    p.add_argument("--version", action="version", version=f"mail-triage {__version__}")
    p.add_argument("--home", help="data folder (default: per-user application data)")
    p.add_argument("--config", help="settings file (default: config.toml in the data folder)")
    sub = p.add_subparsers(dest="command")

    def source(s):
        group = s.add_mutually_exclusive_group()
        group.add_argument("--source", choices=("applemail", "outlook"), default="applemail",
                           help="local mail provider (default: applemail; Outlook is experimental)")
        group.add_argument("--folder", help="read exported .eml/.mbox files instead of a local mail client")

    s = sub.add_parser("start", help="guided: set up if needed, scan, open the report")
    source(s)
    s.add_argument("--yes", action="store_true", help="answer yes to download questions")
    s = sub.add_parser("setup", help="create settings, download the model, check Mail access")
    source(s)
    s.add_argument("--backend", choices=("mlx", "systemone"))
    s.add_argument("--base-python", help="ARM Python 3.11+ used to create the MLX runtime")
    s.add_argument("--yes", action="store_true", help="download without asking")
    s = sub.add_parser("doctor", help="check settings, model and Mail access")
    source(s)
    s.add_argument("--account", action="append", help="probe only this Outlook account (repeatable)")
    s.add_argument("--probe-read", action="store_true",
                   help="Outlook only: opt in to one in-memory message read per inbox; print capability flags only")
    s = sub.add_parser("scan", help="decide new emails and write a report")
    source(s)
    s.add_argument("--account", action="append", help="only this account (repeatable)")
    s.add_argument("--mailbox", help="mailbox inside --account (default: inbox)")
    s.add_argument("--limit", type=int, help="decide at most N emails")
    s.add_argument("--rescan", action="store_true", help="decide all emails again")
    s.add_argument("--rescan-changed", action="store_true", help="re-decide emails decided with older settings")
    s.add_argument("--console", choices=configmod.CONSOLE, help="what to print per email")
    s.add_argument("--no-report", action="store_true")
    s.add_argument("--open", action="store_true", help="open the report when done")
    s = sub.add_parser("report", help="write the report from history")
    s.add_argument("--account", action="append")
    s.add_argument("--open", action="store_true")
    sub.add_parser("categories", help="list categories and their future folders")
    s = sub.add_parser("teach", help="correct a sender or domain: teach sender ADDRESS CATEGORY")
    s.add_argument("what", choices=("sender", "domain", "list", "remove"))
    s.add_argument("value", nargs="?")
    s.add_argument("category", nargs="?")
    s = sub.add_parser("forget", help="delete history (GDPR): --sender, --older-than or --all")
    s.add_argument("--sender")
    s.add_argument("--older-than", type=int)
    s.add_argument("--all", action="store_true")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("addin", help="Outlook add-in: install | status | start | stop | run | uninstall")
    s.add_argument("action", nargs="?", default="status",
                   choices=("install", "status", "start", "stop", "run", "uninstall"))
    s.add_argument("--verbose", action="store_true", help="run: log per-message status lines (never content)")
    s.add_argument("--no-open", action="store_true", help="install: do not open Finder/Outlook")
    s.add_argument("--yes", action="store_true", help="install: download the model without asking")
    s = sub.add_parser("apply", help="move decided emails into their category folders (shows the plan, asks first)")
    source(s)
    s.add_argument("--account", action="append", help="only this account (repeatable)")
    s.add_argument("--mailbox", help="mailbox inside --account that was scanned (default: inbox)")
    s.add_argument("--limit", type=int, help="move at most N emails (try a few first)")
    s.add_argument("--dry-run", action="store_true", help="only show what would move")
    s.add_argument("--yes", action="store_true", help="move without asking")
    s.add_argument("--console", choices=configmod.CONSOLE, help="what to print per email")
    s = sub.add_parser("undo", help="move the emails of the last apply back")
    source(s)
    s.add_argument("--account", action="append", help="only this account (repeatable)")
    s.add_argument("--all", action="store_true", help="undo every apply, not only the last one")
    s.add_argument("--yes", action="store_true", help="move back without asking")
    sub.add_parser("help", help="show this help")
    return p


COMMANDS = {"start": cmd_start, "setup": cmd_setup, "doctor": cmd_doctor, "scan": cmd_scan, "report": cmd_report,
            "categories": cmd_categories, "teach": cmd_teach, "forget": cmd_forget, "apply": cmd_apply,
            "undo": cmd_undo, "addin": cmd_addin}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.home:
        os.environ["MAIL_TRIAGE_HOME"] = str(Path(args.home).expanduser().resolve())
    if args.command in (None, "help"):
        parser().print_help()
        return 0
    for name in ("folder", "yes"):
        if not hasattr(args, name):
            setattr(args, name, None)
    try:
        if getattr(args, "probe_read", False) and args.source != "outlook":
            raise ConnectorError("--probe-read requires --source outlook")
        return COMMANDS[args.command](args)
    except (ConfigError, ConnectorError, BackendError, provision.SetupError) as exc:
        print(f"\n✗ {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Everything decided so far is saved.")
        return 130
