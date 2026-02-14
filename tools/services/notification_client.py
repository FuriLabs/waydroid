# Copyright 2025 Bardia Moshiri
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import dbus
import dbus.service
import dbus.mainloop.glib
import threading
from gi.repository import GLib

from tools import helpers
from tools import config
from tools.helpers import ipc
from tools.interfaces import IPlatform
from tools.actions import app_manager

stopping = False

class AndromedaMpris(dbus.service.Object):
    BUS_NAME = "org.mpris.MediaPlayer2.andromeda"
    OBJECT_PATH = "/org/mpris/MediaPlayer2"

    def __init__(self, session_bus, android_iface):
        self.session_bus = session_bus
        self.bus_name = dbus.service.BusName(self.BUS_NAME, bus=session_bus)
        super().__init__(self.bus_name, self.OBJECT_PATH)

        self.android_iface = android_iface

        self.identity = "Andromeda"
        self.desktop_entry = "andromeda"

        self.playback_state = "Stopped"

        self.android_key = ""
        self.package_name = ""

        self.title = ""
        self.artist = ""
        self.ticker = ""

        self.art_url = ""

    def icon_path_for_package(self, package_name: str) -> str:
        try:
            return config.session_defaults["andromeda_data"] + "/icons/" + str(package_name) + ".png"
        except Exception:
            return ""

    def art_url_for_package(self, package_name: str) -> str:
        path = self.icon_path_for_package(package_name)
        if not path:
            return ""
        return "file://" + path

    def track_id_for_key(self, android_key: str) -> dbus.ObjectPath:
        safe = "".join([c if (c.isalnum() or c == "_") else "_" for c in android_key])
        if not safe:
            safe = "unknown"
        return dbus.ObjectPath(f"/io/furios/Andromeda/Mpris/Track/{safe}")

    def metadata(self) -> dbus.Dictionary:
        md = {
            "mpris:trackid": self.track_id_for_key(self.android_key) if self.android_key else dbus.ObjectPath("/"),
            "xesam:title": self.title or "",
            "xesam:artist": dbus.Array([self.artist or ""], signature="s"),
        }

        if self.art_url:
            md["mpris:artUrl"] = self.art_url

        return dbus.Dictionary(md, signature="sv")

    def playback_status(self) -> str:
        return self.playback_state

    def emit_properties_changed(self, iface: str, changed: dict):
        self.PropertiesChanged(
            iface,
            dbus.Dictionary(changed, signature="sv"),
            dbus.Array([], signature="s")
        )

    def call_android_control(self, method_name: str):
        try:
            if self.android_iface is None:
                logging.debug(f"Android iface is None. ignoring control call: {method_name}")
                return

            fn = getattr(self.android_iface, method_name, None)
            if fn is None:
                logging.error(f"Android control method missing: {method_name}")
                return

            fn()
        except dbus.DBusException as e:
            logging.error(f"Android control call failed ({method_name}): {e}")
        except Exception as e:
            logging.error("Android control call error ({method_name}): {e}")

    def set_playback_state(self, new_state: str):
        if new_state not in ("Playing", "Paused", "Stopped"):
            return
        if self.playback_state == new_state:
            return

        self.playback_state = new_state
        self.emit_properties_changed("org.mpris.MediaPlayer2.Player", {
            "PlaybackStatus": self.playback_status(),
        })

    def can_raise(self) -> bool:
        return bool(self.package_name)

    def launch_current_app(self):
        pkg = (self.package_name or "").strip()
        if not pkg:
            logging.debug("MPRIS Raise called but package_name is empty")
            return

        try:
            args = helpers.arguments()
            args.cache = {}
            args.work = config.defaults["work"]
            args.config = args.work + "/andromeda.cfg"
            args.log = args.work + "/andromeda.log"
            args.sudo_timer = True
            args.timeout = 1800
            args.PACKAGE = pkg
            app_manager.launch(args)
        except Exception as e:
            logging.error(f"Failed to raise app via MPRIS (PACKAGE={pkg}): {e}")

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        all_props = self.GetAll(interface_name)
        if property_name not in all_props:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs",
                f"Unknown property {interface_name}.{property_name}"
            )
        return all_props[property_name]

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        if interface_name == "org.mpris.MediaPlayer2":
            return dbus.Dictionary({
                "CanQuit": False,
                "CanRaise": self.can_raise(),
                "HasTrackList": False,
                "Identity": self.identity,
                "DesktopEntry": self.desktop_entry,
                "SupportedUriSchemes": dbus.Array([], signature="s"),
                "SupportedMimeTypes": dbus.Array([], signature="s"),
            }, signature="sv")

        if interface_name == "org.mpris.MediaPlayer2.Player":
            return dbus.Dictionary({
                "PlaybackStatus": self.playback_status(),
                "LoopStatus": "None",
                "Rate": dbus.Double(1.0),
                "Shuffle": False,
                "Metadata": self.metadata(),
                "Volume": dbus.Double(1.0),
                "Position": dbus.Int64(0),
                "MinimumRate": dbus.Double(1.0),
                "MaximumRate": dbus.Double(1.0),
                "CanGoNext": True,
                "CanGoPrevious": True,
                "CanPlay": True,
                "CanPause": True,
                "CanSeek": False,
                "CanControl": True,
            }, signature="sv")

        return dbus.Dictionary({}, signature="sv")

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ssv", out_signature="")
    def Set(self, interface_name, property_name, value):
        raise dbus.exceptions.DBusException(
            "org.freedesktop.DBus.Error.PropertyReadOnly",
            f"Property is read-only: {interface_name}.{property_name}"
        )

    @dbus.service.signal("org.freedesktop.DBus.Properties", signature="sa{sv}as")
    def PropertiesChanged(self, interface_name, changed_properties, invalidated_properties):
        pass

    @dbus.service.method("org.mpris.MediaPlayer2", in_signature="", out_signature="")
    def Raise(self):
        self.launch_current_app()

    @dbus.service.method("org.mpris.MediaPlayer2", in_signature="", out_signature="")
    def Quit(self):
        return

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Play(self):
        self.call_android_control("MprisPlay")
        self.set_playback_state("Playing")

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Pause(self):
        self.call_android_control("MprisPause")
        self.set_playback_state("Paused")

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def PlayPause(self):
        if self.playback_state == "Playing":
            self.call_android_control("MprisPause")
            self.set_playback_state("Paused")
        else:
            self.call_android_control("MprisPlay")
            self.set_playback_state("Playing")

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Next(self):
        self.call_android_control("MprisNext")

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="", out_signature="")
    def Previous(self):
        self.call_android_control("MprisPrevious")

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="x", out_signature="")
    def Seek(self, offset):
        return

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="ox", out_signature="")
    def SetPosition(self, track_id, position):
        return

    @dbus.service.method("org.mpris.MediaPlayer2.Player", in_signature="s", out_signature="")
    def OpenUri(self, uri):
        return

    def update_from_transport_notification(self, android_key: str, package_name: str,
                                           ticker: str, title: str, text: str):
        self.android_key = str(android_key)
        self.package_name = str(package_name)
        self.ticker = str(ticker)

        self.title = str(title)
        self.artist = str(text)

        self.art_url = self.art_url_for_package(self.package_name)

        # when we get an active transport notification, assume it represents a playable session.
        # whether it's currently paused/playing is unknown here, but "Playing" is the most likely
        self.playback_state = "Playing"

        self.emit_properties_changed("org.mpris.MediaPlayer2.Player", {
            "PlaybackStatus": self.playback_status(),
            "Metadata": self.metadata(),
        })

        # CanRaise may flip from False to True once we learn package_name
        self.emit_properties_changed("org.mpris.MediaPlayer2", {
            "CanRaise": self.can_raise(),
        })

    def clear(self):
        self.playback_state = "Stopped"
        self.android_key = ""
        self.package_name = ""
        self.ticker = ""
        self.title = ""
        self.artist = ""
        self.art_url = ""

        self.emit_properties_changed("org.mpris.MediaPlayer2.Player", {
            "PlaybackStatus": self.playback_status(),
            "Metadata": self.metadata(),
        })

        self.emit_properties_changed("org.mpris.MediaPlayer2", {
            "CanRaise": self.can_raise(),
        })

