"""The full flow with a phone simulated by a folder, and the interface."""

from __future__ import annotations

import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

from wa_transfer import android, crypt15, diagnosis, ios_backup, session

PREFIX = android.WA_ROOT
BACKUP_FILE = android.backup_file(PREFIX)
MEDIA_DIR = android.media_dir(PREFIX)


class FakeDevice:
    """Behaves like the real phone: resetting WhatsApp also deletes its media folder."""

    def __init__(self, root: Path, fail_push_on: str | None = None, prefix: str = PREFIX):
        self.root = root
        self.prefix = prefix
        self.fail_push_on = fail_push_on
        self.cleared = 0

    def _local(self, remote: str) -> Path:
        assert remote.startswith(self.prefix), remote
        return self.root / remote[len(self.prefix) + 1 :]

    def write(self, remote: str, data: bytes) -> None:
        path = self._local(remote)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def files(self) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()}

    def remote_file(self, remote):
        path = self._local(remote)
        return android.RemoteFile(path.stat().st_size, 1000) if path.is_file() else None

    def whatsapp_root(self):
        return self.prefix

    def now(self):
        return 1100

    def whatsapp_version(self):
        return "2.26.44.7"

    def free_bytes(self):
        return 100 * 1024**3

    def tree_bytes(self, _remote):
        return 1024

    def pull(self, remote, local, timeout=None):
        source = self._local(remote)
        local.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            for p in source.rglob("*"):
                if p.is_file():
                    target = local / p.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(p.read_bytes())
        else:
            local.write_bytes(source.read_bytes())

    def shell(self, command, **_):
        assert command.startswith("ls -A")
        return "\n".join(sorted(p.name for p in self.root.iterdir())) if self.root.exists() else ""

    def clear_whatsapp(self):
        self.cleared += 1
        for p in sorted(self.root.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()

    def push_tree(self, local_root, remote_root, progress=None):
        for p in local_root.rglob("*"):
            if p.is_file():
                if self.fail_push_on and self.fail_push_on in p.name:
                    raise android.AndroidError("cable disconnected")
                self.write(f"{remote_root}/{p.relative_to(local_root).as_posix()}", p.read_bytes())

    def push_verified(self, local, remote):
        self.write(remote, local.read_bytes())

    def count_files(self, remote):
        path = self._local(remote)
        return sum(1 for p in path.rglob("*") if p.is_file())


def quiet(*_):
    pass


@pytest.fixture
def phone(tmp_path, real_backup_blob) -> FakeDevice:
    device = FakeDevice(tmp_path / "phone")
    device.write(BACKUP_FILE, real_backup_blob)
    device.write(f"{MEDIA_DIR}/WhatsApp Images/old.jpg", b"already there")
    device.write(f"{PREFIX}/Backups/wa.db.crypt15", b"contacts")
    return device


@pytest.fixture
def ready(tmp_path, backup_dir, phone, root_key):
    s = session.Session(tmp_path / "work")
    s.read_iphone(ios_backup.read_info(backup_dir), None, quiet)
    s.read_phone_backup(phone, root_key.hex(), quiet)
    return s


def test_existing_phone_files_survive_the_reset(ready, phone):
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    files = phone.files()
    assert phone.cleared == 1
    assert "Media/WhatsApp Images/old.jpg" in files
    assert "Backups/wa.db.crypt15" in files
    assert "Databases/msgstore.db.crypt15" in files
    assert any(f.startswith("Media/WhatsApp Video/VID-") for f in files)
    assert ready.state.copied


def test_phone_backup_decrypts_with_the_same_key(ready, phone, root_key):
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    blob = (phone.root / "Databases/msgstore.db.crypt15").read_bytes()
    database = phone.root.parent / "read_back.db"
    database.write_bytes(crypt15.decrypt(blob, root_key).database)
    db = sqlite3.connect(database)
    assert db.execute("SELECT count(*) FROM message WHERE chat_row_id>0").fetchone()[0] == 18
    db.close()


def test_interrupted_copy_can_be_resumed_without_losing_files(ready, phone, tmp_path):
    ready.convert(quiet)
    phone.fail_push_on = "VID-"
    with pytest.raises(android.AndroidError):
        ready.apply_to_phone(phone, quiet)
    assert ready.state.phone_changed and not ready.state.copied
    assert phone.remote_file(BACKUP_FILE) is None, "the backup must be copied last"

    # The program is reopened: what was saved before the reset is still valid.
    resumed = session.Session(tmp_path / "work")
    assert resumed.state.device_saved and resumed.state.phone_changed
    phone.fail_push_on = None
    resumed.apply_to_phone(phone, quiet)
    assert resumed.state.copied
    assert "Media/WhatsApp Images/old.jpg" in phone.files()


def test_undo_restores_the_original_backup_even_after_rereading(ready, phone, root_key, real_backup_blob):
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    # A second round: the backup on the phone is now ours, it must not become "the original".
    ready.read_phone_backup(phone, root_key.hex(), quiet)
    assert not ready.state.converted
    assert ready.original_backup.read_bytes() == real_backup_blob
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    ready.undo_on_phone(phone, quiet)
    assert (phone.root / "Databases/msgstore.db.crypt15").read_bytes() == real_backup_blob
    assert "Media/WhatsApp Images/old.jpg" in phone.files()


def test_second_round_does_not_duplicate_messages(ready, phone, root_key):
    first = ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    ready.read_phone_backup(phone, root_key.hex(), quiet)
    second = ready.convert(quiet)
    assert second.messages == 0 and second.duplicates == first.messages


def test_not_enough_space_on_phone_changes_nothing(ready, phone, monkeypatch):
    ready.convert(quiet)
    monkeypatch.setattr(phone, "free_bytes", lambda: 1024)
    with pytest.raises(session.NotEnoughSpaceError):
        ready.apply_to_phone(phone, quiet)
    assert phone.cleared == 0 and not ready.state.phone_changed


def test_missing_backup_on_phone_is_explained(tmp_path, root_key):
    empty = FakeDevice(tmp_path / "empty")
    empty.root.mkdir()
    s = session.Session(tmp_path / "work")
    with pytest.raises(android.AndroidError, match="Back up"):
        s.read_phone_backup(empty, root_key.hex(), quiet)


def test_key_is_never_written_to_disk(ready, phone, root_key):
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    text = root_key.hex().encode()
    for path in ready.workdir.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert text not in data and root_key not in data, path


def test_clean_removes_all_chat_copies(ready, phone):
    ready.convert(quiet)
    ready.clean()
    assert not [p for p in ready.workdir.iterdir() if p.name != "transfer.log"]
    assert ready.root_key is None and not ready.state.converted


# ---- resuming after the backups


def test_resume_rebuilds_from_the_original_backup(ready, phone, root_key, tmp_path):
    first = ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)

    # Program reopened after the restore: the key isn't saved, the phone isn't needed.
    resumed = session.Session(tmp_path / "work")
    assert resumed.can_resume()
    resumed.resume_after_backups(root_key.hex(), None, quiet)
    assert not resumed.state.converted and resumed.state.phone_changed and resumed.state.device_saved

    second = resumed.convert(quiet)
    assert second.messages == first.messages and second.duplicates == 0, "it must restart from the backup without the chats already merged"
    resumed.apply_to_phone(phone, quiet)
    assert resumed.state.copied
    assert "Media/WhatsApp Images/old.jpg" in phone.files()


