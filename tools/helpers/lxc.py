# Copyright 2021 Erfan Abdi
# Copyright 2025 Bardia Moshiri
# SPDX-License-Identifier: GPL-3.0-or-later

import subprocess
import os
import re
import logging
import glob
import shutil
import time
import platform
import gbinder
import tools.config
import tools.helpers.run

def get_lxc_version(args):
    if shutil.which("lxc-info") is not None:
        command = ["lxc-info", "--version"]
        version_str = tools.helpers.run.user(args, command, output_return=True)
        return int(version_str[0])
    else:
        return 0

def add_node_entry(nodes, src, dist, mnt_type, options, check):
    if check and not os.path.exists(src):
        return False
    entry = "lxc.mount.entry = "
    entry += src + " "
    if dist is None:
        dist = src[1:]
    entry += dist + " "
    entry += mnt_type + " "
    entry += options
    nodes.append(entry)
    return True

def generate_nodes_lxc_config(args):
    nodes = []
    def make_entry(src, dist=None, mnt_type="none", options="bind,create=file,optional 0 0", check=True):
        return add_node_entry(nodes, src, dist, mnt_type, options, check)

    # Necessary dev nodes
    make_entry("tmpfs", "dev", "tmpfs", "nosuid 0 0", False)
    make_entry("/dev/zero")
    make_entry("/dev/null")
    make_entry("/dev/full")
    make_entry("/dev/ashmem")
    make_entry("/dev/fuse")
    make_entry("/dev/ion")
    make_entry("/dev/tty")
    make_entry("/dev/char", options="bind,create=dir,optional 0 0")

    # Graphic dev nodes
    make_entry("/dev/kgsl-3d0")
    make_entry("/dev/mali0")
    make_entry("/dev/pvr_sync")
    make_entry("/dev/pmsg0")
    make_entry("/dev/dxg")
    render, card = tools.helpers.gpu.getDriNode(args)
    make_entry(render, "dev/dri/renderD128")
    make_entry(card, "dev/dri/card0")

    for n in glob.glob("/dev/fb*"):
        make_entry(n)
    for n in glob.glob("/dev/graphics/fb*"):
        make_entry(n)
    for n in glob.glob("/dev/video*"):
        make_entry(n)

    # Binder dev nodes
    make_entry("/dev/" + args.BINDER_DRIVER, "dev/binder", check=False)
    make_entry("/dev/" + args.VNDBINDER_DRIVER, "dev/vndbinder", check=False)
    make_entry("/dev/" + args.HWBINDER_DRIVER, "dev/hwbinder", check=False)

    if args.vendor_type != "MAINLINE":
        if not make_entry("/dev/hwbinder", "dev/host_hwbinder"):
            raise OSError('Binder node "hwbinder" of host not found')
        make_entry("/vendor", "vendor_extra", options="rbind,optional 0 0")

    # Necessary device nodes for adb
    make_entry("none", "dev/pts", "devpts", "defaults,mode=644,ptmxmode=666,create=dir 0 0", False)
    make_entry("/dev/uhid")

    # TUN/TAP device node for VPN
    make_entry("/dev/net/tun", "dev/tun")

    # Low memory killer sys node
    make_entry("/sys/module/lowmemorykiller", options="bind,create=dir,optional 0 0")

    # Mount host permissions
    make_entry(tools.config.defaults["host_perms"],
               "vendor/etc/host-permissions", options="bind,optional 0 0")

    # Necessary sw_sync node for HWC
    make_entry("/dev/sw_sync")
    make_entry("/sys/kernel/debug", options="rbind,create=dir,optional 0 0")

    # Vibrator
    make_entry("/sys/class/leds/vibrator",
               options="bind,create=dir,optional 0 0")
    make_entry("/sys/devices/virtual/timed_output/vibrator",
               options="bind,create=dir,optional 0 0")

    # Media dev nodes (for Mediatek)
    make_entry("/dev/Vcodec")
    make_entry("/dev/MTK_SMI")
    make_entry("/dev/mdp_sync")
    make_entry("/dev/mtk_cmdq")
    make_entry("/dev/mtk_mdp")

    # WSLg
    make_entry("tmpfs", "mnt_extra", "tmpfs", "nodev 0 0", False)
    make_entry("/mnt/wslg", "mnt_extra/wslg",
               options="rbind,create=dir,optional 0 0")

    # Make a tmpfs at every possible rootfs mountpoint
    make_entry("tmpfs", "tmp", "tmpfs", "nodev 0 0", False)
    make_entry("tmpfs", "var", "tmpfs", "nodev 0 0", False)
    make_entry("tmpfs", "run", "tmpfs", "nodev 0 0", False)

    # NFC config
    make_entry("/system/etc/libnfc-nci.conf", options="bind,optional 0 0")

    # DBus system bus for AIDL radio
    make_entry("/var/run/dbus", "var", options="rbind,optional 0 0")

    return nodes