class NotificationService:
    ANDROID_BUS_NAME = "io.furios.Andromeda.Notification"
    ANDROID_OBJECT_PATH = "/io/furios/Andromeda/Notification"
    ANDROID_IFACE = "io.furios.Andromeda.Notification"

    def __init__(self, args):
        self.args = args

        # Android-key -> desktop notification id
        self.open_notifications = {}
        # desktop notification id -> Android-key
        self.desktop_to_android_key = {}

        # desktop notification id -> handler(action_key)
        self.action_handlers = {}

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        self.notifications_iface = None
        self.android_iface = None
        self.session_bus = None

        self.system_bus = dbus.SystemBus()

        self.mpris = None
        self.mpris_keys = set()

        self.setup_dbus_proxies()
        self.setup_dbus_signals()
        self.setup_name_owner_tracking()

    def setup_dbus_proxies(self):
        bus = dbus.SessionBus()
        self.session_bus = bus

        notification_service = bus.get_object('org.freedesktop.Notifications',
                                              '/org/freedesktop/Notifications')
        self.notifications_iface = dbus.Interface(notification_service,
                                                  dbus_interface='org.freedesktop.Notifications')

        self.notifications_iface.connect_to_signal("ActionInvoked", self.on_action_invoked)

        self.rebuild_android_iface()

    def name_has_owner(self) -> bool:
        try:
            dbus_obj = self.system_bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            dbus_iface = dbus.Interface(dbus_obj, "org.freedesktop.DBus")
            return bool(dbus_iface.NameHasOwner(self.ANDROID_BUS_NAME))
        except Exception as e:
            logging.error(f"NameHasOwner check failed: {e}")
            return False

    def rebuild_android_iface(self):
        if not self.name_has_owner():
            self.android_iface = None
            logging.debug("Android service not running.")
            if self.mpris is not None:
                self.mpris.android_iface = None
            return

        android_obj = self.system_bus.get_object(
            self.ANDROID_BUS_NAME,
            self.ANDROID_OBJECT_PATH,
            follow_name_owner_changes=True
        )
        self.android_iface = dbus.Interface(android_obj, dbus_interface=self.ANDROID_IFACE)
        logging.debug("Android DBus iface rebuilt")

        if self.mpris is not None:
            self.mpris.android_iface = self.android_iface

    def setup_name_owner_tracking(self):
        try:
            self.system_bus.add_signal_receiver(
                self.on_name_owner_changed,
                signal_name="NameOwnerChanged",
                dbus_interface="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                arg0=self.ANDROID_BUS_NAME
            )
        except Exception as e:
            logging.error(f"Failed to setup NameOwnerChanged tracking: {e}")

    def on_name_owner_changed(self, name, old_owner, new_owner):
        try:
            logging.debug(f"NameOwnerChanged for {name}: {old_owner} -> {new_owner}")

            if not new_owner:
                self.android_iface = None
                if self.mpris is not None:
                    self.mpris.android_iface = None
                logging.debug("Android service vanished. android_iface cleared.")
                return

            self.rebuild_android_iface()

        except Exception as e:
            logging.error("Failed handling NameOwnerChanged: {e}")

    def ensure_mpris(self):
        if self.mpris is None:
            self.mpris = AndromedaMpris(self.session_bus, self.android_iface)
            logging.debug("MPRIS player created")

    def destroy_mpris(self):
        if self.mpris is None:
            return
        try:
            self.mpris.remove_from_connection()
        except Exception:
            pass

        self.mpris.bus_name = None
        self.mpris = None
        logging.debug("MPRIS player destroyed")

    def shutdown(self):
        try:
            if self.mpris is not None:
                self.mpris.clear()
            self.mpris_keys.clear()
            self.destroy_mpris()
        except Exception as e:
            logging.error(f"Failed to shutdown MPRIS cleanly: {e}")

    def setup_dbus_signals(self):
        try:
            self.system_bus.add_signal_receiver(
                self.on_new_message,
                signal_name="NewMessage",
                dbus_interface=self.ANDROID_IFACE,
                bus_name=self.ANDROID_BUS_NAME,
                path=self.ANDROID_OBJECT_PATH
            )
            self.system_bus.add_signal_receiver(
                self.on_update_message,
                signal_name="UpdateMessage",
                dbus_interface=self.ANDROID_IFACE,
                bus_name=self.ANDROID_BUS_NAME,
                path=self.ANDROID_OBJECT_PATH
            )
            self.system_bus.add_signal_receiver(
                self.on_delete_message,
                signal_name="DeleteMessage",
                dbus_interface=self.ANDROID_IFACE,
                bus_name=self.ANDROID_BUS_NAME,
                path=self.ANDROID_OBJECT_PATH
            )

            self.system_bus.add_signal_receiver(
                self.on_new_mpris_message,
                signal_name="NewMprisMessage",
                dbus_interface=self.ANDROID_IFACE,
                bus_name=self.ANDROID_BUS_NAME,
                path=self.ANDROID_OBJECT_PATH
            )
            self.system_bus.add_signal_receiver(
                self.on_update_mpris_message,
                signal_name="UpdateMprisMessage",
                dbus_interface=self.ANDROID_IFACE,
                bus_name=self.ANDROID_BUS_NAME,
                path=self.ANDROID_OBJECT_PATH
            )
            self.system_bus.add_signal_receiver(
                self.on_remove_mpris_message,
                signal_name="RemoveMprisMessage",
                dbus_interface=self.ANDROID_IFACE,
                bus_name=self.ANDROID_BUS_NAME,
                path=self.ANDROID_OBJECT_PATH
            )
        except Exception as e:
            logging.error(f"Failed to setup DBus signals: {e}")

    def get_app_name(self, package_name):
        args = helpers.arguments()
        args.cache = {}
        args.work = config.defaults["work"]
        args.config = args.work + "/andromeda.cfg"
        args.log = args.work + "/andromeda.log"
        args.sudo_timer = True
        args.timeout = 1800

        ipc.DBusSessionService()
        cm = ipc.DBusContainerService()
        session = cm.GetSession()
        if session["state"] == "FROZEN":
            cm.Unfreeze()

        platform_service = IPlatform.get_service(args)
        if platform_service:
            apps_list = platform_service.getAppsInfo()
            app_name_dict = {app['packageName']: app['name'] for app in apps_list}
            app_name = app_name_dict.get(package_name)
            return True, app_name

        logging.error("Failed to access IPlatform service")

        if session["state"] == "FROZEN":
            cm.Freeze()

        return False, None

    def on_action_invoked(self, notification_id, action_key):
        try:
            nid = int(notification_id)
        except Exception:
            return

        if nid in self.action_handlers:
            handler = self.action_handlers[nid]
            try:
                handler(action_key)
            finally:
                del self.action_handlers[nid]

    def create_action_handler(self, pkg_name):
        def handler(action_key):
            if action_key == 'open':
                args = helpers.arguments()
                args.cache = {}
                args.work = config.defaults["work"]
                args.config = args.work + "/andromeda.cfg"
                args.log = args.work + "/andromeda.log"
                args.sudo_timer = True
                args.timeout = 1800
                args.PACKAGE = pkg_name
                app_manager.launch(args)
        return handler

    def notify_send(self, app_name, package_name, ticker, title, text,
                    is_foreground_service, show_light, updates_id):
        # When the title and text fields are not present, we choose an empty title
        # and the ticker as text.
        if title == '' or text == '':
            title = ''
            text = ticker

        nid = self.notifications_iface.Notify(
            app_name,
            int(updates_id),
            config.session_defaults["andromeda_data"] + "/icons/" + package_name + ".png",
            title,
            text,
            ['default', 'Open', 'open', 'Open'],
            {'urgency': 1 if show_light else 0},
            5000
        )

        try:
            return int(nid)
        except Exception:
            return 0

    def close_notification_send(self, notification_id):
        try:
            self.notifications_iface.CloseNotification(int(notification_id))
        except Exception as e:
            logging.error(f"CloseNotification error: {e}")

        try:
            if int(notification_id) in self.action_handlers:
                del self.action_handlers[int(notification_id)]
        except Exception:
            pass

    def on_new_message(self, msg_hash, msg_id, package_name, ticker, title, text,
                       is_foreground_service, is_group_summary, show_light, when):
        android_key = str(msg_hash)

        logging.debug(
            f"Received new message notification: "
            f"{android_key}, {msg_id}, {package_name}, {ticker}, {title}, {text}, "
            f"{is_foreground_service}, {is_group_summary}, {show_light}, {when}"
        )

        try:
            ok, app_name = self.get_app_name(package_name)
            if ok and not is_group_summary:
                nid = self.notify_send(app_name, package_name, ticker, title, text,
                                       is_foreground_service, show_light, 0)
                if nid != 0:
                    self.open_notifications[android_key] = nid
                    self.desktop_to_android_key[nid] = android_key
                    self.action_handlers[nid] = self.create_action_handler(package_name)
        except dbus.DBusException:
            logging.error("Andromeda session is stopped")
        except Exception as e:
            logging.error(f"on_new_message error: {e}")

    def on_update_message(self, msg_hash, replaces_hash, msg_id, package_name, ticker, title, text,
                          is_foreground_service, is_group_summary, show_light, when):
        android_key = str(msg_hash)
        replaces_key = str(replaces_hash)

        logging.debug(
            f"Received update message notification: "
            f"{android_key}, {replaces_key}, {msg_id}, {package_name}, {ticker}, "
            f"{title}, {text}, {is_foreground_service}, {is_group_summary}, "
            f"{show_light}, {when}"
        )

        try:
            ok, app_name = self.get_app_name(package_name)
            if not ok:
                return

            # If we already showed this notification, update it in-place.
            if replaces_key in self.open_notifications:
                existing_nid = self.open_notifications[replaces_key]
                nid = self.notify_send(app_name, package_name, ticker, title, text,
                                       is_foreground_service, show_light,
                                       existing_nid)

                if nid != 0 and nid != existing_nid:
                    try:
                        if existing_nid in self.desktop_to_android_key:
                            del self.desktop_to_android_key[existing_nid]
                    except Exception:
                        pass
                    self.desktop_to_android_key[nid] = android_key

                    try:
                        if existing_nid in self.action_handlers:
                            del self.action_handlers[existing_nid]
                    except Exception:
                        pass
                    self.action_handlers[nid] = self.create_action_handler(package_name)

                self.open_notifications[android_key] = (nid if nid != 0 else existing_nid)

                if android_key != replaces_key:
                    try:
                        del self.open_notifications[replaces_key]
                    except Exception:
                        pass
            else:
                if not is_group_summary:
                    nid = self.notify_send(app_name, package_name, ticker, title, text,
                                           is_foreground_service, show_light, 0)
                    if nid != 0:
                        self.open_notifications[android_key] = nid
                        self.desktop_to_android_key[nid] = android_key
                        self.action_handlers[nid] = self.create_action_handler(package_name)

        except dbus.DBusException:
            logging.error("Andromeda session is stopped")
        except Exception as e:
            logging.error(f"on_update_message error: {e}")

    def on_delete_message(self, msg_hash):
        android_key = str(msg_hash)
        logging.debug(f"Received delete message notification: {android_key}")

        try:
            if android_key in self.open_notifications:
                nid = self.open_notifications[android_key]
                self.close_notification_send(nid)

                try:
                    del self.open_notifications[android_key]
                except Exception:
                    pass

                try:
                    if nid in self.desktop_to_android_key:
                        del self.desktop_to_android_key[nid]
                except Exception:
                    pass
        except dbus.DBusException:
            logging.error("Andromeda session is stopped")
        except Exception as e:
            logging.error(f"on_delete_message error: {e}")

    def on_new_mpris_message(self, msg_hash, msg_id, package_name, ticker, title, text,
                             is_foreground_service, is_group_summary, show_light, when):
        android_key = str(msg_hash)

        logging.debug(
            f"Received new mpris message notification: "
            f"{android_key}, {msg_id}, {package_name}, {ticker}, {title}, {text}, "
            f"{is_foreground_service}, {is_group_summary}, {show_light}, {when}"
        )

        self.mpris_keys.add(android_key)
        self.ensure_mpris()

        try:
            if self.mpris is not None:
                self.mpris.update_from_transport_notification(android_key, package_name, ticker, title, text)
        except Exception as e:
            logging.error(f"on_new_mpris_message error: {e}")

    def on_update_mpris_message(self, msg_hash, replaces_hash, msg_id, package_name, ticker, title, text,
                                is_foreground_service, is_group_summary, show_light, when):
        android_key = str(msg_hash)
        replaces_key = str(replaces_hash)

        logging.debug(
            f"Received update mpris message notification: "
            f"{android_key}, {replaces_key}, {msg_id}, {package_name}, {ticker}, "
            f"{title}, {text}, {is_foreground_service}, {is_group_summary}, "
            f"{show_light}, {when}"
        )

        self.mpris_keys.add(android_key)
        self.ensure_mpris()

        try:
            if self.mpris is not None:
                self.mpris.update_from_transport_notification(android_key, package_name, ticker, title, text)
        except Exception as e:
            logging.error(f"on_update_mpris_message error: {e}")

    def on_remove_mpris_message(self, msg_hash):
        android_key = str(msg_hash)

        logging.debug(f"Received delete mpris message notification: {android_key}")

        try:
            self.mpris_keys.discard(android_key)

            if len(self.mpris_keys) == 0:
                if self.mpris is not None:
                    self.mpris.clear()
                self.destroy_mpris()
        except Exception as e:
            logging.error(f"on_remove_mpris_message error: {e}")

    def run(self):
        self.args.notificationLoop = GLib.MainLoop()
        logging.debug("Notification client service running")
        self.args.notificationLoop.run()

def service_thread(args):
    global stopping

    try:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        notification_service = NotificationService(args)
        args.notification_service = notification_service

        while not stopping:
            try:
                notification_service.run()
            except Exception as e:
                logging.error(f"Error in notification service loop: {e}")
                if not stopping:
                    continue
                break
    except Exception as e:
        logging.error(f"Notification service error: {str(e)}")

def start(args):
    global stopping
    logging.debug("Starting notification client service")

    stopping = False
    args.notification_manager = threading.Thread(target=service_thread, args=(args,))
    args.notification_manager.daemon = True
    args.notification_manager.start()

def stop(args):
    global stopping

    logging.debug("Stopping notification client service")
    stopping = True

    try:
        if hasattr(args, "notification_service") and args.notification_service:
            try:
                args.notification_service.shutdown()
            except Exception:
                pass
    except Exception:
        pass

    try:
        if hasattr(args, "notificationLoop") and args.notificationLoop:
            args.notificationLoop.quit()
    except Exception as e:
        logging.error(f"Error stopping notification service: {e}")
