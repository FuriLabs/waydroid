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

class INotification(dbus.service.Object):
    def __init__(self, bus_name, object_path='/io/furios/Andromeda/Notification'):
        super().__init__(bus_name, object_path)

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='ssssssbbbt')
    def NewMessage(self, msg_hash, msg_id, package_name, ticker, title, text,
                   is_foreground_service, is_group_summary, show_light, when):
        pass

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='sssssssbbbt')
    def UpdateMessage(self, msg_hash, replaces_hash, msg_id, package_name, ticker, title, text,
                      is_foreground_service, is_group_summary, show_light, when):
        pass

    @dbus.service.signal(dbus_interface='io.furios.Andromeda.Notification', signature='s')
    def DeleteMessage(self, msg_hash):
        pass

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
        logging.warning("Failed to start McsService rc=%s err=%s", rc, (err or "").strip())

def mcs_service_loop():
    # wait for the system to fully boot up
    remaining = 15
    while running and remaining > 0:
        time.sleep(1)
        remaining -= 1

    if not running:
        return

    run_mcs_service_once()

    while running:
        remaining = MCS_SERVICE_INTERVAL_SEC
        while running and remaining > 0:
            time.sleep(1)
            remaining -= 1
        if running:
            run_mcs_service_once()

def ensure_mcs_scheduler_started():
    global mcs_thread, mcs_started
    with mcs_lock:
        if mcs_started:
            return
        mcs_started = True
        mcs_thread = threading.Thread(target=mcs_service_loop, name="mcs-keepalive", daemon=True)
        mcs_thread.start()
        logging.info("Started McsService keepalive")

def get_notifications_loop(_old_notification):
    old_notifications: dict[str, dict] = {}

    system_bus = dbus.SystemBus()
    bus_name = dbus.service.BusName('io.furios.Andromeda.Notification', system_bus)
    interface = INotification(bus_name, object_path='/io/furios/Andromeda/Notification')

    logging.info("Starting notification server service")

    while running:
        if not is_mounted(ROOTFS_PATH):
            monitor_mounts()

        try:
            keys = cmd_notification_list()
            ensure_mcs_scheduler_started()
        except Exception as e:
            logging.error("Failed to list notifications: %s", e)
            time.sleep(POLL_INTERVAL_SEC)
            continue

        notifications: dict[str, dict] = {}

        for key in keys:
            try:
                out = cmd_notification_get(key)
                n = parse_notification_get_output(out, key)
                notifications[key] = n
            except Exception as e:
                logging.error("Failed to get/parse notification key=%s: %s", key, e)
                continue

        # analyse and send notifications
        for key, n in notifications.items():
            # skip system-generated notifications from the Android system package
            if n.get("package_name") == "android":
                continue

            # this happens e.g. for foreground applications when they start.
            # currently they are ignored, but they could also be transformed
            # into a "<app> started in background" message
            if (n["ticker"] in ("null", "") and (n["title"] == "" or n["text"] == "")):
                continue

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

        old_notifications = notifications
        time.sleep(POLL_INTERVAL_SEC)

def run_dbus_mainloop():
    global mainloop
    mainloop = GLib.MainLoop()
    mainloop.run()

def start(_args):
    global running, loop_thread, mainloop_thread

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    running = True

    loop_thread = threading.Thread(target=get_notifications_loop, args=({},))
    loop_thread.start()
    mainloop_thread = threading.Thread(target=run_dbus_mainloop)
    mainloop_thread.start()

def stop(_args):
    global running, loop_thread, mainloop, mainloop_thread, mcs_thread, mcs_started
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
