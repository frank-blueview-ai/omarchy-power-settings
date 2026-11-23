import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import Quickshell.Services.UPower
import qs.Commons

Item {
  id: root

  WatchedFile {
    id: config
    path: Quickshell.env("HOME") + "/.config/omarchy/shell.json"
  }

  Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  readonly property var policy: {
    try { return JSON.parse(config.text).idle || ({}) }
    catch (error) { return ({}) }
  }
  readonly property bool keepLidAwake: (UPower.onBattery ? policy.lidAwakeBattery : policy.lidAwakeAC) === true
  readonly property int idleSeconds: {
    const value = UPower.onBattery ? policy.sleepBattery : policy.sleepAC
    return Number.isInteger(value) && value >= 60 && value <= 86400 ? value : 0
  }
  property bool lidHeld: false

  // Only claim lid events. Manual suspend, low-battery actions, and the
  // desktop's clamshell monitor handling keep their own paths.
  Process {
    id: inhibitor
    command: ["systemd-inhibit", "--what=handle-lid-switch", "--mode=block",
              "--who=Blue View Power", "--why=Keep awake when lid closes",
              "sh", "-c", "echo ready; exec cat >/dev/null"]
    stdinEnabled: true
    running: root.keepLidAwake
    stdout: SplitParser { onRead: line => { if (line === "ready") root.lidHeld = true } }
    onExited: function(code) {
      root.lidHeld = false
      if (code !== 0 && root.keepLidAwake) {
        Quickshell.execDetached(["notify-send", strings.t("settings"), strings.t("lidError")])
      }
    }
  }

  IdleMonitor {
    id: monitor
    enabled: root.idleSeconds > 0
    timeout: Math.max(1, root.idleSeconds)
    respectInhibitors: true
    onIsIdleChanged: if (isIdle && enabled) idleCheck.running = true
  }

  Process {
    id: idleCheck
    command: ["omarchy-toggle-idle", "status"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          if (monitor.enabled && monitor.isIdle && JSON.parse(text).enabled === false)
            suspendProc.running = true
        } catch (error) { console.warn("Power settings: idle state unavailable") }
      }
    }
  }
  Process {
    id: suspendProc
    command: ["systemctl", "--no-ask-password", "--check-inhibitors=yes", "suspend"]
    onExited: function(code) {
      if (code !== 0) console.warn("Power settings: automatic suspend declined", code)
    }
  }

  ShellIpc {
    target: "bvos.power.policy"
    function status(): string {
      return JSON.stringify({ onBattery: UPower.onBattery, lidHeld: root.lidHeld,
                              idleSeconds: root.idleSeconds, keepLidAwake: root.keepLidAwake })
    }
  }
}