LXC_APPARMOR_PROFILE = "lxc-waydroid"
def get_apparmor_status(args):
    enabled = False
    if shutil.which("aa-enabled"):
        enabled = (tools.helpers.run.user(args, ["aa-enabled", "--quiet"], check=False) == 0)
    if not enabled and shutil.which("systemctl"):
        enabled = (tools.helpers.run.user(args, ["systemctl", "is-active", "-q", "apparmor"], check=False) == 0)
    try:
        with open("/sys/kernel/security/apparmor/profiles", "r") as f:
            enabled &= (LXC_APPARMOR_PROFILE in f.read())
    except:
        enabled = False
    return enabled

def set_lxc_config(args):
    lxc_path = tools.config.defaults["lxc"] + "/waydroid"
    lxc_ver = get_lxc_version(args)
    if lxc_ver == 0:
        raise OSError("LXC is not installed")
    config_paths = tools.config.tools_src + "/data/configs/config_"
    seccomp_profile = tools.config.tools_src + "/data/configs/waydroid.seccomp"

    config_snippets = [ config_paths + "base" ]
    # lxc v1 and v2 are bit special because some options got renamed later
    if lxc_ver <= 2:
        config_snippets.append(config_paths + "1")
    else:
        for ver in range(3, 5):
            snippet = config_paths + str(ver)
            if lxc_ver >= ver and os.path.exists(snippet):
                config_snippets.append(snippet)

    command = ["mkdir", "-p", lxc_path]
    tools.helpers.run.user(args, command)
    command = ["sh", "-c", "cat {} > \"{}\"".format(' '.join('"{0}"'.format(w) for w in config_snippets), lxc_path + "/config")]
    tools.helpers.run.user(args, command)
    command = ["sed", "-i", "s/LXCARCH/{}/".format(platform.machine()), lxc_path + "/config"]
    tools.helpers.run.user(args, command)
    command = ["cp", "-fpr", seccomp_profile, lxc_path + "/waydroid.seccomp"]
    tools.helpers.run.user(args, command)
    if get_apparmor_status(args):
        command = ["sed", "-i", "-E", "/lxc.aa_profile|lxc.apparmor.profile/ s/unconfined/{}/g".format(LXC_APPARMOR_PROFILE), lxc_path + "/config"]
        tools.helpers.run.user(args, command)

    nodes = generate_nodes_lxc_config(args)
    config_nodes_tmp_path = args.work + "/config_nodes"
    config_nodes = open(config_nodes_tmp_path, "w")
    for node in nodes:
        config_nodes.write(node + "\n")
    config_nodes.close()
    command = ["mv", config_nodes_tmp_path, lxc_path]
    tools.helpers.run.user(args, command)

    # Create empty file
    open(os.path.join(lxc_path, "config_session"), mode="w").close()

