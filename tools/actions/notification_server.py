# Copyright 2025 Bardia Moshiri
# SPDX-License-Identifier: GPL-3.0-or-later

import re
import time
import socket
import logging
import threading
import subprocess

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

NETLINK_KOBJECT_UEVENT = 15
BUFFER_SIZE = 4096

ROOTFS_PATH = '/var/lib/andromeda/rootfs'
LXC_PATH = "/var/lib/andromeda/lxc"
LXC_NAME = "andromeda"

POLL_INTERVAL_SEC = 3

MCS_SERVICE_COMPONENT = "com.google.android.gms/org.microg.gms.gcm.McsService"
MCS_SERVICE_INTERVAL_SEC = 120

running = False
loop_thread = None

mainloop = None
mainloop_thread = None

mcs_thread = None
mcs_started = False
mcs_lock = threading.Lock()
mcs_stop_event = threading.Event()

dbus_ready = threading.Event()
dbus_interface_obj = None

class INotification(dbus.service.Object):
    def __init__(self, bus_name, object_path='/io/furios/Andromeda/Notification'):
        super().__init__(bus_name, object_path)

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='ssssssbbbt')
    def NewMessage(self, msg_hash, msg_id, package_name, ticker, title, text,
                   is_foreground_service, is_group_summary, show_light, when):
        logging.debug(
            f"NewMessage msg_hash={msg_hash} msg_id={msg_id} package={package_name} "
            f"ticker={ticker} title={title} text={text} "
            f"is_foreground_service={is_foreground_service} "
            f"is_group_summary={is_group_summary} show_light={show_light} when={when}"
        )

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='sssssssbbbt')
    def UpdateMessage(self, msg_hash, replaces_hash, msg_id, package_name, ticker, title, text,
                      is_foreground_service, is_group_summary, show_light, when):
        logging.debug(
            f"UpdateMessage msg_hash={msg_hash} replaces_hash={replaces_hash} "
            f"msg_id={msg_id} package={package_name} "
            f"ticker={ticker} title={title} text={text} "
            f"is_foreground_service={is_foreground_service} "
            f"is_group_summary={is_group_summary} show_light={show_light} when={when}"
        )

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='s')
    def DeleteMessage(self, msg_hash):
        logging.debug(f"DeleteMessage msg_hash={msg_hash}")

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='ssssssbbbt')
    def NewMprisMessage(self, msg_hash, msg_id, package_name, ticker, title, text,
                        is_foreground_service, is_group_summary, show_light, when):
        logging.debug(
            f"NewMprisMessage msg_hash={msg_hash} msg_id={msg_id} package={package_name} "
            f"ticker={ticker} title={title} text={text} "
            f"is_foreground_service={is_foreground_service} "
            f"is_group_summary={is_group_summary} show_light={show_light} when={when}"
        )

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='sssssssbbbt')
    def UpdateMprisMessage(self, msg_hash, replaces_hash, msg_id, package_name, ticker, title, text,
                           is_foreground_service, is_group_summary, show_light, when):
        logging.debug(
            f"UpdateMprisMessage msg_hash={msg_hash} replaces_hash={replaces_hash} "
            f"msg_id={msg_id} package={package_name} "
            f"ticker={ticker} title={title} text={text} "
            f"is_foreground_service={is_foreground_service} "
            f"is_group_summary={is_group_summary} show_light={show_light} when={when}"
        )

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='s')
    def RemoveMprisMessage(self, msg_hash):
        logging.debug("RemoveMprisMessage {msg_hash}")

    @dbus.service.method(dbus_interface='io.furios.Andromeda.Notification', in_signature='', out_signature='b')
    def MprisPause(self):
        logging.debug("MprisPause")
        return bool(media_session_dispatch("pause"))

    @dbus.service.method(dbus_interface='io.furios.Andromeda.Notification', in_signature='', out_signature='b')
    def MprisPlay(self):
        logging.debug("MprisPlay")
        return bool(media_session_dispatch("play"))

    @dbus.service.method(dbus_interface='io.furios.Andromeda.Notification', in_signature='', out_signature='b')
    def MprisNext(self):
        logging.debug("MprisNext")
        return bool(media_session_dispatch("next"))

    @dbus.service.method(dbus_interface='io.furios.Andromeda.Notification', in_signature='', out_signature='b')
    def MprisPrevious(self):
        logging.debug("MprisPrevious")
        return bool(media_session_dispatch("previous"))

