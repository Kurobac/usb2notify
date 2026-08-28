PYTHON ?= python3
USER_BIN_DIR := $(HOME)/.local/bin
USER_SYSTEMD_DIR := $(HOME)/.local/share/systemd/user

.PHONY: test check install enable disable uninstall

test:
	$(PYTHON) -B -m unittest discover -s tests -v

check:
	$(PYTHON) -B -m unittest discover -s tests
	$(PYTHON) -B src/usb2notify.py --help >/dev/null

install:
	install -Dm755 src/usb2notify.py "$(USER_BIN_DIR)/usb2notify"
	install -Dm644 systemd/usb2notify.service.in "$(USER_SYSTEMD_DIR)/usb2notify.service"
	sed -i 's|@USB2NOTIFY_EXEC@|%h/.local/bin/usb2notify|' "$(USER_SYSTEMD_DIR)/usb2notify.service"

enable:
	systemctl --user daemon-reload
	systemctl --user enable --now usb2notify.service

disable:
	systemctl --user disable --now usb2notify.service

uninstall: disable
	rm -f "$(USER_BIN_DIR)/usb2notify"
	rm -f "$(USER_SYSTEMD_DIR)/usb2notify.service"
	systemctl --user daemon-reload