def generate_session_lxc_config(args, session):
    nodes = []
    def make_entry(src, dist=None, mnt_type="none", options="rbind,create=file 0 0"):
        if any(x in src for x in ["\n", "\r"]):
            logging.warning("User-provided mount path contains illegal character: " + src)
            return False
        if dist is None and (not os.path.exists(src) or
                             str(os.stat(src).st_uid) != session["user_id"]):
            logging.warning("User-provided mount path is not owned by user: " + src)
            return False
        return add_node_entry(nodes, src, dist, mnt_type, options, check=False)

    # Make sure XDG_RUNTIME_DIR exists
    if not make_entry("tmpfs", tools.config.defaults["container_xdg_runtime_dir"], options="create=dir 0 0"):
        raise OSError("Failed to create XDG_RUNTIME_DIR mount point")

    wayland_host_socket = os.path.realpath(os.path.join(session["xdg_runtime_dir"], session["wayland_display"]))
    wayland_container_socket = os.path.realpath(os.path.join(tools.config.defaults["container_xdg_runtime_dir"], tools.config.defaults["container_wayland_display"]))
    if not make_entry(wayland_host_socket, wayland_container_socket[1:]):
        raise OSError("Failed to bind Wayland socket")

    # Make sure PULSE_RUNTIME_DIR exists
    pulse_host_socket = os.path.join(session["pulse_runtime_path"], "native")
    pulse_container_socket = os.path.join(tools.config.defaults["container_pulse_runtime_path"], "native")
    make_entry(pulse_host_socket, pulse_container_socket[1:])

    if not make_entry(session["waydroid_data"], "data", options="rbind 0 0"):
        raise OSError("Failed to bind userdata")

    lxc_path = tools.config.defaults["lxc"] + "/waydroid"
    config_nodes_tmp_path = args.work + "/config_session"
    config_nodes = open(config_nodes_tmp_path, "w")
    for node in nodes:
        config_nodes.write(node + "\n")
    config_nodes.close()
    command = ["mv", config_nodes_tmp_path, lxc_path]
    tools.helpers.run.user(args, command)