def is_mounted(path):
    with open('/proc/mounts', 'r') as f:
        for line in f:
            if path in line.split():
                return True
    return False

def monitor_mounts():
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT)
    sock.bind((0, -1))

    try:
        while True:
            data = sock.recv(BUFFER_SIZE)
            messages = data.decode('utf-8', errors='ignore').split('\0')
            event_info = {}
            for message in messages:
                if '=' in message:
                    key, value = message.split('=', 1)
                    event_info[key] = value

            if event_info.get("SUBSYSTEM") == "block" and event_info.get("ACTION") in {"add", "remove", "change"}:
                if is_mounted(ROOTFS_PATH):
                    break
    except Exception:
        pass
    finally:
        sock.close()

def run_in_android_shell(shell_cmd: str) -> tuple[int, str, str]:
    cmd = [
        "lxc-attach", "-P", LXC_PATH, "-n", LXC_NAME, "--clear-env", "--",
        "/system/bin/sh", "-c", shell_cmd
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_b, err_b = proc.communicate()
    out = out_b.decode('utf-8', errors='replace') if out_b else ""
    err = err_b.decode('utf-8', errors='replace') if err_b else ""
    return proc.returncode, out, err

def media_session_dispatch(key: str) -> bool:
    rc, out, err = run_in_android_shell(f"cmd media_session dispatch {key}")
    if rc != 0:
        clean_err = (err or "").strip()
        logging.warning(f"media_session dispatch failed key={key} rc={rc} err={clean_err}")
        return False
    return True

def sh_quote_single(s: str) -> str:
    return s.replace("'", r"'\''")

def cmd_notification_list() -> list[str]:
    rc, out, err = run_in_android_shell("cmd notification list")
    if rc != 0:
        raise RuntimeError(f"cmd notification list failed rc={rc}: {err.strip()}")
    keys = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            keys.append(line)
    return keys

def cmd_notification_get(notification_key: str) -> str:
    key_escaped = sh_quote_single(notification_key)
    rc, out, err = run_in_android_shell(f"cmd notification get '{key_escaped}'")
    if rc != 0:
        raise RuntimeError(f"cmd notification get failed key={notification_key} rc={rc}: {err.strip()}")
    return out

def strip_wrapped_parens(s: str) -> str:
    s2 = s.rstrip()
    if s2.endswith(")"):
        return s2[:-1].rstrip()
    return s2

def parse_notification_get_output(get_output: str, notification_key: str) -> dict:
    n = {
        "key": notification_key,
        "package_name": "",
        "msg_id": "",
        "ticker": "",
        "title": "",
        "text": "",
        "category": "",
        "is_foreground_msg": False,
        "is_group_summary": False,
        "show_light": False,
        "when": 0,
    }

    # Header example:
    # NotificationRecord(0x09070b35: pkg=org.telegram.messenger ... id=-2059424501 ... key=0|...)
    header = None
    for line in get_output.splitlines():
        if line.startswith("NotificationRecord("):
            header = line
            break

    if header:
        m_pkg = re.search(r"\bpkg=([^\s]+)", header)
        if m_pkg:
            n["package_name"] = m_pkg.group(1).strip()

        m_id = re.search(r"\bid=([-\d]+)", header)
        if m_id:
            n["msg_id"] = m_id.group(1).strip()

        m_cat = re.search(r"\bcategory=([^\s\)]+)", header)
        if m_cat:
            n["category"] = m_cat.group(1).strip()

    multiline_text = None

    for raw in get_output.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()

        # Multiline android.text parsing
        if multiline_text is not None:
            multiline_text += "\n" + s
            if s.endswith(")"):
                n["text"] = strip_wrapped_parens(multiline_text)
                multiline_text = None
            continue

        if s.startswith("category=") and not n["category"]:
            n["category"] = s.replace("category=", "").strip()
            continue

        # flags=0x10
        if s.startswith("flags="):
            try:
                flags = int(s.replace("flags=", "").strip(), 0)
            except Exception:
                flags = 0
            # 0x40 FLAG_FOREGROUND_SERVICE, 0x200 FLAG_GROUP_SUMMARY
            n["is_foreground_msg"] = (flags & 0x00000040) != 0
            n["is_group_summary"] = (flags & 0x00000200) != 0
            continue

        # when=...
        if s.startswith("when="):
            val = s.replace("when=", "").strip()
            try:
                n["when"] = int(val, 0)
            except Exception:
                n["when"] = 0
            continue

        # tickerText=null
        if s.startswith("tickerText="):
            n["ticker"] = s.replace("tickerText=", "").strip()
            continue

        # mLight= null / something
        if s.startswith("mLight="):
            n["show_light"] = s.replace("mLight=", "").strip() != "null"
            continue

        # extras title
        if s.startswith("android.title="):
            m = re.search(r"android\.title=\w+\s*\((.*)\)$", s)
            if m:
                n["title"] = m.group(1).strip()
            continue

        # extras text
        if s.startswith("android.text="):
            m = re.search(r"android\.text=\w+\s*\((.*)$", s)
            if m:
                content = m.group(1).strip()
                multiline_text = content
                if multiline_text.endswith(")"):
                    n["text"] = strip_wrapped_parens(multiline_text)
                    multiline_text = None
            continue

    return n

def notifications_equal(a: dict, b: dict) -> bool:
    keys = (
        "package_name", "msg_id", "ticker", "title", "text",
        "is_foreground_msg", "is_group_summary", "show_light", "when"
    )
    return all(a.get(k) == b.get(k) for k in keys)

def run_mcs_service_once():
    if not is_mounted(ROOTFS_PATH):
        return
    rc, out, err = run_in_android_shell(f"am startservice -n {MCS_SERVICE_COMPONENT}")
    if rc != 0:
        clean_err = (err or "").strip()
        logging.warning(f"Failed to start McsService rc={rc} err={clean_err}")

def mcs_service_loop():
    try:
        # wait for the system to fully boot up
        remaining = 15
        while running and remaining > 0 and not mcs_stop_event.is_set():
            time.sleep(1)
            remaining -= 1

        if not running or mcs_stop_event.is_set():
            return

        run_mcs_service_once()

        while running and not mcs_stop_event.is_set():
            if not is_mounted(ROOTFS_PATH):
                return

            remaining = MCS_SERVICE_INTERVAL_SEC
            while running and remaining > 0 and not mcs_stop_event.is_set():
                time.sleep(1)
                remaining -= 1

            if running and not mcs_stop_event.is_set():
                run_mcs_service_once()
    finally:
        global mcs_thread, mcs_started
        with mcs_lock:
            mcs_started = False
            mcs_thread = None
        mcs_stop_event.clear()

def ensure_mcs_scheduler_started():
    global mcs_thread, mcs_started
    with mcs_lock:
        if mcs_started:
            if mcs_thread is not None and mcs_thread.is_alive():
                return
            mcs_started = False
            mcs_thread = None

        mcs_stop_event.clear()
        mcs_started = True
        mcs_thread = threading.Thread(target=mcs_service_loop, name="mcs-keepalive", daemon=True)
        mcs_thread.start()
        logging.info("Started McsService keepalive")

def run_dbus_mainloop():
    global mainloop, dbus_interface_obj

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    system_bus = dbus.SystemBus()
    bus_name = dbus.service.BusName('io.furios.Andromeda.Notification', system_bus)
    dbus_interface_obj = INotification(bus_name, object_path='/io/furios/Andromeda/Notification')

    dbus_ready.set()

    mainloop = GLib.MainLoop()
    mainloop.run()

def poll_notifications_loop():
    global dbus_interface_obj

    dbus_ready.wait()

    interface = dbus_interface_obj

    old_notifications: dict[str, dict] = {}
    old_mpris: dict[str, dict] = {}

    logging.info("Starting notification poll loop")

    while running:
        if not is_mounted(ROOTFS_PATH):
            mcs_stop_event.set()
            monitor_mounts()

        try:
            keys = cmd_notification_list()
            ensure_mcs_scheduler_started()
        except Exception as e:
            logging.error(f"Failed to list notifications: {e}")
            time.sleep(POLL_INTERVAL_SEC)
            continue

        notifications: dict[str, dict] = {}
        mpris: dict[str, dict] = {}

        # analyse and send notifications
        for key in keys:
            try:
                out = cmd_notification_get(key)
                n = parse_notification_get_output(out, key)

                # skip system-generated notifications from the Android system package
                if n.get("package_name") == "android":
                    continue

                # this happens e.g. for foreground applications when they start.
                # currently they are ignored, but they could also be transformed
                # into a "<app> started in background" message
                if (n["ticker"] in ("null", "") and (n["title"] == "" or n["text"] == "")):
                    continue

                if n.get("category") == "transport":
                    mpris[key] = n
                else:
                    notifications[key] = n
            except Exception as e:
                logging.error(f"Failed to get/parse notification key={key}: {e}")
                continue

        # Notification
        for key, n in notifications.items():
            if key not in old_notifications:
                interface.NewMessage(
                    key,
                    n["msg_id"],
                    n["package_name"],
                    n["ticker"],
                    n["title"],
                    n["text"],
                    n["is_foreground_msg"],
                    n["is_group_summary"],
                    n["show_light"],
                    int(n["when"])
                )
            else:
                if not notifications_equal(n, old_notifications[key]):
                    # Update in place
                    interface.UpdateMessage(
                        key,
                        key,
                        n["msg_id"],
                        n["package_name"],
                        n["ticker"],
                        n["title"],
                        n["text"],
                        n["is_foreground_msg"],
                        n["is_group_summary"],
                        n["show_light"],
                        int(n["when"])
                    )

        # Deletions
        for key in list(old_notifications.keys()):
            if key not in notifications:
                interface.DeleteMessage(key)

        # MPRIS (category=transport)
        for key, n in mpris.items():
            if key not in old_mpris:
                interface.NewMprisMessage(
                    key,
                    n["msg_id"],
                    n["package_name"],
                    n["ticker"],
                    n["title"],
                    n["text"],
                    n["is_foreground_msg"],
                    n["is_group_summary"],
                    n["show_light"],
                    int(n["when"])
                )
            else:
                if not notifications_equal(n, old_mpris[key]):
                    interface.UpdateMprisMessage(
                        key,
                        key,
                        n["msg_id"],
                        n["package_name"],
                        n["ticker"],
                        n["title"],
                        n["text"],
                        n["is_foreground_msg"],
                        n["is_group_summary"],
                        n["show_light"],
                        int(n["when"])
                    )

        for key in list(old_mpris.keys()):
            if key not in mpris:
                interface.RemoveMprisMessage(key)

        old_notifications = notifications
        old_mpris = mpris
        time.sleep(POLL_INTERVAL_SEC)

def start(_args):
    global running, loop_thread, mainloop_thread

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    running = True

    mainloop_thread = threading.Thread(target=run_dbus_mainloop, name="dbus-mainloop")
    mainloop_thread.start()

    loop_thread = threading.Thread(target=poll_notifications_loop, name="notification-poll")
    loop_thread.start()

def stop(_args):
    global running, loop_thread, mainloop, mainloop_thread, mcs_thread, mcs_started, dbus_interface_obj
    running = False

    if loop_thread is not None:
        loop_thread.join()
        loop_thread = None

    if mainloop is not None:
        try:
            mainloop.quit()
        except Exception:
            pass
        mainloop = None

    if mainloop_thread is not None:
        mainloop_thread.join()
        mainloop_thread = None

    if mcs_thread is not None:
        mcs_thread.join(timeout=2)
        mcs_thread = None

    mcs_started = False
    dbus_interface_obj = None
    dbus_ready.clear()