def test_resume_with_wrong_key_changes_nothing(ready, tmp_path):
    ready.convert(quiet)
    resumed = session.Session(tmp_path / "work")
    with pytest.raises(crypt15.Crypt15Error):
        resumed.resume_after_backups("0" * 64, None, quiet)
    assert resumed.state.converted and resumed.root_key is None


def test_resume_needs_the_iphone_backup_still_on_the_pc(ready, backup_dir, tmp_path):
    (backup_dir / "Manifest.plist").unlink()
    assert not session.Session(tmp_path / "work").can_resume()


def test_program_does_not_offer_a_stale_copy(ready, phone, root_key, tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from wa_transfer import gui

    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    resumed = session.Session(tmp_path / "work")
    resumed.resume_after_backups(root_key.hex(), None, quiet)  # the converted backup is now the old one
    try:
        app = gui.App(tmp_path / "work")
    except tk.TclError:
        pytest.skip("no display")
    try:
        app.update()
        assert app.page == gui.P_CONVERT
        assert "Resume from the backups already made" in _all_text(app.body)

        # The real path: key, click, and the conversion becomes available.
        app.key_text = root_key.hex()
        _button(app.body, "Read the backups again").invoke()
        deadline = time.monotonic() + 30
        while app.busy and time.monotonic() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        assert not app.busy and app.page == gui.P_CONVERT
        assert "Resume from the backups already made" not in _all_text(app.body)
        assert str(_button(app.body, "Start the conversion").cget("state")) == "normal"
    finally:
        app.destroy()


def test_choosing_the_old_folder_resumes_the_transfer(ready, phone, tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from wa_transfer import gui

    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    old = tmp_path / "other_disk" / "WhatsApp Transfer"
    old.parent.mkdir()
    ready.workdir.rename(old)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **_: str(old.parent))
    try:
        app = gui.App(tmp_path / "new", gui.P_WELCOME)
    except tk.TclError:
        pytest.skip("no display")
    try:
        _button(app.body, "Change folder").invoke()
        app.update()
        assert app.page == gui.P_RESTORE
    finally:
        app.destroy()


def _button(widget, text):
    if widget.winfo_class() == "Button" and widget.cget("text") == text:
        return widget
    for child in widget.winfo_children():
        found = _button(child, text)
        if found is not None:
            return found
    return None


def _all_text(widget) -> str:
    own = widget.cget("text") if widget.winfo_class() in ("Label", "Button") else ""
    return " ".join([own, *(_all_text(child) for child in widget.winfo_children())])


# ---- diagnostics


def _db_with_lid_chats(path: Path) -> None:
    """After the restore: the iPhone chat on the number and WhatsApp's new one on the LID."""
    db = sqlite3.connect(path)
    pn = db.execute("SELECT _id FROM jid WHERE raw_string LIKE '15555550100%'").fetchone()[0]
    lid = db.execute("INSERT INTO jid(user, server, agent, device, type, raw_string) VALUES ('999','lid',0,0,18,'999@lid')").lastrowid
    db.execute("INSERT INTO jid_map(lid_row_id, jid_row_id) VALUES (?,?)", (lid, pn))
    chat = db.execute("INSERT INTO chat(jid_row_id, created_timestamp, sort_timestamp) VALUES (?,?,?)", (lid, 4_000_000_000_000, 4_000_000_000_000)).lastrowid
    db.execute(
        "INSERT INTO message(chat_row_id, from_me, key_id, status, timestamp, message_type, text_data, sort_id) VALUES (?,0,'NEW',0,?,0,'private text',999)",
        (chat, 4_000_000_000_000),
    )
    db.commit()
    db.close()


def test_report_measures_contain_no_private_data(ready):
    ready.convert(quiet)
    text = "\n".join(diagnosis.describe(ready.output_db))
    assert "chats per server" in text and "s.whatsapp.net" in text and "unavailable" not in text
    assert "Hello" not in text and "15555550100" not in text and "Alice" not in text


def test_report_finds_split_chats_and_where_new_messages_went(ready, tmp_path):
    ready.convert(quiet)
    since = diagnosis.newest_timestamp(ready.output_db)
    current = tmp_path / "current.db"
    current.write_bytes(ready.output_db.read_bytes())
    _db_with_lid_chats(current)
    shape = "\n".join(diagnosis.describe(current))
    assert "one on the number and one on the LID: [(1,)]" in shape
    new = "\n".join(diagnosis.describe_since(current, since))
    assert "('lid', 0, 0, 0, 1)" in new and "[('lid', 1)]" in new
    assert "private text" not in shape + new and "999@lid" not in shape + new


def test_inspection_recognises_a_backup_not_made_again(ready, phone, root_key):
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    lines = ready.inspect_phone_backup(phone, root_key.hex())
    assert "no new backup was made" in lines[0]
    assert not (ready.workdir / "diagnostics").exists()


def test_inspection_reads_the_new_backup_and_leaves_no_copies(ready, phone, root_key, tmp_path):
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    current = tmp_path / "current.db"
    current.write_bytes(ready.output_db.read_bytes())
    _db_with_lid_chats(current)
    reference = crypt15.decrypt(ready.output_backup.read_bytes(), root_key)
    phone.write(BACKUP_FILE, crypt15.encrypt(current.read_bytes(), root_key, reference))
    text = "\n".join(ready.inspect_phone_backup(phone, root_key.hex()))
    assert "('lid', 0, 0, 0, 1)" in text
    assert not (ready.workdir / "diagnostics").exists()


def test_inspection_without_key_or_with_wrong_key(ready, phone):
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    assert "Key not entered" in ready.inspect_phone_backup(phone, "  ")[0]
    phone.write(BACKUP_FILE, phone._local(BACKUP_FILE).read_bytes() + b"x")
    assert "does not open" in ready.inspect_phone_backup(phone, "0" * 64)[0]


def test_database_errors_are_scrubbed():
    class Phone(android.Android):
        def __init__(self):
            super().__init__(Path("adb"))

        def shell(self, command, **_):
            return "package:com.whatsapp uid:10234"

        def _run(self, *args, **_):
            assert "--uid=10234" in args
            return (
                "11-02 12:00:01.000 100 200 E SQLiteLog: (2067) abort at 18 in [INSERT INTO message VALUES('private text')]: "
                "UNIQUE constraint failed: message.sort_id\n"
                "11-02 12:00:02.000 100 200 E SQLiteDatabase: 15555550100@s.whatsapp.net invalid 15555550100\n"
                "11-02 12:00:03.000 100 200 I Other: unrelated line"
            )

    text = Phone().database_errors()
    assert "UNIQUE constraint failed: message.sort_id" in text
    assert "private text" not in text and "15555550100" not in text and "unrelated" not in text


def test_clicking_the_empty_list_notice_is_not_an_error(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from wa_transfer import gui

    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr(gui.ios_backup, "find_backups", lambda _e=None: [])
    try:
        app = gui.App(tmp_path / "work", gui.P_IPHONE)
    except tk.TclError:
        pytest.skip("no display")

    def listboxes(widget):
        found = [widget] if widget.winfo_class() == "Listbox" else []
        for child in widget.winfo_children():
            found += listboxes(child)
        return found

    try:
        listbox = listboxes(app.body)[0]
        listbox.selection_set(0)
        listbox.event_generate("<<ListboxSelect>>")
        app.update()
    finally:
        app.destroy()
    assert not errors


# ---- password-protected iPhone backup (simulated library: creating one needs a real iPhone)


def test_encrypted_backup_flow(tmp_path, ios_db, monkeypatch):
    from conftest import MEDIA_FILES, build_backup

    class WrongPass(ValueError):
        pass

    class FakeEncrypted:
        def __init__(self, *, backup_directory, passphrase):
            if passphrase != "right":
                raise WrongPass

        def test_decryption(self):
            return True

        def extract_file(self, *, relative_path, domain_like, output_filename):
            assert domain_like == ios_backup.WHATSAPP_DOMAIN
            if relative_path != "ChatStorage.sqlite":
                raise FileNotFoundError
            Path(output_filename).write_bytes(ios_db.read_bytes())

        def extract_files(self, *, relative_paths_like, domain_like, output_folder, preserve_folders, incremental, filter_callback):
            assert preserve_folders and relative_paths_like == "Message/Media/%"
            for n, (relative, data) in enumerate(MEDIA_FILES.items()):
                filter_callback(n=n, total_files=len(MEDIA_FILES))
                path = Path(output_folder) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    fake = types.ModuleType("iphone_backup_decrypt")
    fake.EncryptedBackup = FakeEncrypted
    errors = types.ModuleType("iphone_backup_decrypt.exceptions")
    errors.IncorrectPassphraseError = WrongPass
    monkeypatch.setitem(sys.modules, "iphone_backup_decrypt", fake)
    monkeypatch.setitem(sys.modules, "iphone_backup_decrypt.exceptions", errors)

    info = ios_backup.read_info(build_backup(tmp_path / "b", ios_db, encrypted=True))
    with pytest.raises(ios_backup.WrongPasswordError, match="not correct"):
        ios_backup.extract(info, tmp_path / "w", "wrong")
    data = ios_backup.extract(info, tmp_path / "w", "right")
    assert set(data.media) == set(MEDIA_FILES)
    assert data.database.is_file()


def test_real_encrypted_library_exposes_what_we_use():
    from iphone_backup_decrypt import EncryptedBackup
    from iphone_backup_decrypt.exceptions import IncorrectPassphraseError

    assert issubclass(IncorrectPassphraseError, ValueError)
    for name in ("test_decryption", "extract_file", "extract_files", "manifest_db_cursor"):
        assert callable(getattr(EncryptedBackup, name))


# ---- interface


def test_every_page_opens(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from wa_transfer import gui

    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr(gui, "tool_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(gui.android, "find_adb", lambda _d: Path("adb"))
    monkeypatch.setattr(gui.ios_backup, "find_backups", lambda _e=None: [])
    try:
        app = gui.App(tmp_path / "work", 0)
    except tk.TclError:
        pytest.skip("no display")
    try:
        for page in range(len(gui.PAGES)):
            app.show(page)
            app.update()
        app.trouble = True
        app.show(gui.P_END)
        app.update()
        # Leaving the connection page while adb was still being prepared must not raise.
        deadline = time.monotonic() + 5
        while app.busy and time.monotonic() < deadline:
            app.update()
            time.sleep(0.02)
    finally:
        app.destroy()
    assert not errors


def test_crash_in_a_click_is_logged_and_shown(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from wa_transfer import gui

    shown = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(sys, "stderr", None)  # as in the exe, which has no console
    try:
        app = gui.App(tmp_path / "work", 0)
    except tk.TclError:
        pytest.skip("no display")

    def boom():
        raise RuntimeError("test failure")

    try:
        app._button(app.body, "x", boom).invoke()
        app.update()
    finally:
        app.destroy()
    log_text = (tmp_path / "work" / "transfer.log").read_text("utf-8")
    assert "test failure" in log_text and "Traceback" in log_text
    assert shown and "transfer.log" in shown[0][1]


def test_report_works_without_phone_and_includes_log(ready, phone):
    from wa_transfer import gui

    (ready.workdir / "transfer.log").write_text("test line\n", "utf-8")
    ready.convert(quiet)
    text = gui._diagnostic(None, ready)
    assert "Phone: not connected" in text and "test line" in text
    assert "Hello" not in text, "no message content"


def test_startup_failure_is_reported(tmp_path, monkeypatch):
    pytest.importorskip("tkinter")
    from wa_transfer import gui

    shown = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(gui.tempfile, "gettempdir", lambda: str(tmp_path))

    def broken(*_a, **_k):
        raise PermissionError("folder not writable")

    monkeypatch.setattr(gui, "App", broken)
    gui.main()
    assert "folder not writable" in (tmp_path / "WhatsAppTransfer_error.txt").read_text("utf-8")
    assert shown


def test_navigation_is_gated(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from wa_transfer import gui

    monkeypatch.setattr(gui.ios_backup, "find_backups", lambda _e=None: [])
    try:
        app = gui.App(tmp_path / "work", gui.P_IPHONE)
    except tk.TclError:
        pytest.skip("no display")
    try:
        app.update()
        assert str(app.next_button.cget("state")) == "disabled"
        app.show(gui.P_COPY)
        app.update()
        assert str(app.next_button.cget("state")) == "disabled"
    finally:
        app.destroy()


# ---- phones that keep WhatsApp in the old folder


def test_transfer_uses_the_folder_the_phone_really_has(tmp_path, backup_dir, root_key, real_backup_blob):
    device = FakeDevice(tmp_path / "old_phone", prefix=android.LEGACY_ROOT)
    device.write(android.backup_file(android.LEGACY_ROOT), real_backup_blob)
    device.write(f"{android.media_dir(android.LEGACY_ROOT)}/WhatsApp Images/old.jpg", b"already there")
    s = session.Session(tmp_path / "work")
    s.read_iphone(ios_backup.read_info(backup_dir), None, quiet)
    s.read_phone_backup(device, root_key.hex(), quiet)
    assert s.state.phone_root == android.LEGACY_ROOT
    s.convert(quiet)
    s.apply_to_phone(device, quiet)
    assert s.state.copied
    assert "Databases/msgstore.db.crypt15" in device.files()
    assert "Media/WhatsApp Images/old.jpg" in device.files()


def test_phone_folder_is_remembered_after_the_reset(ready, phone, tmp_path, monkeypatch):
    ready.convert(quiet)
    ready.apply_to_phone(phone, quiet)
    resumed = session.Session(tmp_path / "work")
    monkeypatch.setattr(phone, "whatsapp_root", lambda: pytest.fail("must not be detected again"))
    assert resumed.phone_root(phone) == PREFIX
