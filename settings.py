"""Power preferences for the local Blue View battery widget."""
import json
from pathlib import Path
import subprocess

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

CONFIG = Gio.File.new_for_path(str(Path.home() / ".config/omarchy/shell.json"))
UPOWER = "org.freedesktop.UPower"
DEVICE = UPOWER + ".Device"


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=5).stdout.strip()


def set_idle(key, value, file=CONFIG):
    # Merge against the latest revision, including changes made in other panels.
    for attempt in range(3):
        _, data, etag = file.load_contents(None)
        config = json.loads(data)
        config.setdefault("idle", {})[key] = value
        data = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode()
        try:
            file.replace_contents(data, etag, False, Gio.FileCreateFlags.NONE, None)
            return
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.WRONG_ETAG) or attempt == 2:
                raise


class Preferences(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Power settings", default_width=540, default_height=610)
        self.toasts = Adw.ToastOverlay()
        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(Adw.HeaderBar())
        self.toasts.set_child(self.toolbar)
        self.set_content(self.toasts)
        self.pending_charges = set()
        self.refresh()
        self.connect("notify::is-active", self.on_active)

    def on_active(self, window, prop):
        if self.is_active() and not self.pending_charges:
            self.refresh()

    def group(self, title, description=None):
        group = Adw.PreferencesGroup(title=title)
        if description:
            group.set_description(description)
        self.page.add(group)
        return group

    def report(self, error):
        message = getattr(error, "stderr", "") or str(error)
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message.strip()[:230]), timeout=6))

    def apply(self, callback, value):
        try:
            callback(value)
        except (GLib.Error, OSError, ValueError, subprocess.SubprocessError) as error:
            self.report(error)
            GLib.idle_add(self.refresh)

    def switch(self, group, title, enabled, callback, subtitle=None):
        row = Adw.SwitchRow(title=title, active=enabled, use_markup=False)
        if subtitle:
            row.set_subtitle(subtitle)
        group.add(row)
        row.connect("notify::active", lambda row, prop: self.apply(callback, row.get_active()))
        return row

    def choice(self, group, title, current, options, fmt, callback):
        values = sorted(set([*options, current]))
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new([fmt(v) for v in values]),
                           selected=values.index(current), use_markup=False)
        group.add(row)
        row.connect("notify::selected", lambda row, prop: self.apply(callback, values[row.get_selected()]))

    def refresh(self):
        try:
            _, data, _ = CONFIG.load_contents(None)
            idle = json.loads(data).get("idle", {})
            paused = json.loads(command("omarchy-toggle-idle", "status"))["enabled"]
            policy = json.loads(command("omarchy-shell", "bvos.power.policy", "status"))
        except (GLib.Error, OSError, ValueError, subprocess.SubprocessError) as error:
            self.report(error)
            return False

        self.page = Adw.PreferencesPage()
        self.toolbar.set_content(self.page)

        lid = self.group("When the lid closes",
                         "Turn on Keep awake for either power source. Turning it off restores the system lid policy.")
        first = self.switch(lid, "Keep awake on battery", idle.get("lidAwakeBattery", False),
                    lambda v: set_idle("lidAwakeBattery", v))
        self.switch(lid, "Keep awake when plugged in", idle.get("lidAwakeAC", False),
                    lambda v: set_idle("lidAwakeAC", v))
        if policy["keepLidAwake"] and not policy["lidHeld"]:
            self.report("The lid override is selected but has not taken effect.")

        sleep = self.group("Sleep after inactivity", "These timers also apply while the lid is closed.")
        self.timeout(sleep, "On battery", idle, "sleepBattery", 0)
        self.timeout(sleep, "When plugged in", idle, "sleepAC", 0)
        self.switch(sleep, "Pause idle actions", paused,
                    lambda v: command("omarchy-toggle-idle", "stay-awake" if v else "allow-idle"),
                    "Pauses automatic sleep, locking, and the screensaver.")

        screen = self.group("Display and lock")
        self.timeout(screen, "Start screensaver", idle, "screensaver", 150)
        self.timeout(screen, "Lock screen", idle, "lock", 300)
        try:
            raw = command("brightnessctl", "-c", "backlight", "-m")
            percent = int(raw.splitlines()[0].split(",")[3].rstrip("%"))
            self.choice(screen, "Screen brightness", percent, [10, 25, 50, 75, 100],
                        lambda v: f"{v}%", lambda v: command("brightnessctl", "-c", "backlight", "set", f"{v}%"))
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass  # An external display may have no controllable backlight.

        self.batteries()
        first.grab_focus()
        self.page.scroll_to_top()
        return False

    def timeout(self, group, title, idle, key, default):
        def label(seconds):
            if seconds == 0:
                return "Never"
            if seconds % 60:
                return f"{seconds} seconds"
            minutes = seconds // 60
            return "1 minute" if minutes == 1 else f"{minutes} minutes"
        self.choice(group, title, int(idle.get(key, default)), [0, 60, 300, 600, 900, 1800, 3600],
                    label, lambda value: set_idle(key, value))

    def batteries(self):
        group = None
        try:
            for path in command("upower", "-e").splitlines():
                device = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE,
                                                        None, UPOWER, path, DEVICE, None)
                def prop(key):
                    value = device.get_cached_property(key)
                    return value.unpack() if value is not None else None
                if prop("Type") != 2 or not prop("PowerSupply") or not prop("ChargeThresholdSupported"):
                    continue
                if group is None:
                    group = self.group("Battery charging")
                low, high = prop("ChargeStartThreshold"), prop("ChargeEndThreshold")
                subtitle = f"Charge from {low}% up to {high}%." if 0 < (low or 0) < (high or 0) <= 100 else None
                row = self.switch(group, f"Limit charging · {prop('NativePath')}", prop("ChargeThresholdEnabled"),
                                  lambda enabled, d=device: self.set_charge_limit(d, enabled), subtitle)
                row.set_sensitive(path not in self.pending_charges)
                row.connect("notify::active", lambda r, prop: r.set_sensitive(False))
        except (GLib.Error, OSError, subprocess.SubprocessError) as error:
            self.report(error)

    def set_charge_limit(self, device, enabled):
        path = device.get_object_path()
        self.pending_charges.add(path)

        def finished(proxy, result):
            self.pending_charges.discard(path)
            try:
                proxy.call_finish(result)
            except GLib.Error as error:
                self.report(error)
            self.refresh()
        device.call("EnableChargeThreshold", GLib.Variant("(b)", (enabled,)),
                    Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 120000, None, finished)


def activate(app):
    win = app.get_active_window() or Preferences(app)
    win.present()


if __name__ == "__main__":
    app = Adw.Application(application_id="local.blueview.PowerSettings")
    app.connect("activate", activate)
    app.run()
