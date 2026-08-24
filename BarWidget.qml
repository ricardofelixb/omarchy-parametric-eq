import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.prudhviy99.parametric-eq"

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool equalizerEnabled: panelLoader.item ? panelLoader.item.equalizerEnabled === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  readonly property string backendPath: decodeURIComponent(String(Qt.resolvedUrl("backend/peqctl.py")).replace("file://", ""))

  function open() { if (panelLoader.item) panelLoader.item.openFromHotkey() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()
  Component.onDestruction: Quickshell.execDetached([root.backendPath, "cleanup-if-disabled"])

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰓃"
    dimmed: !root.equalizerEnabled
    tooltipText: root.equalizerEnabled ? "Parametric EQ is processing audio" : "Parametric EQ is bypassed"
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton && panelLoader.item) panelLoader.item.toggleEqualizer()
      else root.togglePanel()
    }
  }
}
