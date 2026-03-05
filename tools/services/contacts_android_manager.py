# Copyright 2026 Bardia Moshiri
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import time
import queue
import sqlite3
import threading
import logging

from gi.repository import GLib

import pyinotify

from tools.helpers import lxc

class ContactsAndroidManager:
    ANDROMEDA_ACCOUNT_NAME = "andromeda"
    ANDROMEDA_ACCOUNT_TYPE = "io.furios.andromeda"
    ANDROMEDA_OWNER_MARKER = "io.furios.andromeda.owned"

    CONTACTS_DB_REL_PATH = "data/com.android.providers.contacts/databases/contacts2.db"
    CONTACTS_DB_DIR_REL_PATH = "data/com.android.providers.contacts/databases"

    DEBOUNCE_SECONDS = 1.0

    def __init__(
        self,
        args,
        session,
        get_session,
        emit_cb,
        emit_linux_contact_imported_cb=None,
        emit_android_contact_updated_cb=None,
        emit_android_contact_removed_cb=None,
    ):
        self.args = args
        self.session = session
        self.get_session = get_session
        self.emit_cb = emit_cb
        self.emit_linux_contact_imported_cb = emit_linux_contact_imported_cb
        self.emit_android_contact_updated_cb = emit_android_contact_updated_cb
        self.emit_android_contact_removed_cb = emit_android_contact_removed_cb

        self.watch_enabled = False
        self.initial_force_emit_done = False
        self.debounce_source_id = None

        self.cache_lock = threading.Lock()
        self.last_seen = {}

        self.db_dir = os.path.join(session["andromeda_data"], self.CONTACTS_DB_DIR_REL_PATH)
        self.db_path = os.path.join(session["andromeda_data"], self.CONTACTS_DB_REL_PATH)

        self.account_lock = threading.Lock()
        self.andromeda_account_id = None

        self.watch_manager = None
        self.notifier = None
        self.watch_thread = None
        self.watch_stop_event = threading.Event()

        self.job_queue = queue.Queue()
        self.worker_stop_event = threading.Event()
        self.worker_thread = threading.Thread(
            target=self.worker_loop,
            daemon=True,
            name="andromeda-contacts-android",
        )
        self.worker_thread.start()

    def db_exists(self):
        return os.path.exists(self.db_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=2.0)

        def phonebook_collation(a, b):
            sa = "" if a is None else str(a)
            sb = "" if b is None else str(b)

            ka = sa.casefold()
            kb = sb.casefold()

            if ka < kb:
                return -1
            if ka > kb:
                return 1
            return 0

        conn.create_collation("PHONEBOOK", phonebook_collation)
        conn.create_collation("LOCALIZED", phonebook_collation)
        conn.create_collation("UNICODE", phonebook_collation)
        return conn

    def shutdown(self):
        self.stop_watcher()
        self.worker_stop_event.set()
        self.job_queue.put(("__stop__", (), {}))
        if self.worker_thread.is_alive():
            try:
                self.worker_thread.join(timeout=2.0)
            except Exception as e:
                logging.warning(f"Failed to join worker thread: {e}")

    def enqueue_job(self, op, *args, **kwargs):
        self.job_queue.put((op, args, kwargs))

    def enqueue_import_linux_contact(self, linux_uid, display_name, phones):
        self.enqueue_job("import_linux_contact", linux_uid, display_name, phones)

    def enqueue_update_android_contact(self, raw_id, display_name, phones):
        self.enqueue_job("update_android_contact", raw_id, display_name, phones)

    def enqueue_remove_android_contact(self, raw_id):
        self.enqueue_job("remove_android_contact", raw_id)

    def enqueue_scan(self, force_emit=False):
        self.enqueue_job("scan_and_emit", force_emit=force_emit)

    def worker_loop(self):
        while not self.worker_stop_event.is_set():
            try:
                op, args, kwargs = self.job_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if op == "__stop__":
                self.job_queue.task_done()
                break

            try:
                if op == "import_linux_contact":
                    linux_uid, display_name, phones = args
                    raw_id = self.import_linux_contact(linux_uid, display_name, phones)
                    ok = raw_id > 0
                    err = "" if ok else "Failed to import Linux contact into Android"
                    self.emit_linux_contact_imported(linux_uid, raw_id, ok, err)
                elif op == "update_android_contact":
                    raw_id, display_name, phones = args
                    ok = bool(self.update_android_contact(raw_id, display_name, phones))
                    err = "" if ok else "Failed to update Android contact"
                    self.emit_android_contact_updated(raw_id, ok, err)
                elif op == "remove_android_contact":
                    (raw_id,) = args
                    ok = bool(self.remove_android_contact(raw_id))
                    err = "" if ok else "Failed to remove Android contact"
                    self.emit_android_contact_removed(raw_id, ok, err)
                elif op == "scan_and_emit":
                    force_emit = bool(kwargs.get("force_emit", False))
                    self.scan_and_emit(force_emit=force_emit)
            except Exception as e:
                logging.warning(f"ContactsAndroidManager worker job failed op={op}: {e}")
                if op == "import_linux_contact":
                    linux_uid = str(args[0]) if args else ""
                    self.emit_linux_contact_imported(linux_uid, 0, False, str(e))
                elif op == "update_android_contact":
                    raw_id = int(args[0]) if args else 0
                    self.emit_android_contact_updated(raw_id, False, str(e))
                elif op == "remove_android_contact":
                    raw_id = int(args[0]) if args else 0
                    self.emit_android_contact_removed(raw_id, False, str(e))
            finally:
                self.job_queue.task_done()

    def idle_call(self, fn, *args):
        def runner():
            try:
                fn(*args)
            except Exception as e:
                logging.warning(f"Idle callback failed for {getattr(fn, '__name__', repr(fn))}: {e}")
            return False

        GLib.idle_add(runner)

    def emit_contact_changed(self, payload, change_type):
        self.idle_call(self.emit_cb, payload, change_type)

    def emit_linux_contact_imported(self, linux_uid, raw_id, success, error):
        if self.emit_linux_contact_imported_cb:
            self.idle_call(
                self.emit_linux_contact_imported_cb,
                linux_uid,
                int(raw_id),
                bool(success),
                str(error or ""),
            )

    def emit_android_contact_updated(self, raw_id, success, error):
        if self.emit_android_contact_updated_cb:
            self.idle_call(
                self.emit_android_contact_updated_cb,
                int(raw_id),
                bool(success),
                str(error or ""),
            )

    def emit_android_contact_removed(self, raw_id, success, error):
        if self.emit_android_contact_removed_cb:
            self.idle_call(
                self.emit_android_contact_removed_cb,
                int(raw_id),
                bool(success),
                str(error or ""),
            )

    def ensure_andromeda_account(self):
        with self.account_lock:
            if self.andromeda_account_id is not None:
                return self.andromeda_account_id

            if not self.db_exists():
                logging.warning(f"Contacts DB not found at {self.db_path}")
                self.andromeda_account_id = None
                return None

            try:
                conn = self.connect()
                cur = conn.cursor()
                cur.execute(
                    "SELECT _id FROM accounts WHERE account_name=? AND account_type=? LIMIT 1",
                    (self.ANDROMEDA_ACCOUNT_NAME, self.ANDROMEDA_ACCOUNT_TYPE),
                )
                row = cur.fetchone()
                if row:
                    self.andromeda_account_id = int(row[0])

                    cur.execute(
                        """
                        UPDATE accounts
                        SET ungrouped_visible = 0,
                            should_sync = 1,
                            x_is_default = 0
                        WHERE _id = ?
                        """,
                        (self.andromeda_account_id,),
                    )
                    conn.commit()
                    conn.close()
                    return self.andromeda_account_id

                cur.execute(
                    """
                    INSERT INTO accounts (account_name, account_type, data_set, ungrouped_visible, should_sync, x_is_default)
                    VALUES (?, ?, NULL, 0, 1, 0)
                    """,
                    (self.ANDROMEDA_ACCOUNT_NAME, self.ANDROMEDA_ACCOUNT_TYPE),
                )
                conn.commit()

                cur.execute(
                    "SELECT _id FROM accounts WHERE account_name=? AND account_type=? LIMIT 1",
                    (self.ANDROMEDA_ACCOUNT_NAME, self.ANDROMEDA_ACCOUNT_TYPE),
                )
                row = cur.fetchone()
                self.andromeda_account_id = int(row[0]) if row else None
                conn.commit()
                conn.close()
                logging.debug(f"Ensured Andromeda account_id={self.andromeda_account_id}")
                return self.andromeda_account_id
            except Exception as e:
                logging.warning(f"Failed to ensure Andromeda account: {e}")
                self.andromeda_account_id = None
                return None

    def get_andromeda_account_id(self):
        if self.andromeda_account_id is not None:
            return self.andromeda_account_id
        return self.ensure_andromeda_account()

    def list_android_contacts(self, include_andromeda_owned=True):
        if not self.db_exists():
            return []

        self.get_andromeda_account_id()

        try:
            conn = self.connect()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT _id, contact_id, account_id, deleted, version, display_name, sync1, sync2, sourceid
                FROM raw_contacts NOT INDEXED
                WHERE deleted = 0
                """
            )
            rows = cur.fetchall()
            raw_ids = [int(r[0]) for r in rows]

            phones = self.fetch_phones(conn, raw_ids)
            names = self.fetch_names(conn, raw_ids)

            results = []
            for r in rows:
                raw_id = int(r[0])
                contact_id = int(r[1]) if r[1] is not None else 0
                account_id = int(r[2]) if r[2] is not None else 0
                deleted = int(r[3]) if r[3] is not None else 0
                version = int(r[4]) if r[4] is not None else 0
                display_name = r[5] or ""
                sync1 = r[6] or ""
                sync2 = r[7] or ""
                sourceid = r[8] or ""

                name = names.get(raw_id) or display_name
                phs = phones.get(raw_id, [])

                is_owned = self.is_andromeda_owned(account_id, sync1)

                if not include_andromeda_owned and is_owned:
                    continue

                results.append(
                    {
                        "raw_contact_id": raw_id,
                        "contact_id": contact_id,
                        "account_id": account_id,
                        "display_name": name or display_name,
                        "phones": phs,
                        "emails": [],
                        "updated_ts": float(time.time()),
                        "sync1": sync1,
                        "sync2": sync2,
                        "sourceid": sourceid,
                        "is_andromeda_owned": bool(is_owned),
                        "version": version,
                        "deleted": deleted,
                    }
                )

            conn.close()
            return results
        except Exception as e:
            logging.warning(f"Failed to list Android contacts: {e}")
            return []

    def split_name(self, display_name):
        name = (display_name or "").strip()
        if not name:
            return "", ""
        parts = name.split()
        if len(parts) <= 1:
            return name, ""
        return " ".join(parts[:-1]), parts[-1]

    def provider_insert_raw_contact(self, linux_uid):
        uri = (
            "content://com.android.contacts/raw_contacts"
            "?caller_is_syncadapter=true"
            f"&account_name={self.ANDROMEDA_ACCOUNT_NAME}"
            f"&account_type={self.ANDROMEDA_ACCOUNT_TYPE}"
        )

        lxc.content_insert(
            self.args,
            [
                "--uri", uri,
                "--bind", f"account_name:s:{self.ANDROMEDA_ACCOUNT_NAME}",
                "--bind", f"account_type:s:{self.ANDROMEDA_ACCOUNT_TYPE}",
                "--bind", f"sync1:s:{self.ANDROMEDA_OWNER_MARKER}",
                "--bind", f"sync2:s:{linux_uid}",
                "--bind", f"sourceid:s:{linux_uid}",
            ],
        )

        account_id = self.get_andromeda_account_id() or 0

        conn = self.connect()
        cur = conn.cursor()
        try:
            for _ in range(20):
                cur.execute(
                    """
                    SELECT _id
                    FROM raw_contacts NOT INDEXED
                    WHERE account_id = ?
                      AND sync1 = ?
                      AND sync2 = ?
                      AND deleted = 0
                    ORDER BY _id DESC
                    LIMIT 1
                    """,
                    (int(account_id), self.ANDROMEDA_OWNER_MARKER, linux_uid),
                )
                row = cur.fetchone()
                if row:
                    return int(row[0])
                time.sleep(0.1)
        finally:
            conn.close()

        return 0

    def provider_insert_name_data(self, raw_id, display_name):
        uri = (
            "content://com.android.contacts/data"
            "?caller_is_syncadapter=true"
            f"&account_name={self.ANDROMEDA_ACCOUNT_NAME}"
            f"&account_type={self.ANDROMEDA_ACCOUNT_TYPE}"
        )
        given, family = self.split_name(display_name)

        content_args = [
            "--uri", uri,
            "--bind", f"raw_contact_id:i:{int(raw_id)}",
            "--bind", "mimetype:s:vnd.android.cursor.item/name",
            "--bind", f"data1:s:{display_name or ''}",
        ]

        if given:
            content_args.extend(["--bind", f"data2:s:{given}"])
        if family:
            content_args.extend(["--bind", f"data3:s:{family}"])

        lxc.content_insert(self.args, content_args)

    def provider_insert_phone_data(self, raw_id, phone):
        uri = (
            "content://com.android.contacts/data"
            "?caller_is_syncadapter=true"
            f"&account_name={self.ANDROMEDA_ACCOUNT_NAME}"
            f"&account_type={self.ANDROMEDA_ACCOUNT_TYPE}"
        )

        lxc.content_insert(
            self.args,
            [
                "--uri", uri,
                "--bind", f"raw_contact_id:i:{int(raw_id)}",
                "--bind", "mimetype:s:vnd.android.cursor.item/phone_v2",
                "--bind", f"data1:s:{phone or ''}",
                "--bind", "data2:i:2",
                "--bind", "data3:s:Mobile",
            ],
        )

    def provider_delete_mimetype_rows(self, raw_id, mimetype):
        uri = (
            "content://com.android.contacts/data"
            "?caller_is_syncadapter=true"
            f"&account_name={self.ANDROMEDA_ACCOUNT_NAME}"
            f"&account_type={self.ANDROMEDA_ACCOUNT_TYPE}"
        )

        lxc.content_delete(
            self.args,
            [
                "--uri", uri,
                "--where", f"raw_contact_id={int(raw_id)} AND mimetype='{mimetype}'",
            ],
        )

    def provider_refresh_contact(self, raw_id, display_name, phones):
        try:
            self.provider_delete_mimetype_rows(raw_id, "vnd.android.cursor.item/name")
            self.provider_delete_mimetype_rows(raw_id, "vnd.android.cursor.item/phone_v2")

            self.provider_insert_name_data(raw_id, display_name)

            for phone in phones:
                if str(phone).strip():
                    self.provider_insert_phone_data(raw_id, str(phone).strip())

            return True
        except Exception as e:
            logging.warning(f"Failed to refresh Android provider contact raw_contact_id={raw_id}: {e}")
            return False

    def import_linux_contact(self, linux_uid, display_name, phones):
        if not linux_uid:
            return 0
        if not self.db_exists():
            return 0

        account_id = self.get_andromeda_account_id()
        if not account_id:
            return 0

        display_name = display_name or ""
        phones = [p for p in (phones or []) if p]

        conn = None
        try:
            conn = self.connect()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT _id FROM raw_contacts NOT INDEXED
                WHERE account_id = ?
                  AND sync1 = ?
                  AND sync2 = ?
                  AND deleted = 0
                LIMIT 1
                """,
                (account_id, self.ANDROMEDA_OWNER_MARKER, linux_uid),
            )
            row = cur.fetchone()
            conn.close()
            conn = None

            if row:
                raw_id = int(row[0])
                if self.provider_refresh_contact(raw_id, display_name, phones):
                    logging.warning(
                        f"Updated Android contact from Linux (linux_uid={linux_uid} raw_contact_id={raw_id})"
                    )
                    return raw_id
                return 0

            raw_id = self.provider_insert_raw_contact(linux_uid)
            if raw_id <= 0:
                logging.warning(f"Failed to create Android raw contact from Linux uid={linux_uid}")
                return 0

            if not self.provider_refresh_contact(raw_id, display_name, phones):
                return 0

            logging.debug(
                f"Added Android contact from Linux (linux_uid={linux_uid} raw_contact_id={raw_id} "
                f"name={display_name!r} phones={phones})"
            )
            return raw_id
        except Exception as e:
            logging.warning(f"Failed to import Linux contact: {e}")
            try:
                if conn:
                    conn.close()
            except Exception as e:
                logging.warning(f"Failed to close connection after failure: {e}")
            return 0

    def update_android_contact(self, raw_id, display_name, phones):
        if raw_id <= 0 or not self.db_exists():
            return False

        try:
            ok = self.provider_refresh_contact(raw_id, display_name or "", [p for p in (phones or []) if p])
            if ok:
                logging.debug(f"Updated Android raw_contact_id={raw_id}")
            return ok
        except Exception as e:
            logging.warning(f"Failed to update Android contact raw_contact_id={raw_id}: {e}")
            return False

    def remove_android_contact(self, raw_id):
        if raw_id <= 0 or not self.db_exists():
            return False

        try:
            uri = (
                "content://com.android.contacts/raw_contacts"
                "?caller_is_syncadapter=true"
                f"&account_name={self.ANDROMEDA_ACCOUNT_NAME}"
                f"&account_type={self.ANDROMEDA_ACCOUNT_TYPE}"
            )

            lxc.content_delete(
                self.args,
                [
                    "--uri", uri,
                    "--where", f"_id={int(raw_id)}",
                ],
            )

            conn = self.connect()
            cur = conn.cursor()
            for _ in range(20):
                cur.execute("SELECT 1 FROM raw_contacts NOT INDEXED WHERE _id = ?", (int(raw_id),))
                row = cur.fetchone()
                if row is None:
                    conn.close()
                    logging.debug(f"Removed Android raw_contact_id={raw_id} deleted=1")
                    return True
                time.sleep(0.1)
            conn.close()

            logging.warning(f"Failed to remove Android contact raw_contact_id={raw_id}")
            return False
        except Exception as e:
            logging.warning(f"Failed to remove Android contact raw_contact_id={raw_id}: {e}")
            return False

    def start_watcher(self):
        if self.watch_enabled:
            return
        if not self.db_exists():
            logging.warning(f"Contacts DB not found at {self.db_path}, watcher not started")
            return

        self.get_andromeda_account_id()

        if not os.path.isdir(self.db_dir):
            logging.warning(f"Contacts DB directory not found: {self.db_dir}")
            return

        try:
            self.prime_cache()
        except Exception as e:
            logging.warning(f"Failed to prime contacts cache: {e}")

        try:
            self.watch_manager = pyinotify.WatchManager()
            mask = (
                pyinotify.IN_MODIFY
                | pyinotify.IN_CLOSE_WRITE
                | pyinotify.IN_CREATE
                | pyinotify.IN_MOVED_TO
                | pyinotify.IN_MOVE_SELF
                | pyinotify.IN_DELETE_SELF
            )

            manager = self

            class Handler(pyinotify.ProcessEvent):
                def process_default(self, event):
                    name = (getattr(event, "name", "") or "").strip()
                    if name.startswith("contacts2.db"):
                        GLib.idle_add(manager.debounce)

            self.notifier = pyinotify.Notifier(self.watch_manager, Handler(), read_freq=10)
            self.watch_manager.add_watch(self.db_dir, mask, rec=True, auto_add=True)

            self.watch_stop_event.clear()
            self.watch_enabled = True
            self.watch_thread = threading.Thread(target=self.watch_loop, daemon=True)
            self.watch_thread.start()

            logging.debug(f"Started Android contacts watcher on {self.db_dir}")
            if not self.initial_force_emit_done:
                self.initial_force_emit_done = True
                self.trigger_sync_scan(force_emit=True)
        except Exception as e:
            logging.warning(f"Failed to start contacts watcher: {e}")
            self.watch_enabled = False
            self.watch_manager = None
            self.notifier = None
            self.watch_thread = None

    def stop_watcher(self):
        self.watch_enabled = False

        if self.debounce_source_id:
            try:
                GLib.source_remove(self.debounce_source_id)
            except Exception as e:
                logging.warning(f"Failed to remove debounce source: {e}")
            self.debounce_source_id = None

        self.watch_stop_event.set()

        if self.notifier:
            try:
                self.notifier.stop()
            except Exception as e:
                logging.warning(f"Failed to stop notifier: {e}")

        if self.watch_thread and self.watch_thread.is_alive():
            try:
                self.watch_thread.join(timeout=1.0)
            except Exception as e:
                logging.warning(f"Failed to join watch thread: {e}")

        self.watch_manager = None
        self.notifier = None
        self.watch_thread = None

        logging.debug("Stopped Android contacts watcher")

    def watch_loop(self):
        if not self.notifier:
            return
        while not self.watch_stop_event.is_set():
            try:
                self.notifier.process_events()
                if self.notifier.check_events(timeout=500):
                    self.notifier.read_events()
            except Exception as e:
                logging.warning(f"Contacts watcher loop error: {e}")
                time.sleep(0.2)

    def trigger_sync_scan(self, force_emit=False):
        self.enqueue_scan(force_emit=force_emit)

    def debounce(self):
        if self.debounce_source_id:
            try:
                GLib.source_remove(self.debounce_source_id)
            except Exception as e:
                logging.warning(f"Failed to remove existing debounce source: {e}")
        self.debounce_source_id = GLib.timeout_add(
            int(self.DEBOUNCE_SECONDS * 1000),
            self.debounced_scan,
        )
        return False

    def debounced_scan(self):
        self.debounce_source_id = None
        self.enqueue_scan(force_emit=False)
        return False

    def prime_cache(self):
        if not self.db_exists():
            return
        conn = self.connect()
        cur = conn.cursor()
        cur.execute("SELECT _id, version, deleted FROM raw_contacts NOT INDEXED")
        rows = cur.fetchall()
        conn.close()
        with self.cache_lock:
            self.last_seen = {int(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in rows}

    def scan_and_emit(self, force_emit):
        if not self.db_exists():
            return

        self.get_andromeda_account_id()

        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT _id, contact_id, account_id, deleted, version, display_name, sync1, sync2, sourceid "
            "FROM raw_contacts NOT INDEXED"
        )
        rows = cur.fetchall()

        current = {}
        meta = {}
        for r in rows:
            raw_id = int(r[0])
            contact_id = int(r[1]) if r[1] is not None else 0
            account_id = int(r[2]) if r[2] is not None else 0
            deleted = int(r[3]) if r[3] is not None else 0
            version = int(r[4]) if r[4] is not None else 0
            display_name = r[5] or ""
            sync1 = r[6] or ""
            sync2 = r[7] or ""
            sourceid = r[8] or ""
            current[raw_id] = (version, deleted)
            meta[raw_id] = (contact_id, account_id, deleted, version, display_name, sync1, sync2, sourceid)

        to_added = []
        to_modified = []
        to_deleted = []

        with self.cache_lock:
            prev = dict(self.last_seen)

        for raw_id, (_pver, pdel) in prev.items():
            if raw_id not in current:
                to_deleted.append(raw_id)
            else:
                _cver, cdel = current[raw_id]
                if pdel == 0 and cdel != 0:
                    to_deleted.append(raw_id)

        for raw_id, (cver, cdel) in current.items():
            if cdel != 0:
                continue
            if raw_id not in prev:
                to_added.append(raw_id)
            else:
                pver, pdel = prev[raw_id]
                if pdel != 0:
                    to_added.append(raw_id)
                elif cver != pver:
                    to_modified.append(raw_id)

        if force_emit:
            to_added = []
            to_deleted = []
            to_modified = [rid for rid, (_v, d) in current.items() if d == 0]

        changed_ids = list({*to_added, *to_modified})
        phones = self.fetch_phones(conn, changed_ids)
        names = self.fetch_names(conn, changed_ids)

        now_ts = float(time.time())

        for rid in to_added:
            if rid not in meta:
                continue
            payload = self.build_payload(meta[rid], rid, names, phones, now_ts)
            logging.debug(
                f"Android contact added raw_contact_id={payload.get('raw_contact_id')} "
                f"name={payload.get('display_name')!r} phones={payload.get('phones')}"
            )
            self.emit_contact_changed(payload, "added")

        for rid in to_modified:
            if rid not in meta:
                continue
            payload = self.build_payload(meta[rid], rid, names, phones, now_ts)
            logging.debug(
                f"Android contact modified raw_contact_id={payload.get('raw_contact_id')} "
                f"name={payload.get('display_name')!r} phones={payload.get('phones')}"
            )
            self.emit_contact_changed(payload, "modified")

        for rid in to_deleted:
            payload = {
                "raw_contact_id": int(rid),
                "contact_id": 0,
                "account_id": 0,
                "display_name": "",
                "phones": [],
                "emails": [],
                "updated_ts": now_ts,
                "sync1": "",
                "sync2": "",
                "sourceid": "",
                "is_andromeda_owned": False,
                "version": 0,
                "deleted": 1,
            }
            logging.debug(f"Android contact removed raw_contact_id={rid}")
            self.emit_contact_changed(payload, "deleted")

        with self.cache_lock:
            self.last_seen = current

        conn.close()

    def build_payload(self, meta_tuple, raw_id, names, phones, now_ts):
        contact_id, account_id, deleted, version, display_name, sync1, sync2, sourceid = meta_tuple
        name = names.get(raw_id) or display_name
        phs = phones.get(raw_id, [])
        is_owned = self.is_andromeda_owned(account_id, sync1)
        return {
            "raw_contact_id": int(raw_id),
            "contact_id": int(contact_id),
            "account_id": int(account_id),
            "display_name": str(name or ""),
            "phones": list(phs),
            "emails": [],
            "updated_ts": float(now_ts),
            "sync1": str(sync1 or ""),
            "sync2": str(sync2 or ""),
            "sourceid": str(sourceid or ""),
            "is_andromeda_owned": bool(is_owned),
            "version": int(version),
            "deleted": int(deleted),
        }

    def is_andromeda_owned(self, account_id, sync1):
        andromeda_account_id = self.get_andromeda_account_id()
        if andromeda_account_id is None:
            return False

        sync1_s = (sync1 or "").strip()
        return (
            int(account_id or 0) == int(andromeda_account_id)
            and sync1_s == self.ANDROMEDA_OWNER_MARKER
        )

    def fetch_phones(self, conn, raw_ids):
        if not raw_ids:
            return {}
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(raw_ids))
        cur.execute(
            f"""
            SELECT d.raw_contact_id, d.data1
            FROM data d
            JOIN mimetypes m ON m._id = d.mimetype_id
            WHERE m.mimetype = 'vnd.android.cursor.item/phone_v2'
              AND d.raw_contact_id IN ({placeholders})
            """,
            tuple(raw_ids),
        )
        out = {}
        for rid, phone in cur.fetchall():
            rid_i = int(rid)
            if not phone:
                continue
            out.setdefault(rid_i, []).append(str(phone))
        return out

    def fetch_names(self, conn, raw_ids):
        if not raw_ids:
            return {}
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(raw_ids))
        cur.execute(
            f"""
            SELECT d.raw_contact_id, d.data1
            FROM data d
            JOIN mimetypes m ON m._id = d.mimetype_id
            WHERE m.mimetype = 'vnd.android.cursor.item/name'
              AND d.raw_contact_id IN ({placeholders})
            """,
            tuple(raw_ids),
        )
        out = {}
        for rid, name in cur.fetchall():
            if name:
                out[int(rid)] = str(name)
        return out