def make_base_props(args):
    def find_hal(hardware):
        hardware_props = [
            "ro.hardware." + hardware,
            "ro.hardware",
            "ro.product.board",
            "ro.arch",
            "ro.board.platform"]
        for p in hardware_props:
            prop = tools.helpers.props.host_get(args, p)
            if prop != "":
                for lib in ["/odm/lib", "/odm/lib64", "/vendor/lib", "/vendor/lib64", "/system/lib", "/system/lib64"]:
                    hal_file = lib + "/hw/" + hardware + "." + prop + ".so"
                    if os.path.isfile(hal_file):
                        return prop
        return ""

    def find_hidl(intf):
        if args.vendor_type == "MAINLINE":
            return False

        try:
            sm = gbinder.ServiceManager("/dev/hwbinder")
            return intf in sm.list_sync()
        except:
            return False

    def append_override_device_props(props):
        try:
            override_file = "/usr/lib/furios/device/android_override.prop"
            if os.path.exists(override_file):
                with open(override_file, 'r') as override:
                    for prop in override:
                        props.append(prop.strip())
        except Exception as e:
            logging.error(f"Failed to read device override props: {e}")

    props = []

    if not os.path.exists("/dev/ashmem"):
        props.append("sys.use_memfd=true")

    # Added for security reasons
    props.append("ro.adb.secure=1")
    props.append("ro.debuggable=0")

    # SELinux
    props.append("ro.boot.selinux=enforcing")
    props.append("ro.boot.veritymode=enforcing")
    props.append("ro.build.selinux=1")

    # Device state
    props.append("vendor.boot.vbmeta.device_state=locked")
    props.append("ro.boot.verifiedbootstate=green")
    props.append("ro.boot.flash.locked=1")
    props.append("ro.boot.warranty_bit=0")
    props.append("ro.warranty_bit=0")
    props.append("ro.secure=1")
    props.append("ro.vendor.boot.warranty_bit=0")
    props.append("ro.vendor.warranty_bit=0")
    props.append("vendor.boot.vbmeta.device_state=locked")
    props.append("vendor.boot.verifiedbootstate=green")

    # Build tags
    props.append("ro.build.tags=release-keys")
    props.append("ro.odm.build.tags=release-keys")
    props.append("ro.system.build.tags=release-keys")
    props.append("ro.system_ext.build.tags=release-keys")
    props.append("ro.vendor.build.tags=release-keys")
    props.append("ro.vendor_dlkm.build.tags=release-keys")

    # AIDL radio prop
    props.append("furios-aidl-radio.start=1")

    egl = tools.helpers.props.host_get(args, "ro.hardware.egl")
    dri, _ = tools.helpers.gpu.getDriNode(args)

    gralloc = find_hal("gralloc")
    if not gralloc:
        if find_hidl("android.hardware.graphics.allocator@4.0::IAllocator/default"):
            gralloc = "android"
    if not gralloc:
        if dri:
            gralloc = "gbm"
            egl = "mesa"
        else:
            gralloc = "default"
            egl = "swiftshader"
        props.append("debug.stagefright.ccodec=0")
    props.append("ro.hardware.gralloc=" + gralloc)

    if egl != "":
        props.append("ro.hardware.egl=" + egl)

    media_profiles = tools.helpers.props.host_get(args, "media.settings.xml")
    if media_profiles != "":
        media_profiles = media_profiles.replace("vendor/", "vendor_extra/")
        media_profiles = media_profiles.replace("odm/", "odm_extra/")
        props.append("media.settings.xml=" + media_profiles)

    ccodec = tools.helpers.props.host_get(args, "debug.stagefright.ccodec")
    if ccodec != "":
        props.append("debug.stagefright.ccodec=" + ccodec)

    ext_library = tools.helpers.props.host_get(args, "ro.vendor.extension_library")
    if ext_library != "":
        ext_library = ext_library.replace("vendor/", "vendor_extra/")
        ext_library = ext_library.replace("odm/", "odm_extra/")
        props.append("ro.vendor.extension_library=" + ext_library)

    vulkan = find_hal("vulkan")
    if not vulkan and dri:
        vulkan = tools.helpers.gpu.getVulkanDriver(args, os.path.basename(dri))
    if vulkan:
        props.append("ro.hardware.vulkan=" + vulkan)

    treble = tools.helpers.props.host_get(args, "ro.treble.enabled")
    if treble != "true":
        camera = find_hal("camera")
        if camera != "":
            props.append("ro.hardware.camera=" + camera)
        else:
            if args.vendor_type == "MAINLINE":
                props.append("ro.hardware.camera=v4l2")

    opengles = tools.helpers.props.host_get(args, "ro.opengles.version")
    if opengles == "":
        opengles = "196609"
    props.append("ro.opengles.version=" + opengles)

    props.append("waydroid.tools_version=" + tools.config.version)

    if args.vendor_type == "MAINLINE":
        props.append("ro.vndk.lite=true")

    for product in ["brand", "device", "manufacturer", "model", "name"]:
        prop_product = tools.helpers.props.host_get(
            args, "ro.product.vendor." + product)
        if prop_product != "":
            props.append("ro.product.waydroid." + product + "=" + prop_product)
        else:
            if os.path.isfile("/proc/device-tree/" + product):
                with open("/proc/device-tree/" + product) as f:
                    f_value = f.read().strip().rstrip('\x00')
                    if f_value != "":
                        props.append("ro.product.waydroid." +
                                     product + "=" + f_value)

    prop_fp = tools.helpers.props.host_get(args, "ro.vendor.build.fingerprint")
    if prop_fp != "":
        props.append("ro.build.fingerprint=" + prop_fp)

    # now append/override with values in [properties] section of waydroid.cfg
    cfg = tools.config.load(args)
    for k, v in cfg["properties"].items():
        for idx, elem in enumerate(props):
            if (k+"=") in elem:
                props.pop(idx)
        props.append(k+"="+v)

    append_override_device_props(props)

    base_props = open(args.work + "/waydroid_base.prop", "w")
    for prop in props:
        base_props.write(prop + "\n")
    base_props.close()


