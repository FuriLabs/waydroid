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

class NotificationService:
    def __init__(self, args):
        self.args = args

        # Android-key -> desktop notification id
        self.open_notifications = {}
        # desktop notification id -> Android-key
        self.desktop_to_android_key = {}

        # desktop notification id -> handler(action_key)
        self.action_handlers = {}

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        self._notifications_iface = None
        self._android_iface = None

        self.setup_dbus_proxies()
        self.setup_dbus_signals()

    def setup_dbus_proxies(self):
        bus = dbus.SessionBus()
        notification_service = bus.get_object('org.freedesktop.Notifications',
                                              '/org/freedesktop/Notifications')
        self._notifications_iface = dbus.Interface(notification_service,
                                                   dbus_interface='org.freedesktop.Notifications')

        self._notifications_iface.connect_to_signal("ActionInvoked", self.on_action_invoked)

        system_bus = dbus.SystemBus()
        android_obj = system_bus.get_object('io.furios.Andromeda.Notification',
                                            '/io/furios/Andromeda/Notification')
        self._android_iface = dbus.Interface(android_obj,
                                             dbus_interface='io.furios.Andromeda.Notification')

    def setup_dbus_signals(self):
        try:
            system_bus = dbus.SystemBus()
            system_bus.add_signal_receiver(
                self.on_new_message,
                signal_name="NewMessage",
                dbus_interface='io.furios.Andromeda.Notification',
                bus_name='io.furios.Andromeda.Notification',
                path='/io/furios/Andromeda/Notification'
            )
            system_bus.add_signal_receiver(
                self.on_update_message,
                signal_name="UpdateMessage",
                dbus_interface='io.furios.Andromeda.Notification',
                bus_name='io.furios.Andromeda.Notification',
                path='/io/furios/Andromeda/Notification'
            )
            system_bus.add_signal_receiver(
                self.on_delete_message,
                signal_name="DeleteMessage",
                dbus_interface='io.furios.Andromeda.Notification',
                bus_name='io.furios.Andromeda.Notification',
                path='/io/furios/Andromeda/Notification'
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

        nid = self._notifications_iface.Notify(
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
            self._notifications_iface.CloseNotification(int(notification_id))
        except Exception as e:
            logging.error(f"CloseNotification error: {e}")

        try:
            if int(notification_id) in self.action_handlers:
                del self.action_handlers[int(notification_id)]
        except Exception:
            pass

    def on_new_message(self, msg_hash, _msg_id, package_name, ticker, title, text,
                       is_foreground_service, is_group_summary, show_light, _when):
        android_key = str(msg_hash)

        logging.debug(
            f"Received new message notification: "
            f"{android_key}, {_msg_id}, {package_name}, {ticker}, {title}, {text}, "
            f"{is_foreground_service}, {is_group_summary}, {show_light}, {_when}"
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

    def on_update_message(self, msg_hash, replaces_hash, _msg_id, package_name, ticker, title, text,
                          is_foreground_service, _is_group_summary, show_light, _when):
        android_key = str(msg_hash)
        replaces_key = str(replaces_hash)

        logging.debug(
            f"Received update message notification: "
            f"{android_key}, {replaces_key}, {_msg_id}, {package_name}, {ticker}, "
            f"{title}, {text}, {is_foreground_service}, {_is_group_summary}, "
            f"{show_light}, {_when}"
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
                if not _is_group_summary:
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

    def run(self):
        self.args.notificationLoop = GLib.MainLoop()
        logging.debug("Notification client service running")
        self.args.notificationLoop.run()

def service_thread(args):
    global stopping

    try:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        notification_service = NotificationService(args)

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
        if hasattr(args, 'notificationLoop') and args.notificationLoop:
            args.notificationLoop.quit()
    except Exception as e:
        logging.error(f"Error stopping notification service: {e}")
