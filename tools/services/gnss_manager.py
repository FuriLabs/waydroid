# Copyright 2025 Bardia Moshiri
# SPDX-License-Identifier: GPL-3.0-or-later
import gi
import sys
import signal
import os
import subprocess
import logging
import dbus
import dbus.mainloop.glib
import threading
from gi.repository import GLib
gi.require_version('Geoclue', '2.0')
from gi.repository import Geoclue

# This is a hack. when a new process is created the child is no longer in the same subdir as parent
# This hack comes from obex-capabilities which has a similar hack to pass some tests in CI
sys.path.insert(0, '/usr/lib/waydroid')
from tools import helpers
from tools import config

stopping = False
location_service = None

class LocationTracker:
    def __init__(self, args):
        self.mainloop = GLib.MainLoop()
        self.client = None
        self.location = None
        self.process = None
        self.stopping = False
        self.args = args

    def on_location_updated(self, client, location, old=None):
        if hasattr(location, 'name') and location.name == 'location':
            location = client.get_property('location')

        latitude = location.get_property('latitude')
        longitude = location.get_property('longitude')
        accuracy = location.get_property('accuracy')
        altitude = location.get_property('altitude')
        speed = location.get_property('speed')
        heading = location.get_property('heading')
        timestamp = location.get_property('timestamp')

        helpers.props.set(self.args, "furios.gnss.latitude", str(latitude))
        helpers.props.set(self.args, "furios.gnss.longitude", str(longitude))
        helpers.props.set(self.args, "furios.gnss.altitude", str(altitude))

        if speed != -1:
            helpers.props.set(self.args, "furios.gnss.speed", str(speed))

    def run(self):
        try:
            self.client = Geoclue.Simple.new_sync(
                'android-location',
                Geoclue.AccuracyLevel.EXACT,
                None
            )

            location = self.client.get_location()
            if location:
                self.on_location_updated(self.client, location, None)

            self.client.connect('notify::location', self.on_location_updated)
            self.mainloop.run()
        except GLib.Error as e:
            logging.error(f"Error starting GeoClue: {e.message}")
            sys.exit(1)

def tracker_process():
    # Since we are getting another instance of args and initializing it, we NEED argv[1] to be empty
    # this is a requirement since helper does a sanity check and obviously, run_tracker is not in argparser
    # Pop it here before initializing args. we don't need it anymore anyways
    # the process is already created, we are already inside.
    if len(sys.argv) > 1 and sys.argv[1] == "run_tracker":
        sys.argv.pop(1)

    args = helpers.arguments()
    args.cache = {}
    args.work = config.defaults["work"]
    args.config = args.work + "/waydroid.cfg"
    args.log = args.work + "/waydroid.log"
    args.sudo_timer = True
    args.timeout = 1800
    tracker = LocationTracker(args)
    tracker.run()

class LocationService:
    def __init__(self, args):
        self.tracker_process = None
        self.stopping = False
        self.args = args
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.setup_dbus_signals()

    def setup_dbus_signals(self):
        try:
            bus = dbus.SystemBus()
            bus.add_signal_receiver(
                self.gnssStateChanged,
                signal_name='gnssStateChanged',
                dbus_interface='id.waydro.StateChange',
                bus_name='id.waydro.StateChange'
            )
        except Exception as e:
            logging.error(f"Failed to setup DBus signals: {e}")

    def gnssStateChanged(self, state):
        if state:
            self.start_tracking()
        else:
            self.stop_tracking()

    def write_pid(self, pid):
        with open("/tmp/location_tracker.pid", "w") as f:
            f.write(str(pid))

    def read_pid(self):
        try:
            with open("/tmp/location_tracker.pid", "r") as f:
                return int(f.read().strip())
        except FileNotFoundError:
            return None

    def start_tracking(self):
        if self.read_pid():
            logging.info("Location tracking is already running.")
            return

        # FakeShell: This is a bit awkward. tl;dr, I could not find a way to make geoclue remove the object properly after we are done when using gi
        # inb4 when developing for mm_modem_location in oFono2MM, since we needed to seteuid to 32011 (to get a location from geoclue we need an agent and there is no agent for uid 0)
        # to not make the entire thread of that process become 32011 (as we need euid 0 for some NM operations), the solution I came up was to make geoclue run in a separate process
        # and kill it once we are done with GetLocation() and user gets a result. The code here is very similar, with the main difference being subprocess vs multiprocessing.
        # This is not correct at all, making a new process for a dbus request is plain stupid but for the life of me I cannot figure out how to remove the object properly
        # to not keep the GNSS chip active, so this will do for now. I will regret this later when trying to clean it up.
        try:
            process = subprocess.Popen(
                [sys.executable, __file__, "run_tracker"],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )

            self.write_pid(process.pid)
            logging.info(f"Location tracking started (PID: {process.pid})")
        except Exception as e:
            logging.error(f"Failed to start tracking: {e}")

    def stop_tracking(self):
        pid = self.read_pid()
        if not pid:
            logging.info("Location tracking is not running.")
            return

        try:
            os.kill(pid, signal.SIGTERM)
            os.remove("/tmp/location_tracker.pid")
            logging.info("Location tracking stopped.")
        except ProcessLookupError:
            logging.warning("Location tracking process not found.")
            os.remove("/tmp/location_tracker.pid")
        except Exception as e:
            logging.error(f"Error stopping tracker: {e}")

def start(args):
    def service_thread():
        global location_service
        import dbus.mainloop.glib
        from gi.repository import GLib

        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            location_service = LocationService(args)
            args.gnssLoop = GLib.MainLoop()

            while not stopping:
                try:
                    args.gnssLoop.run()
                except Exception as e:
                    logging.error(f"Error in location service loop: {e}")
                    if not stopping:
                        continue
                    break
        except Exception as e:
            logging.error(f"Location service error: {str(e)}")

    global stopping
    stopping = False
    args.gnss_manager = threading.Thread(target=service_thread)
    args.gnss_manager.start()

def stop(args):
    global stopping
    stopping = True
    try:
        if args.gnssLoop:
            args.gnssLoop.quit()
        if location_service:
            location_service.stop_tracking()
    except AttributeError:
        logging.debug("GNSS service is not even started")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run_tracker":
        tracker_process()