def setup_host_perms(args):
    if not os.path.exists(tools.config.defaults["host_perms"]):
        os.mkdir(tools.config.defaults["host_perms"])

    treble = tools.helpers.props.host_get(args, "ro.treble.enabled")
    if treble != "true":
        return

    sku = tools.helpers.props.host_get(args, "ro.boot.product.hardware.sku")
    copy_list = []
    copy_list.extend(
        glob.glob("/vendor/etc/permissions/android.hardware.nfc.*"))
    if os.path.exists("/vendor/etc/permissions/android.hardware.consumerir.xml"):
        copy_list.append("/vendor/etc/permissions/android.hardware.consumerir.xml")
    copy_list.extend(
        glob.glob("/odm/etc/permissions/android.hardware.nfc.*"))
    if os.path.exists("/odm/etc/permissions/android.hardware.consumerir.xml"):
        copy_list.append("/odm/etc/permissions/android.hardware.consumerir.xml")
    if sku != "":
        copy_list.extend(
            glob.glob("/odm/etc/permissions/sku_{}/android.hardware.nfc.*".format(sku)))
        if os.path.exists("/odm/etc/permissions/sku_{}/android.hardware.consumerir.xml".format(sku)):
            copy_list.append(
                "/odm/etc/permissions/sku_{}/android.hardware.consumerir.xml".format(sku))

    for filename in copy_list:
        shutil.copy(filename, tools.config.defaults["host_perms"])

def status(args):
    command = ["lxc-info", "-P", tools.config.defaults["lxc"], "-n", "waydroid", "-sH"]
    try:
        return tools.helpers.run.user(args, command, output_return=True).strip()
    except:
        logging.info("Couldn't get LXC status. Assuming STOPPED.")
        return "STOPPED"

def wait_for_running(args):
    lxc_status = status(args)
    timeout = 10
    while lxc_status != "RUNNING" and timeout > 0:
        lxc_status = status(args)
        logging.info(
            "waiting {} seconds for container to start...".format(timeout))
        timeout = timeout - 1
        time.sleep(1)
    if lxc_status != "RUNNING":
        raise OSError("container failed to start")

def start(args):
    command = ["lxc-start", "-P", tools.config.defaults["lxc"],
               "-F", "-n", "waydroid", "--", "/init"]
    tools.helpers.run.user(args, command, output="background")
    wait_for_running(args)
    # Workaround lxc-start changing stdout/stderr permissions to 700
    os.chmod(args.log, 0o666)

def stop(args):
    command = ["lxc-stop", "-P",
               tools.config.defaults["lxc"], "-n", "waydroid", "-k"]
    tools.helpers.run.user(args, command)

def freeze(args):
    command = ["lxc-freeze", "-P", tools.config.defaults["lxc"], "-n", "waydroid"]
    tools.helpers.run.user(args, command)

def unfreeze(args):
    command = ["lxc-unfreeze", "-P",
               tools.config.defaults["lxc"], "-n", "waydroid"]
    tools.helpers.run.user(args, command)

