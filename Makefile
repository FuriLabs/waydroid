PREFIX := /usr

USE_SYSTEMD ?= 1
USE_DBUS_ACTIVATION ?= 1
USE_NFTABLES ?= 0

SYSCONFDIR := /etc
WAYDROID_DIR := $(PREFIX)/lib/waydroid
BIN_DIR := $(PREFIX)/bin
APPS_DIR := $(PREFIX)/share/applications
APPS_DIRECTORY_DIR := $(PREFIX)/share/desktop-directories
APPS_MENU_DIR := $(SYSCONFDIR)/xdg/menus/applications-merged
ICONS_DIR := $(PREFIX)/share/icons
SYSD_DIR := $(PREFIX)/lib/systemd/system
SYSD_USER_DIR := $(PREFIX)/lib/systemd/user
DBUS_DIR := $(PREFIX)/share/dbus-1
POLKIT_DIR := $(PREFIX)/share/polkit-1
APPARMOR_DIR := $(SYSCONFDIR)/apparmor.d
ANDROID_STORE_DIR := $(PREFIX)/lib/android-store
LIBEXEC_DIR := $(PREFIX)/libexec

INSTALL_WAYDROID_DIR := $(DESTDIR)$(WAYDROID_DIR)
INSTALL_BIN_DIR := $(DESTDIR)$(BIN_DIR)
INSTALL_APPS_DIR := $(DESTDIR)$(APPS_DIR)
INSTALL_APPS_DIRECTORY_DIR := $(DESTDIR)$(APPS_DIRECTORY_DIR)
INSTALL_APPS_MENU_DIR := $(DESTDIR)$(APPS_MENU_DIR)
INSTALL_ICONS_DIR := $(DESTDIR)$(ICONS_DIR)
INSTALL_SYSD_DIR := $(DESTDIR)$(SYSD_DIR)
INSTALL_SYSD_USER_DIR := $(DESTDIR)$(SYSD_USER_DIR)
INSTALL_DBUS_DIR := $(DESTDIR)$(DBUS_DIR)
INSTALL_POLKIT_DIR := $(DESTDIR)$(POLKIT_DIR)
INSTALL_APPARMOR_DIR := $(DESTDIR)$(APPARMOR_DIR)
INSTALL_ANDROID_STORE_DIR := $(DESTDIR)$(ANDROID_STORE_DIR)
INSTALL_LIBEXEC_DIR := $(DESTDIR)$(LIBEXEC_DIR)

build:
	@echo "Nothing to build, run 'make install' to copy the files!"

install:
	install -d $(INSTALL_WAYDROID_DIR) $(INSTALL_BIN_DIR) $(INSTALL_DBUS_DIR)/system.d $(INSTALL_POLKIT_DIR)/actions $(INSTALL_ANDROID_STORE_DIR)
	install -d $(INSTALL_APPS_DIR) $(INSTALL_ICONS_DIR)/hicolor/512x512/apps $(INSTALL_APPS_DIRECTORY_DIR) $(INSTALL_APPS_MENU_DIR) $(INSTALL_LIBEXEC_DIR)
	cp -a data tools waydroid.py $(INSTALL_WAYDROID_DIR)
	cp -a android-store/android-store.py android-store/repos $(INSTALL_ANDROID_STORE_DIR)
	ln -sf $(WAYDROID_DIR)/waydroid.py $(INSTALL_BIN_DIR)/waydroid
	ln -sf $(ANDROID_STORE_DIR)/android-store.py $(INSTALL_LIBEXEC_DIR)/android-store
	mv $(INSTALL_WAYDROID_DIR)/data/AppIcon.png $(INSTALL_ICONS_DIR)/hicolor/512x512/apps/waydroid.png
	mv $(INSTALL_WAYDROID_DIR)/data/*.desktop $(INSTALL_APPS_DIR)
	mv $(INSTALL_WAYDROID_DIR)/data/*.menu $(INSTALL_APPS_MENU_DIR)
	mv $(INSTALL_WAYDROID_DIR)/data/*.directory $(INSTALL_APPS_DIRECTORY_DIR)
	cp dbus/id.waydro.Container.conf $(INSTALL_DBUS_DIR)/system.d/
	cp dbus/id.waydro.Notification.conf $(INSTALL_DBUS_DIR)/system.d/
	cp dbus/id.waydro.StateChange.conf $(INSTALL_DBUS_DIR)/system.d/
	if [ $(USE_DBUS_ACTIVATION) = 1 ]; then \
		install -d $(INSTALL_DBUS_DIR)/system-services; \
		install -d $(INSTALL_DBUS_DIR)/services/; \
		cp dbus/id.waydro.Container.service $(INSTALL_DBUS_DIR)/system-services/; \
		cp dbus/id.waydro.Notification.service $(INSTALL_DBUS_DIR)/system-services/; \
		cp dbus/io.FuriOS.AndroidStore.service $(INSTALL_DBUS_DIR)/services/; \
	fi
	if [ $(USE_SYSTEMD) = 1 ]; then \
		install -d $(INSTALL_SYSD_DIR) $(INSTALL_SYSD_USER_DIR); \
		cp systemd/waydroid-container.service $(INSTALL_SYSD_DIR); \
		cp systemd/waydroid-notification-server.service $(INSTALL_SYSD_DIR); \
		cp systemd/waydroid-statechange-server.service $(INSTALL_SYSD_DIR); \
		cp systemd/waydroid-session.service $(INSTALL_SYSD_USER_DIR); \
	fi
	if [ $(USE_NFTABLES) = 1 ]; then \
		sed '/LXC_USE_NFT=/ s/false/true/' -i $(INSTALL_WAYDROID_DIR)/data/scripts/waydroid-net.sh; \
	fi

install_apparmor:
	install -d $(INSTALL_APPARMOR_DIR) $(INSTALL_APPARMOR_DIR)/lxc
	mkdir -p $(INSTALL_APPARMOR_DIR)/local/
	touch $(INSTALL_APPARMOR_DIR)/local/adbd
	touch $(INSTALL_APPARMOR_DIR)/local/android_app
	touch $(INSTALL_APPARMOR_DIR)/local/lxc-waydroid
	cp -f data/configs/apparmor_profiles/adbd $(INSTALL_APPARMOR_DIR)/adbd
	cp -f data/configs/apparmor_profiles/android_app $(INSTALL_APPARMOR_DIR)/android_app
	cp -f data/configs/apparmor_profiles/lxc-waydroid $(INSTALL_APPARMOR_DIR)/lxc/lxc-waydroid
	# Load the profiles if not just packaging
	if [ -z $(DESTDIR) ] && { aa-enabled --quiet || systemctl is-active -q apparmor; } 2>/dev/null; then \
		apparmor_parser -r -T -W "$(INSTALL_APPARMOR_DIR)/adbd"; \
		apparmor_parser -r -T -W "$(INSTALL_APPARMOR_DIR)/android_app"; \
		apparmor_parser -r -T -W "$(INSTALL_APPARMOR_DIR)/lxc/lxc-waydroid"; \
	fi
