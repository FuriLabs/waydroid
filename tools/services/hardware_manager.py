# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import threading
import tools.actions.container_manager
import tools.actions.session_manager
import tools.config
from tools import helpers
from tools.interfaces import IHardware

stopping = False

def start(args):
    def enableNFC(enable):
        logging.debug("Function enableNFC not implemented")

    def enableBluetooth(enable):
        logging.debug("Function enableBluetooth not implemented")

    def suspend():
        cfg = tools.config.load(args)
        if cfg["waydroid"]["suspend_action"] == "stop":
            tools.actions.session_manager.stop(args)
        else:
            tools.actions.container_manager.freeze(args)

    def reboot():
        helpers.lxc.stop(args)
        helpers.lxc.start(args)

    def upgrade(system_zip, system_time, vendor_zip, vendor_time):
        pass

    def service_thread():
        while not stopping:
            IHardware.add_service(
                args, enableNFC, enableBluetooth, suspend, reboot, upgrade)

    global stopping
    stopping = False
    args.hardware_manager = threading.Thread(target=service_thread)
    args.hardware_manager.start()

def stop(args):
    global stopping
    stopping = True
    try:
        if args.hardwareLoop:
            args.hardwareLoop.quit()
    except AttributeError:
        logging.debug("Hardware service is not even started")