ANDROID_ENV = {
    "PATH": "/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin",
    "ANDROID_ROOT": "/system",
    "ANDROID_DATA": "/data",
    "ANDROID_STORAGE": "/storage",
    "ANDROID_ART_ROOT": "/apex/com.android.art",
    "ANDROID_I18N_ROOT": "/apex/com.android.i18n",
    "ANDROID_TZDATA_ROOT": "/apex/com.android.tzdata",
    "ANDROID_RUNTIME_ROOT": "/apex/com.android.runtime",
    "BOOTCLASSPATH": "/apex/com.android.wifi/javalib/framework-wifi.jar:/apex/com.android.wifi/javalib/service-wifi.jar:/apex/com.android.uwb/javalib/framework-uwb.jar:/apex/com.android.uwb/javalib/service-uwb.jar:/apex/com.android.tethering/javalib/framework-connectivity-t.jar:/apex/com.android.tethering/javalib/framework-connectivity.jar:/apex/com.android.tethering/javalib/framework-tethering.jar:/apex/com.android.tethering/javalib/service-connectivity.jar:/apex/com.android.sdkext/javalib/framework-sdkextensions.jar:/apex/com.android.scheduling/javalib/framework-scheduling.jar:/apex/com.android.scheduling/javalib/service-scheduling.jar:/apex/com.android.permission/javalib/framework-permission-s.jar:/apex/com.android.permission/javalib/framework-permission.jar:/apex/com.android.permission/javalib/service-permission.jar:/apex/com.android.os.statsd/javalib/framework-statsd.jar:/apex/com.android.os.statsd/javalib/service-statsd.jar:/apex/com.android.ondevicepersonalization/javalib/framework-ondevicepersonalization.jar:/apex/com.android.mediaprovider/javalib/framework-mediaprovider.jar:/apex/com.android.media/javalib/service-media-s.jar:/apex/com.android.media/javalib/updatable-media.jar:/apex/com.android.ipsec/javalib/android.net.ipsec.ike.jar:/apex/com.android.i18n/javalib/core-icu4j.jar:/apex/com.android.conscrypt/javalib/conscrypt.jar:/apex/com.android.btservices/javalib/framework-bluetooth.jar:/apex/com.android.btservices/javalib/service-bluetooth.jar:/apex/com.android.art/javalib/apache-xml.jar:/apex/com.android.art/javalib/bouncycastle.jar:/apex/com.android.art/javalib/core-libart.jar:/apex/com.android.art/javalib/core-oj.jar:/apex/com.android.art/javalib/okhttp.jar:/apex/com.android.art/javalib/service-art.jar:/apex/com.android.appsearch/javalib/framework-appsearch.jar:/apex/com.android.appsearch/javalib/service-appsearch.jar:/apex/com.android.adservices/javalib/framework-adservices.jar:/apex/com.android.adservices/javalib/framework-sdksandbox.jar:/apex/com.android.adservices/javalib/service-adservices.jar:/apex/com.android.adservices/javalib/service-sdksandbox.jar:/system/framework/abx.jar:/system/framework/am.jar:/system/framework/android.hidl.base-V1.0-java.jar:/system/framework/android.hidl.manager-V1.0-java.jar:/system/framework/android.test.base.jar:/system/framework/android.test.mock.jar:/system/framework/android.test.runner.jar:/system/framework/appwidget.jar:/system/framework/bmgr.jar:/system/framework/bu.jar:/system/framework/com.android.location.provider.jar:/system/framework/com.android.mediadrm.signer.jar:/system/framework/content.jar:/system/framework/ext.jar:/system/framework/framework-graphics.jar:/system/framework/framework.jar:/system/framework/hid.jar:/system/framework/ims-common.jar:/system/framework/incident-helper-cmd.jar:/system/framework/javax.obex.jar:/system/framework/lockagent.jar:/system/framework/locksettings.jar:/system/framework/monkey.jar:/system/framework/org.apache.http.legacy.jar:/system/framework/org.lineageos.platform.jar:/system/framework/services.jar:/system/framework/sm.jar:/system/framework/svc.jar:/system/framework/telecom.jar:/system/framework/telephony-common.jar:/system/framework/uiautomator.jar:/system/framework/uinput.jar:/system/framework/voip-common.jar"
}

def android_env_attach_options():
    env = [k + "=" + v for k, v in ANDROID_ENV.items()]
    return [x for var in env for x in ("--set-var", var)]

def shell(args):
    state = status(args)
    if state == "FROZEN":
        unfreeze(args)
    elif state != "RUNNING":
        logging.error("WayDroid container is {}".format(state))
        return
    command = ["lxc-attach", "-P", tools.config.defaults["lxc"],
               "-n", "waydroid", "--clear-env"]
    command.extend(android_env_attach_options())
    if args.uid!=None:
        command.append("--uid="+str(args.uid))
    if args.gid!=None:
        command.append("--gid="+str(args.gid))
    elif args.uid!=None:
        command.append("--gid="+str(args.uid))
    if args.nolsm or args.allcaps or args.nocgroup:
        elevatedprivs = "--elevated-privileges="
        addpipe = False
        if args.nolsm:
            if addpipe:
                elevatedprivs+="|"
            elevatedprivs+="LSM"
            addpipe = True
        if args.allcaps:
            if addpipe:
                elevatedprivs+="|"
            elevatedprivs+="CAP"
            addpipe = True
        if args.nocgroup:
            if addpipe:
                elevatedprivs+="|"
            elevatedprivs+="CGROUP"
            addpipe = True
        command.append(elevatedprivs)
    if args.context!=None and not args.nolsm:
        command.append("--context="+args.context)
    command.append("--")
    if args.COMMAND:
        command.extend(args.COMMAND)
    else:
        command.append("/system/bin/sh")
    subprocess.run(command)
    if state == "FROZEN":
        freeze(args)

