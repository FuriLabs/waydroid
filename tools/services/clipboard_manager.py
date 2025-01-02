# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import dbus
import logging
import threading
from tools.interfaces import IClipboard
from tools.helpers import WaylandClipboardHandler

stopping = False
clipboard_handler = None

def start(args):
    def setup_dbus_signals():
        global clipboard_handler
        bus = dbus.SystemBus()

        bus.add_signal_receiver(
            clipboard_handler.copy,
            signal_name='sendClipboardData',
            dbus_interface='id.waydro.StateChange',
            bus_name='id.waydro.StateChange'
        )

    def service_thread():
        global clipboard_handler

        import dbus.mainloop.glib
        from gi.repository import GLib

        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

            clipboard_handler = WaylandClipboardHandler()
            setup_dbus_signals()

            args.clipboardLoop = GLib.MainLoop()
            while not stopping:
                try:
                    args.clipboardLoop.run()
                except Exception as e:
                    logging.error(f"Error in clipboard manager loop: {e}")
                    if not stopping:
                        continue
                    break
        except Exception as e:
            logging.debug(f"Clipboard service error: {str(e)}")

    global stopping
    stopping = False
    args.clipboard_manager = threading.Thread(target=service_thread)
    args.clipboard_manager.start()

def stop(args):
    global stopping
    stopping = True
    try:
        if args.clipboardLoop:
            args.clipboardLoop.quit()
    except AttributeError:
        logging.debug("Clipboard service is not even started")
