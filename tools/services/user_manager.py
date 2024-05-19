# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
from time import sleep
import logging
import os
import threading
import tools.config
import tools.helpers.net
from tools.interfaces import IUserMonitor
from tools.interfaces import IPlatform

stopping = False

def start(args, session, unlocked_cb=None):
    waydroid_data = session["waydroid_data"]
    apps_dir = session["xdg_data_home"] + "/applications/"

    def makeDesktopFile(appInfo):
        if appInfo is None:
            return -1

        showApp = False
        for cat in appInfo["categories"]:
            if cat.strip() == "android.intent.category.LAUNCHER":
                showApp = True
        if not showApp:
            return -1

        packageName = appInfo["packageName"]

        hide = False
        # FuriOS: don't add an icon for default apps such as documents, settings or microG
        if packageName == "com.android.documentsui" or packageName == "com.android.inputmethod.latin" \
            or packageName == "com.android.settings" or packageName == "com.google.android.gms" \
            or packageName == "org.lineageos.jelly" or packageName == "org.lineageos.aperture":
            hide = True

        desktop_file_path = apps_dir + "/waydroid." + packageName + ".desktop"
        if not os.path.exists(desktop_file_path):
            with open(desktop_file_path, "w") as desktop_file:
                desktop_file.write(f"""\
[Desktop Entry]
Type=Application
Name={appInfo["name"]}
Exec=waydroid app launch {packageName}
Icon={waydroid_data}/icons/{packageName}.png
Categories=X-WayDroid-App;
X-Purism-FormFactor=Workstation;Mobile;
Actions=app_settings;
NoDisplay={str(hide).lower()}

[Desktop Action app_settings]
Name=App Settings
Exec=waydroid app intent android.settings.APPLICATION_DETAILS_SETTINGS package:{packageName}
Icon={waydroid_data}/icons/com.android.settings.png
""")
            return 0

    def makeWaydroidDesktopFile(hide):
        desktop_file_path = apps_dir + "/Waydroid.desktop"
        if os.path.isfile(desktop_file_path):
            os.remove(desktop_file_path)

        # FuriOS: we are not using this
        return -1

        with open(desktop_file_path, "w") as desktop_file:
            desktop_file.write(f"""\
[Desktop Entry]
Type=Application
Name=Waydroid
Exec=waydroid show-full-ui
Categories=X-WayDroid-App;
X-Purism-FormFactor=Workstation;Mobile;
Icon=waydroid
NoDisplay={str(hide).lower()}
""")

    def userUnlocked(uid):
        logging.info("Android with user {} is ready".format(uid))

        tools.helpers.net.adb_connect(args)

        platformService = IPlatform.get_service(args)
        if platformService:
            if not os.path.exists(apps_dir):
                os.mkdir(apps_dir, 0o700)
            appsList = platformService.getAppsInfo()
            while len(platformService.getAppsInfo()) < 3:
                sleep(1)

            for app in appsList:
                makeDesktopFile(app)
            multiwin = platformService.getprop("persist.waydroid.multi_windows", "false")
            makeWaydroidDesktopFile(multiwin == "true")
        if unlocked_cb:
            unlocked_cb()

        monitor_apps(platformService)

    def monitor_apps(platformService):
        old_appsList = platformService.getAppsInfo()
        old_length = len(old_appsList)
        while not stopping:
            appsList = platformService.getAppsInfo()
            new_length = len(appsList)

            if new_length > old_length:
                for app in appsList:
                    makeDesktopFile(app)
                old_length = new_length
                old_appsList = appsList
            elif new_length < old_length:
                old_pkgname = []
                new_pkgname = []
                for item in old_appsList:
                    old_pkgname.append(item['packageName'])
                for item in appsList:
                    new_pkgname.append(item['packageName'])

                missing_pkgname = list(set(old_pkgname) - set(new_pkgname))
                desktop_file_path = apps_dir + "/waydroid." + missing_pkgname[0] + ".desktop"
                if os.path.exists(desktop_file_path):
                    os.remove(desktop_file_path)

                old_length = new_length
                old_appsList = appsList

            sleep(2)

    def packageStateChanged(mode, packageName, uid):
        platformService = IPlatform.get_service(args)
        if platformService:
            appInfo = platformService.getAppInfo(packageName)
            desktop_file_path = apps_dir + "/waydroid." + packageName + ".desktop"
            if mode == 0:
                # Package added
                makeDesktopFile(appInfo)
            elif mode == 1:
                if os.path.isfile(desktop_file_path):
                    os.remove(desktop_file_path)
            else:
                if os.path.isfile(desktop_file_path):
                    if makeDesktopFile(appInfo) == -1:
                        os.remove(desktop_file_path)

    def usermonitor_timeout():
        timer = threading.Timer(20.0, lambda: userUnlocked(0))
        timer.start()

        try:
            IUserMonitor.add_service(args, userUnlocked, packageStateChanged)
        except Exception as e:
            logging.error(f"Failed to add service: {e}")
        finally:
            timer.cancel()

    def service_thread():
        while not stopping:
            usermonitor_timeout()
            sleep(1)

    global stopping
    stopping = False
    args.user_manager = threading.Thread(target=service_thread)
    args.user_manager.start()

def stop(args):
    global stopping
    stopping = True
    try:
        if args.userMonitorLoop:
            args.userMonitorLoop.quit()
    except AttributeError:
        logging.debug("UserMonitor service is not even started")