def screen_toggle(args):
    screen_state = sleep_status()
    if screen_state:
        args.COMMAND = ['input', 'keyevent', '224']  # key_wakeup
    else:
        args.COMMAND = ['input', 'keyevent', '223']  # key_sleep

    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def sleep_status():
    command = ["lxc-attach", "-P", tools.config.defaults["lxc"], "-n", "waydroid", "--clear-env"] + \
              android_env_attach_options() + ["--", "dumpsys", "power"]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        logging.info("Failed to check sleep status")
        return False

    lines = result.stdout.split('\n')
    for line in lines:
        if 'mWakefulness=' in line:
            wakefulness = line.split('mWakefulness=')[1].strip()
            if wakefulness == "Awake":
                return False
            elif wakefulness == "Asleep":
                return True
            else:
                return False
            break
    else:
        return False

def install_base_apk(args):
    args.COMMAND = ['pm', 'install', '/data/waydroid_tmp/base.apk']

    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def remove_app(args, packageName):
    args.COMMAND = ['pm', 'uninstall', packageName]

    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def open_app_present():
    command = ["lxc-attach", "-P", tools.config.defaults["lxc"], "-n", "waydroid", "--clear-env"] + \
              android_env_attach_options() + ["--", "dumpsys", "window", "windows"]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        logging.info("Failed to check open app presence")
        return False

    target_lines = re.findall(".*mInputMethodTarget.*", result.stdout)

    for line in target_lines:
        if "com.android.launcher" in line:
            return False

    return True

def toggle_nfc(args):
    nfc_state = nfc_status()
    if nfc_state:
        args.COMMAND = ['service', 'call', 'nfc', '7']  # stop
    else:
        args.COMMAND = ['service', 'call', 'nfc', '8']  # start

    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def nfc_status():
    command = ["lxc-attach", "-P", tools.config.defaults["lxc"], "-n", "waydroid", "--clear-env"] + \
              android_env_attach_options() + ["--", "dumpsys", "nfc"]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        logging.info("Failed to check nfc status")
        return False

    lines = result.stdout.split('\n')
    for line in lines:
        if 'mState=' in line:
            state = line.split('mState=')[1].strip()
            if state == "off" or state == "turning off":
                return False
            elif state == "on" or state == "turning on":
                return True
            else:
                return False
            break
    else:
        return True # on startup we don't have indication, but its true

def logcat(args):
    args.COMMAND = ["/system/bin/logcat"]
    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def force_finish_setup(args):
    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None

    args.COMMAND = ['settings', 'put', 'secure', 'user_setup_complete', '1']
    shell(args)

    args.COMMAND = ['settings', 'put', 'global', 'device_provisioned', '1']
    shell(args)

    args.COMMAND = ['settings', 'put', 'global', 'setup_wizard_has_run', '1']
    shell(args)

def clear_app_data(args, package_name):
    args.COMMAND = ['pm', 'clear', package_name]
    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def kill_app(args, package_name):
    args.COMMAND = ['am', 'force-stop', package_name]
    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def kill_pid(args, pid):
    args.COMMAND = ['kill', '-9', pid]
    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def setprop(args, propname, propvalue):
    args.COMMAND = ['setprop', propname, propvalue]
    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)

def getprop(propname):
    command = ["lxc-attach", "-P", tools.config.defaults["lxc"], "-n", "waydroid", "--clear-env"] + \
              android_env_attach_options() + ["--", "getprop", propname]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        logging.info(f"Failed to getprop {propname}")
        return ""
    return result.stdout.strip()

def watch_prop(propname):
    command = ["lxc-attach", "-P", tools.config.defaults["lxc"], "-n", "waydroid", "--clear-env"] + \
              android_env_attach_options() + ["--", "propwatch", propname]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            logging.info(f"Failed to watch the prop {propname}")
            return ""
        return result.stdout.strip()
    except Exception as e:
        logging.error(f"Failed to watch the prop {propname}: {e}")
