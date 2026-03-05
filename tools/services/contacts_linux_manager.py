# Copyright 2026 Bardia Moshiri
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import time
import sqlite3
import logging

import dbus

import gi
gi.require_version("EDataServer", "1.2")
gi.require_version("EBook", "1.2")
gi.require_version("EBookContacts", "1.2")
from gi.repository import EDataServer, EBook, EBookContacts, GLib

import tools.config
from tools.helpers.ipc import DBusContainerService

class ContactsMappingDB:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.init()

    def conn(self):
        return sqlite3.connect(self.db_path, timeout=2.0)

    def init(self):
        conn = self.conn()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
              linux_uid TEXT NOT NULL,
              android_raw_contact_id INTEGER NOT NULL,
              source TEXT,
              created_ts REAL,
              last_sync_ts REAL,
              PRIMARY KEY (linux_uid, android_raw_contact_id),
              UNIQUE(linux_uid),
              UNIQUE(android_raw_contact_id)
            )
            """
        )
        conn.commit()
        conn.close()

    def get_linux_uid_by_android(self, raw_id):
        conn = self.conn()
        cur = conn.cursor()
        cur.execute(f"SELECT linux_uid FROM mappings WHERE android_raw_contact_id=? LIMIT 1", (int(raw_id),))
        row = cur.fetchone()
        conn.close()
        return str(row[0]) if row and row[0] else None

    def get_android_raw_id_by_linux(self, linux_uid):
        conn = self.conn()
        cur = conn.cursor()
        cur.execute(f"SELECT android_raw_contact_id FROM mappings WHERE linux_uid=? LIMIT 1", (linux_uid,))
        row = cur.fetchone()
        conn.close()
        return int(row[0]) if row and row[0] is not None else None

    def has_android(self, raw_id):
        return self.get_linux_uid_by_android(raw_id) is not None

    def has_linux(self, linux_uid):
        return self.get_android_raw_id_by_linux(linux_uid) is not None

    def link(self, linux_uid, raw_id, source):
        now = float(time.time())
        conn = self.conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO mappings (linux_uid, android_raw_contact_id, source, created_ts, last_sync_ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (linux_uid, int(raw_id), source, now, now),
        )
        cur.execute(
            """
            UPDATE mappings
            SET source=?, last_sync_ts=?
            WHERE linux_uid=? AND android_raw_contact_id=?
            """,
            (source, now, linux_uid, int(raw_id)),
        )
        conn.commit()
        conn.close()

    def unlink_by_linux(self, linux_uid):
        conn = self.conn()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM mappings WHERE linux_uid=?", (linux_uid,))
        conn.commit()
        conn.close()

    def unlink_by_android(self, raw_id):
        conn = self.conn()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM mappings WHERE android_raw_contact_id=?", (int(raw_id),))
        conn.commit()
        conn.close()

class ContactsLinuxManager:
    SUPPRESS_SECONDS = 3.0

    def __init__(self, args, session):
        self.args = args
        self.session = session
        self.bus = dbus.SystemBus()
        self.container = DBusContainerService()

        cfg = tools.config.load(args)
        self.import_book_name = cfg["andromeda"].get("contact_sync_book_name", "Andromeda Contacts")
        self.export_book_uid = cfg["andromeda"].get("contact_sync_export_book_uid", "system-address-book")
        self.enabled = str(cfg["andromeda"].get("contact_sync_enabled", "False")).lower() == "true"

        config_dir = os.path.join(session["host_user"], ".config", "andromeda")
        self.mapping_db = ContactsMappingDB(os.path.join(config_dir, "contacts-sync.db"))

        self.source_registry = None
        self.import_source = None
        self.export_source = None
        self.import_book_client = None
        self.export_book_client = None
        self.view = None

        self.suppress_linux = {}
        self.suppress_android = {}

    def start(self):
        self.subscribe_signal()
        self.subscribe_sync_enabled_signal()
        self.subscribe_linux_contact_imported_signal()
        self.subscribe_android_contact_updated_signal()
        self.subscribe_android_contact_removed_signal()

        if not self.enabled:
            logging.debug("Contacts sync disabled. ContactsLinuxManager waiting for enable signal")
            return

        self.activate_sync()

    def stop(self):
        self.deactivate_sync()
        self.unsubscribe_signal()
        self.unsubscribe_sync_enabled_signal()
        self.unsubscribe_linux_contact_imported_signal()
        self.unsubscribe_android_contact_updated_signal()
        self.unsubscribe_android_contact_removed_signal()
        logging.debug("ContactsLinuxManager stopped")

    def activate_sync(self):
        if not self.ensure_eds_books():
            logging.error("ContactsLinuxManager failed to ensure EDS books. not starting")
            return

        try:
            self.baseline_import()
        except Exception as e:
            logging.warning(f"Baseline import failed: {e}")

        try:
            self.baseline_export()
        except Exception as e:
            logging.warning(f"Baseline export failed: {e}")

        self.start_eds_view()
        logging.debug("ContactsLinuxManager started")

    def deactivate_sync(self):
        try:
            if self.view:
                self.view.stop()
        except Exception as e:
            logging.warning(f"Failed to stop EDS view: {e}")
        self.view = None
        logging.debug("ContactsLinuxManager sync deactivated")

    def subscribe_signal(self):
        self.bus.add_signal_receiver(
            self.on_android_contact_changed,
            signal_name="AndroidContactChanged",
            dbus_interface="io.furios.Andromeda.ContainerManager",
            bus_name="io.furios.Andromeda.Container",
            path="/ContainerManager",
        )

    def unsubscribe_signal(self):
        try:
            self.bus.remove_signal_receiver(
                self.on_android_contact_changed,
                signal_name="AndroidContactChanged",
                dbus_interface="io.furios.Andromeda.ContainerManager",
                bus_name="io.furios.Andromeda.Container",
                path="/ContainerManager",
            )
        except Exception as e:
            logging.warning(f"Failed to unsubscribe AndroidContactChanged signal: {e}")

    def subscribe_sync_enabled_signal(self):
        self.bus.add_signal_receiver(
            self.on_contacts_sync_enabled_changed,
            signal_name="ContactsSyncEnabledChanged",
            dbus_interface="io.furios.Andromeda.ContainerManager",
            bus_name="io.furios.Andromeda.Container",
            path="/ContainerManager",
        )

    def unsubscribe_sync_enabled_signal(self):
        try:
            self.bus.remove_signal_receiver(
                self.on_contacts_sync_enabled_changed,
                signal_name="ContactsSyncEnabledChanged",
                dbus_interface="io.furios.Andromeda.ContainerManager",
                bus_name="io.furios.Andromeda.Container",
                path="/ContainerManager",
            )
        except Exception as e:
            logging.warning(f"Failed to unsubscribe ContactsSyncEnabledChanged signal: {e}")

    def subscribe_linux_contact_imported_signal(self):
        self.bus.add_signal_receiver(
            self.on_linux_contact_imported,
            signal_name="LinuxContactImported",
            dbus_interface="io.furios.Andromeda.ContainerManager",
            bus_name="io.furios.Andromeda.Container",
            path="/ContainerManager",
        )

    def unsubscribe_linux_contact_imported_signal(self):
        try:
            self.bus.remove_signal_receiver(
                self.on_linux_contact_imported,
                signal_name="LinuxContactImported",
                dbus_interface="io.furios.Andromeda.ContainerManager",
                bus_name="io.furios.Andromeda.Container",
                path="/ContainerManager",
            )
        except Exception as e:
            logging.warning(f"Failed to unsubscribe LinuxContactImported signal: {e}")

    def subscribe_android_contact_updated_signal(self):
        self.bus.add_signal_receiver(
            self.on_android_contact_updated,
            signal_name="AndroidContactUpdated",
            dbus_interface="io.furios.Andromeda.ContainerManager",
            bus_name="io.furios.Andromeda.Container",
            path="/ContainerManager",
        )

    def unsubscribe_android_contact_updated_signal(self):
        try:
            self.bus.remove_signal_receiver(
                self.on_android_contact_updated,
                signal_name="AndroidContactUpdated",
                dbus_interface="io.furios.Andromeda.ContainerManager",
                bus_name="io.furios.Andromeda.Container",
                path="/ContainerManager",
            )
        except Exception as e:
            logging.warning(f"Failed to unsubscribe AndroidContactUpdated signal: {e}")

    def subscribe_android_contact_removed_signal(self):
        self.bus.add_signal_receiver(
            self.on_android_contact_removed,
            signal_name="AndroidContactRemoved",
            dbus_interface="io.furios.Andromeda.ContainerManager",
            bus_name="io.furios.Andromeda.Container",
            path="/ContainerManager",
        )

    def unsubscribe_android_contact_removed_signal(self):
        try:
            self.bus.remove_signal_receiver(
                self.on_android_contact_removed,
                signal_name="AndroidContactRemoved",
                dbus_interface="io.furios.Andromeda.ContainerManager",
                bus_name="io.furios.Andromeda.Container",
                path="/ContainerManager",
            )
        except Exception as e:
            logging.warning(f"Failed to unsubscribe AndroidContactRemoved signal: {e}")

    def on_contacts_sync_enabled_changed(self, enabled):
        new_enabled = bool(enabled)

        if new_enabled == self.enabled:
            return

        self.enabled = new_enabled

        if self.enabled:
            logging.debug("Contacts sync enabled on Linux side")
            self.activate_sync()
        else:
            logging.debug("Contacts sync disabled on Linux side")
            self.deactivate_sync()

    def start_eds_view(self):
        if not self.export_book_client:
            return
        try:
            query = '(contains "x-evolution-any-field" "")'
            ok, view = self.export_book_client.get_view_sync(query, None)
            if not ok or not view:
                logging.warning("Failed to create EDS view for contacts sync")
                return

            self.view = view
            self.view.connect("objects-added", self.on_eds_objects_added)
            self.view.connect("objects-modified", self.on_eds_objects_modified)
            self.view.connect("objects-removed", self.on_eds_objects_removed)
            self.view.start()

            book_name = self.export_source.get_display_name() if self.export_source else "unknown"
            logging.debug(f"EDS contacts view started on export/watch book {book_name!r}")
        except Exception as e:
            logging.warning(f"Failed to start EDS contacts view: {e}")

    def suppress_linux_uid(self, linux_uid):
        if linux_uid:
            self.suppress_linux[linux_uid] = time.time() + self.SUPPRESS_SECONDS

    def suppress_android_raw_id(self, raw_id):
        if raw_id > 0:
            self.suppress_android[int(raw_id)] = time.time() + self.SUPPRESS_SECONDS

    def is_linux_suppressed(self, linux_uid):
        if not linux_uid:
            return False
        now = time.time()
        expiry = self.suppress_linux.get(linux_uid, 0)
        if expiry < now:
            self.suppress_linux.pop(linux_uid, None)
            return False
        return True

    def is_android_suppressed(self, raw_id):
        if raw_id <= 0:
            return False
        now = time.time()
        expiry = self.suppress_android.get(int(raw_id), 0)
        if expiry < now:
            self.suppress_android.pop(int(raw_id), None)
            return False
        return True

    def make_dbus_string_array(self, values):
        out = []
        for v in values or []:
            s = str(v).strip()
            if s:
                out.append(dbus.String(s))
        return dbus.Array(out, signature="s")

    def make_import_linux_contact_payload(self, linux_uid, display_name, phones):
        return dbus.Dictionary(
            {
                "linux_uid": dbus.String(str(linux_uid or "")),
                "display_name": dbus.String(str(display_name or "")),
                "phones": self.make_dbus_string_array(phones),
            },
            signature="sv",
        )

    def make_update_android_contact_payload(self, raw_contact_id, display_name, phones):
        return dbus.Dictionary(
            {
                "raw_contact_id": dbus.Int32(int(raw_contact_id or 0)),
                "display_name": dbus.String(str(display_name or "")),
                "phones": self.make_dbus_string_array(phones),
            },
            signature="sv",
        )

    def parse_contact(self, contact):
        if not isinstance(contact, dict):
            return None

        def get_str(key, default=""):
            try:
                v = contact.get(key, default)
                return str(v) if v is not None else default
            except Exception as e:
                logging.warning(f"Failed to parse string field {key!r}: {e}")
                return default

        def get_int(key, default=0):
            try:
                v = contact.get(key, default)
                if v is None:
                    return default
                return int(v)
            except Exception as e:
                logging.warning(f"Failed to parse integer field {key!r}: {e}")
                return default

        def get_bool(key, default=False):
            try:
                v = contact.get(key, default)
                if v is None:
                    return default
                if isinstance(v, bool):
                    return v
                return bool(int(v)) if str(v).strip().isdigit() else bool(v)
            except Exception as e:
                logging.warning(f"Failed to parse boolean field {key!r}: {e}")
                return default

        def get_phones(key="phones"):
            v = contact.get(key, []) or []
            out = []
            try:
                for p in v:
                    s = str(p).strip()
                    if s:
                        out.append(s)
            except Exception as e:
                logging.warning(f"Failed to parse phone list field {key!r}: {e}")
                return []
            return out

        payload = {
            "raw_contact_id": get_int("raw_contact_id", 0),
            "contact_id": get_int("contact_id", 0),
            "account_id": get_int("account_id", 0),
            "display_name": get_str("display_name", "").strip(),
            "phones": get_phones("phones"),
            "emails": [],
            "updated_ts": float(time.time()),
            "sync1": get_str("sync1", ""),
            "sync2": get_str("sync2", ""),
            "sourceid": get_str("sourceid", ""),
            "is_andromeda_owned": get_bool("is_andromeda_owned", False),
            "version": get_int("version", 0),
            "deleted": get_int("deleted", 0),
        }

        if int(payload.get("raw_contact_id", 0)) <= 0:
            return None
        return payload

    def on_android_contact_changed(self, contact, change_type):
        if not self.enabled:
            return

        try:
            payload = self.parse_contact(contact)
            if not payload:
                return

            change = str(change_type)

            raw_id = int(payload.get("raw_contact_id", 0))
            if raw_id <= 0:
                return

            if self.is_android_suppressed(raw_id):
                return

            if bool(payload.get("is_andromeda_owned", False)):
                return

            if change == "deleted":
                self.remove_mapped_linux_contact(raw_id)
                return

            if self.mapping_db.has_android(raw_id):
                self.update_mapped_contact(raw_id, payload)
            else:
                linux_uid = self.import_into_eds(payload)
                if linux_uid:
                    self.mapping_db.link(linux_uid, raw_id, source="android")
        except Exception as e:
            logging.warning(f"AndroidContactChanged handler failed: {e}")

    def on_linux_contact_imported(self, linux_uid, raw_contact_id, success, error):
        linux_uid_s = str(linux_uid or "").strip()
        raw_id = int(raw_contact_id or 0)
        ok = bool(success)
        error_s = str(error or "").strip()

        if not linux_uid_s:
            return

        if not ok or raw_id <= 0:
            logging.warning(f"LinuxContactImported failed uid={linux_uid_s} error={error_s}")
            return

        old_raw_id = self.mapping_db.get_android_raw_id_by_linux(linux_uid_s)
        if old_raw_id is not None and old_raw_id != raw_id:
            logging.debug(f"Replacing mapping for uid={linux_uid_s} old_raw_contact_id={old_raw_id} new_raw_contact_id={raw_id}")
            self.mapping_db.unlink_by_linux(linux_uid_s)

        self.mapping_db.link(linux_uid_s, raw_id, source="linux")
        self.suppress_android_raw_id(raw_id)
        logging.debug(f"LinuxContactImported linked uid={linux_uid_s} raw_contact_id={raw_id}")

    def on_android_contact_updated(self, raw_contact_id, success, error):
        raw_id = int(raw_contact_id or 0)
        ok = bool(success)
        error_s = str(error or "").strip()

        if ok:
            logging.debug(f"AndroidContactUpdated completed raw_contact_id={raw_id}")
        else:
            logging.warning(f"AndroidContactUpdated failed raw_contact_id={raw_id} error={error_s}")

    def on_android_contact_removed(self, raw_contact_id, success, error):
        raw_id = int(raw_contact_id or 0)
        ok = bool(success)
        error_s = str(error or "").strip()

        if not ok:
            logging.warning(f"AndroidContactRemoved failed raw_contact_id={raw_id} error={error_s}")
            return

        linux_uid = self.mapping_db.get_linux_uid_by_android(raw_id)
        self.mapping_db.unlink_by_android(raw_id)
        self.suppress_android_raw_id(raw_id)
        logging.debug(f"AndroidContactRemoved completed raw_contact_id={raw_id} linux_uid={linux_uid or ''}")

    def baseline_import(self):
        contacts = self.container.ListAndroidContacts()
        imported = 0

        for c in contacts:
            payload = self.parse_contact(c)
            if not payload:
                continue

            if bool(payload.get("is_andromeda_owned", False)):
                continue

            raw_id = int(payload.get("raw_contact_id", 0))
            if raw_id <= 0:
                continue

            if self.mapping_db.has_android(raw_id):
                continue

            linux_uid = self.import_into_eds(payload)
            if linux_uid:
                self.mapping_db.link(linux_uid, raw_id, source="android")
                imported += 1

        logging.debug(f"Baseline import complete. Imported {imported} Android contacts into {self.import_book_name!r}")

    def baseline_export(self):
        if not self.export_book_client:
            return

        android_contacts = self.container.ListAndroidContacts()
        existing_android_raw_ids = set()

        for c in android_contacts:
            try:
                raw_id = int(c.get("raw_contact_id", 0) or 0)
                if raw_id > 0:
                    existing_android_raw_ids.add(raw_id)
            except Exception as e:
                logging.warning(f"Failed to parse Android raw_contact_id during baseline export: {e}")

        query = '(contains "x-evolution-any-field" "")'
        ok, contacts = self.export_book_client.get_contacts_sync(query, None)
        if not ok:
            return

        queued_adds = 0
        queued_updates = 0

        for obj in contacts or []:
            data = self.extract_linux_contact(obj)
            if not data:
                continue

            linux_uid = data["linux_uid"]
            raw_id = self.mapping_db.get_android_raw_id_by_linux(linux_uid)

            if raw_id is not None and raw_id not in existing_android_raw_ids:
                logging.debug(f"Removing stale mapping for Linux add uid={linux_uid} raw_contact_id={raw_id}")
                self.mapping_db.unlink_by_linux(linux_uid)
                raw_id = None

            if raw_id is None:
                try:
                    payload = self.make_import_linux_contact_payload(
                        linux_uid,
                        data["display_name"],
                        data["phones"],
                    )
                    self.container.ImportLinuxContact(payload)
                    queued_adds += 1
                    logging.debug(f"Queued Linux add sync to Android uid={linux_uid}")
                except Exception as e:
                    logging.warning(f"Failed baseline export enqueue for Linux contact uid={linux_uid}: {e}")
            else:
                try:
                    payload = self.make_update_android_contact_payload(
                        raw_id,
                        data["display_name"],
                        data["phones"],
                    )
                    self.container.UpdateAndroidContact(payload)
                    queued_updates += 1
                    logging.debug(f"Queued Linux update sync to Android uid={linux_uid} raw_contact_id={raw_id}")
                except Exception as e:
                    logging.warning(f"Failed baseline update enqueue for Linux contact uid={linux_uid} raw_contact_id={raw_id}: {e}")

        book_name = self.export_source.get_display_name() if self.export_source else "unknown"
        logging.debug(f"Baseline Linux export queued. Adds={queued_adds} Updates={queued_updates} from {book_name!r} to Android")

    def ensure_eds_books(self):
        try:
            self.source_registry = EDataServer.SourceRegistry.new_sync(None)
            sources = self.source_registry.list_sources(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK)

            self.import_source = None
            self.export_source = None

            for s in sources:
                if s.get_display_name() == self.import_book_name:
                    self.import_source = s
                    break

            if not self.import_source:
                source = EDataServer.Source(uid=str(GLib.uuid_string_random()))
                source.set_display_name(self.import_book_name)
                ext = source.get_extension(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK)
                ext.set_backend_name("local")
                self.source_registry.commit_source_sync(source, None)
                self.import_source = source

            for s in sources:
                if s.get_uid() == self.export_book_uid:
                    self.export_source = s
                    break

            if not self.export_source:
                for s in sources:
                    if s.get_uid() == "system-address-book":
                        self.export_source = s
                        break

            if not self.export_source:
                for s in sources:
                    if s.get_display_name() == "Personal":
                        self.export_source = s
                        break

            if not self.export_source:
                logging.error("Failed to find export/watch address book")
                return False

            self.import_book_client = EBook.BookClient.connect_sync(self.import_source, 30, None)
            self.export_book_client = EBook.BookClient.connect_sync(self.export_source, 30, None)
            return self.import_book_client is not None and self.export_book_client is not None
        except Exception as e:
            logging.warning(f"Failed to ensure EDS books: {e}")
            return False

    def import_into_eds(self, payload):
        if not self.import_book_client:
            return None

        raw_id = int(payload.get("raw_contact_id", 0))
        name = str(payload.get("display_name", "")).strip()
        phones = payload.get("phones", []) or []
        phones = [str(p).strip() for p in phones if str(p).strip()]

        safe_name = self.escape_vcard(name) if name else "Unknown"

        v_lines = ["BEGIN:VCARD", "VERSION:3.0"]
        v_lines.append(f"N:;{safe_name};;;")
        v_lines.append(f"FN:{safe_name}")
        for p in phones[:5]:
            v_lines.append(f"TEL;TYPE=CELL,VOICE:{self.escape_vcard(p)}")
        v_lines.append("X-ANDROMEDA-IMPORTED:1")
        v_lines.append(f"X-ANDROMEDA-ANDROID-RAW-ID:{raw_id}")
        v_lines.append("END:VCARD")
        vcard = "\n".join(v_lines)

        try:
            contact = EBookContacts.Contact.new_from_vcard(vcard)
            self.import_book_client.add_contact_sync(contact, EBookContacts.BookOperationFlags.NONE, None)

            uid = contact.get_property("id")
            if uid:
                self.suppress_linux_uid(str(uid))
                logging.debug(f"Linux contact added in import book (EDS uid={uid} raw_contact_id={raw_id} name={name!r} phones={phones})")
                return str(uid)

            uid = self.find_contact_uid_by_android_raw_id(raw_id)
            if uid:
                self.suppress_linux_uid(uid)
                logging.debug(f"Linux contact added in import book (EDS uid={uid} raw_contact_id={raw_id} name={name!r} phones={phones})")
            return uid

        except Exception as e:
            logging.warning(f"Failed to import Android contact into EDS: {e}")
            return None

    def remove_mapped_linux_contact(self, raw_id):
        if not self.import_book_client:
            return

        linux_uid = self.mapping_db.get_linux_uid_by_android(raw_id)
        if not linux_uid:
            return

        try:
            self.suppress_linux_uid(linux_uid)
            self.import_book_client.remove_contact_by_uid_sync(linux_uid, EBookContacts.BookOperationFlags.NONE, None)
            self.mapping_db.unlink_by_android(raw_id)
            logging.debug(f"Removed Linux contact for Android raw_contact_id={raw_id} linux_uid={linux_uid}")
        except Exception as e:
            logging.warning(f"Failed to remove mapped Linux contact for raw_id={raw_id}: {e}")

    def update_mapped_contact(self, raw_id, payload):
        if not self.import_book_client:
            return

        linux_uid = self.mapping_db.get_linux_uid_by_android(raw_id)
        if not linux_uid:
            return

        name = str(payload.get("display_name", "")).strip()
        phones = payload.get("phones", []) or []
        phones = [str(p).strip() for p in phones if str(p).strip()]

        safe_name = self.escape_vcard(name) if name else "Unknown"

        try:
            ok, old_contact = self.import_book_client.get_contact_sync(linux_uid, None)
            if not ok or not old_contact:
                return

            v_lines = ["BEGIN:VCARD", "VERSION:3.0"]
            v_lines.append(f"N:;{safe_name};;;")
            v_lines.append(f"FN:{safe_name}")
            for p in phones[:5]:
                v_lines.append(f"TEL;TYPE=CELL,VOICE:{self.escape_vcard(p)}")
            v_lines.append("X-ANDROMEDA-IMPORTED:1")
            v_lines.append(f"X-ANDROMEDA-ANDROID-RAW-ID:{raw_id}")
            v_lines.append("END:VCARD")
            vcard = "\n".join(v_lines)

            new_contact = EBookContacts.Contact.new_from_vcard(vcard)
            new_contact.set_property("id", linux_uid)

            self.suppress_linux_uid(linux_uid)
            self.import_book_client.modify_contact_sync(new_contact, EBookContacts.BookOperationFlags.NONE, None)

            logging.debug(
                f"Linux contact modified in import book (EDS uid={linux_uid} from raw_contact_id={raw_id} name={name!r} phones={phones})"
            )
        except Exception as e:
            logging.warning(f"Failed to update mapped EDS contact for raw_id={raw_id}: {e}")

    def find_contact_uid_by_android_raw_id(self, raw_id):
        if not self.import_book_client:
            return None

        query = '(contains "x-evolution-any-field" "")'
        try:
            ok, contacts = self.import_book_client.get_contacts_sync(query, None)
            if not ok:
                return None

            needle = f"X-ANDROMEDA-ANDROID-RAW-ID:{raw_id}"
            for c in contacts:
                uid = c.get_property("id")
                try:
                    vcard = c.to_string(1)
                except Exception as e:
                    logging.warning(f"Failed to stringify contact as format 1 for raw_id={raw_id}: {e}")
                    try:
                        vcard = c.to_string(EBookContacts.VCardFormat.VCARD_30)
                    except Exception as e:
                        logging.warning(f"Failed to stringify contact as VCARD_30 for raw_id={raw_id}: {e}")
                        vcard = ""
                if needle in vcard:
                    return str(uid) if uid else None
        except Exception as e:
            logging.warning(f"Failed to find contact uid by Android raw id {raw_id}: {e}")
            return None
        return None

    def extract_linux_contact(self, contact_obj):
        try:
            uid = str(contact_obj.get_property("id") or "").strip()
            name = str(contact_obj.get_property("full-name") or "").strip()

            phones = []
            raw_id = None

            try:
                vcard = contact_obj.to_string(1)
            except Exception as e:
                logging.warning(f"Failed to stringify Linux contact as format 1: {e}")
                try:
                    vcard = contact_obj.to_string(EBookContacts.VCardFormat.VCARD_30)
                except Exception as e:
                    logging.warning(f"Failed to stringify Linux contact as VCARD_30: {e}")
                    vcard = ""

            for line in vcard.splitlines():
                line = line.strip()
                if line.startswith("TEL"):
                    try:
                        phones.append(line.split(":", 1)[1].strip())
                    except Exception as e:
                        logging.warning(f"Failed to parse TEL line: {e}")
                elif line.startswith("X-ANDROMEDA-ANDROID-RAW-ID:"):
                    try:
                        raw_id = int(line.split(":", 1)[1].strip())
                    except Exception as e:
                        logging.warning(f"Failed to parse X-ANDROMEDA-ANDROID-RAW-ID line: {e}")

            phones = [p for p in phones if p]

            if not uid:
                return None

            return {
                "linux_uid": uid,
                "display_name": name,
                "phones": phones,
                "android_raw_contact_id": raw_id,
            }
        except Exception as e:
            logging.warning(f"Failed to extract Linux contact: {e}")
            return None

    def on_eds_objects_added(self, view, objects):
        if not self.enabled:
            return

        for obj in objects or []:
            data = self.extract_linux_contact(obj)
            if not data:
                continue

            linux_uid = data["linux_uid"]
            if self.is_linux_suppressed(linux_uid):
                continue

            if self.mapping_db.has_linux(linux_uid):
                continue

            try:
                payload = self.make_import_linux_contact_payload(
                    linux_uid,
                    data["display_name"],
                    data["phones"],
                )
                self.container.ImportLinuxContact(payload)
                logging.debug(f"Queued Linux add sync to Android uid={linux_uid}")
            except Exception as e:
                logging.warning(f"Failed to queue Linux added contact uid={linux_uid}: {e}")

    def on_eds_objects_modified(self, view, objects):
        if not self.enabled:
            return

        for obj in objects or []:
            data = self.extract_linux_contact(obj)
            if not data:
                continue

            linux_uid = data["linux_uid"]
            if self.is_linux_suppressed(linux_uid):
                continue

            raw_id = self.mapping_db.get_android_raw_id_by_linux(linux_uid)
            if raw_id is None:
                try:
                    payload = self.make_import_linux_contact_payload(
                        linux_uid,
                        data["display_name"],
                        data["phones"],
                    )
                    self.container.ImportLinuxContact(payload)
                    logging.debug(f"Queued Linux modify on Android uid={linux_uid}")
                except Exception as e:
                    logging.warning(f"Failed to queue Linux modified new contact uid={linux_uid}: {e}")
                continue

            try:
                payload = self.make_update_android_contact_payload(
                    raw_id,
                    data["display_name"],
                    data["phones"],
                )
                self.container.UpdateAndroidContact(payload)
                logging.debug(f"Queued Linux modify sync to Android uid={linux_uid} raw_contact_id={raw_id}")
            except Exception as e:
                logging.warning(f"Failed to queue Linux modified contact uid={linux_uid} raw_contact_id={raw_id}: {e}")

    def on_eds_objects_removed(self, view, objects):
        if not self.enabled:
            return

        for obj in objects or []:
            try:
                linux_uid = str(obj).strip()
            except Exception as e:
                logging.warning(f"Failed to parse removed Linux contact uid: {e}")
                continue

            if not linux_uid:
                continue

            if self.is_linux_suppressed(linux_uid):
                continue

            raw_id = self.mapping_db.get_android_raw_id_by_linux(linux_uid)
            if raw_id is None:
                continue

            try:
                self.container.RemoveAndroidContact(int(raw_id))
                logging.debug(f"Queued Linux remove sync to Android uid={linux_uid} raw_contact_id={raw_id}")
            except Exception as e:
                logging.warning(f"Failed to queue Linux removed contact uid={linux_uid} raw_contact_id={raw_id}: {e}")

    def escape_vcard(self, s):
        return (s or "").replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")

manager = None

def start(args, session):
    global manager
    if manager is not None:
        return
    manager = ContactsLinuxManager(args, session)
    manager.start()

def stop():
    global manager
    if manager is None:
        return
    manager.stop()
    manager = None
