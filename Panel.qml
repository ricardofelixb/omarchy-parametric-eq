import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.prudhviy99.parametric-eq"
  ipcTarget: "io.github.prudhviy99.parametric-eq"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root
  property var eqState: ({ enabled: false, bands: [], response: [], available_presets: [], snapshots: { A: null, B: null } })
  property int selectedBand: 0
  property bool busy: false
  property string statusMessage: "Loading audio graph…"
  property var pendingCommand: null
  readonly property bool equalizerEnabled: eqState && eqState.enabled === true
  readonly property var selected: eqState && eqState.bands && eqState.bands.length > selectedBand ? eqState.bands[selectedBand] : null
  readonly property bool selectedUsesGain: selected && (selected.type === "bell" || selected.type === "low-shelf" || selected.type === "high-shelf")
  readonly property string backendPath: decodeURIComponent(String(Qt.resolvedUrl("backend/peqctl.py")).replace("file://", ""))
  readonly property color fg: bar ? bar.foreground : "white"
  readonly property color ac: "#cacccc"

  function scrollToOverview() {
    if (scrollArea && scrollArea.contentItem) scrollArea.contentItem.contentY = 0
  }
  function open() {
    root.controller.show()
    Qt.callLater(scrollToOverview)
    refresh()
  }
  function openFromHotkey() { open() }
  function close() { root.controller.hide() }
  function toggle() { if (opened) close(); else openFromHotkey() }
  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function") return bar.switchPanelFrom(barIdentity, direction)
    return false
  }

  function ingest(text) {
    try {
      var parsed = JSON.parse(String(text || ""))
      if (parsed.error) { statusMessage = parsed.error; return }
      eqState = parsed
      if (selectedBand >= eqState.bands.length) selectedBand = Math.max(0, eqState.bands.length - 1)
      statusMessage = parsed.apply_mode === "rebuilt" ? "Audio graph rebuilt" : (parsed.runtime && parsed.runtime.node_present ? "Live · PipeWire" : "Audio graph unavailable")
    } catch (error) {
      statusMessage = "Could not read EQ state"
    }
  }

  function refresh() {
    if (!stateProc.running) stateProc.running = true
  }

  function execute(arguments) {
    var command = [backendPath].concat(arguments)
    if (actionProc.running) {
      pendingCommand = command
      return
    }
    actionProc.command = command
    busy = true
    actionProc.running = true
  }

  function runPending() {
    if (!pendingCommand) return
    var command = pendingCommand
    pendingCommand = null
    actionProc.command = command
    busy = true
    actionProc.running = true
  }

  function toggleEqualizer() { execute([equalizerEnabled ? "disable" : "enable"]) }
  function commitBand(enabled, kind, frequency, gain, q) {
    if (!selected) return
    var nextBands = eqState.bands.slice()
    var nextBand = Object.assign({}, selected, {
      enabled: enabled,
      type: kind,
      frequency: Number(frequency),
      gain: Number(gain),
      q: Number(q)
    })
    nextBands[selectedBand] = nextBand
    eqState = Object.assign({}, eqState, { bands: nextBands, preset: "Custom" })
    execute(["set-band", String(selectedBand + 1), String(enabled), kind, String(frequency), String(gain), String(q)])
  }
  function commitBandEditors() {
    frequencyControl.commitPending()
    gainControl.commitPending()
    qControl.commitPending()
  }
  function refreshBandEditors() {
    frequencyControl.refreshInput()
    gainControl.refreshInput()
    qControl.refreshInput()
  }
  function selectBand(index) {
    if (!eqState.bands || eqState.bands.length === 0) return
    var bounded = Math.max(0, Math.min(eqState.bands.length - 1, index))
    if (bounded === selectedBand) return
    commitBandEditors()
    selectedBand = bounded
    Qt.callLater(refreshBandEditors)
  }
  function logFrequency(sliderValue) { return 20 * Math.pow(1000, sliderValue) }
  function frequencySlider(frequency) { return Math.log(Math.max(20, Math.min(20000, frequency)) / 20) / Math.log(1000) }
  function logQ(sliderValue) { return 0.1 * Math.pow(100, sliderValue) }
  function qSlider(q) { return Math.log(Math.max(0.1, Math.min(10, q)) / 0.1) / Math.log(100) }
  function formatFrequency(value) { return value >= 1000 ? (value / 1000).toFixed(value >= 10000 ? 0 : 1) + " kHz" : Math.round(value) + " Hz" }
  function formatSigned(value) { return (value > 0 ? "+" : "") + Number(value).toFixed(1) + " dB" }

  Component.onCompleted: refresh()
  onOpenedChanged: if (opened) refresh()
  onSelectedBandChanged: Qt.callLater(refreshBandEditors)

  Process {
    id: stateProc
    command: [root.backendPath, "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (!actionProc.running && !root.pendingCommand) root.ingest(text)
    }
  }

  Process {
    id: actionProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (!root.pendingCommand) root.ingest(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (String(text || "").trim() !== "") {
        try { root.statusMessage = JSON.parse(text).error } catch (error) { root.statusMessage = String(text).trim() }
      }
    }
    onRunningChanged: if (!running) {
      root.busy = false
      Qt.callLater(root.runPending)
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(620))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(760))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: presetDropdown.popupOpen || typeDropdown.popupOpen || autoEqPath.activeFocus || presetName.activeFocus
        || frequencyControl.inputActiveFocus || gainControl.inputActiveFocus || qControl.inputActiveFocus || preampControl.inputActiveFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: content.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

        Column {
          id: content
          width: scrollArea.width
          spacing: Style.space(14)

          Item {
            width: parent.width
            implicitHeight: hero.implicitHeight
            Column {
              id: hero
              anchors.left: parent.left
              anchors.right: parent.right
              Text {
                text: "Parametric EQ"
                color: root.fg
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }
              Text {
                text: root.busy ? "APPLYING CHANGES…" : root.statusMessage.toUpperCase()
                color: Qt.darker(root.fg, 1.35)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.1
              }
            }
          }

          Toggle {
            width: parent.width
            label: root.equalizerEnabled ? "Equalizer is ON" : "Equalizer is BYPASSED"
            description: root.equalizerEnabled
              ? "Audio is passing through the filters and preamp. Click anywhere here to bypass it."
              : "No EQ or preamp is affecting your audio. Click anywhere here to turn processing on."
            checked: root.equalizerEnabled
            foreground: root.fg
            accent: root.ac
            fontFamily: root.bar.fontFamily
            enabled: !root.busy
            onClicked: root.toggleEqualizer()
          }

          BorderSurface {
            width: parent.width
            implicitHeight: quickPresetContent.implicitHeight + Style.space(22)
            radius: Style.cornerRadius
            color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.035)
            borderSpec: Border.flat(Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.10), 1)

            Column {
              id: quickPresetContent
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              spacing: Style.space(6)

              Text {
                text: "QUICK PRESET"
                color: Qt.darker(root.fg, 1.35)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1
              }
              Row {
                width: parent.width
                spacing: Style.space(10)
                Dropdown {
                  id: presetDropdown
                  width: parent.width - applyPreset.implicitWidth - Style.space(10)
                  label: "Choose a sound profile"
                  fontFamily: root.bar.fontFamily
                  options: root.eqState.available_presets || []
                  value: root.eqState.preset || "Flat"
                }
                Button {
                  id: applyPreset
                  text: "APPLY"
                  bordered: true
                  enabled: !root.busy
                  tooltipText: "Apply the selected preset"
                  onClicked: root.execute(["preset", "apply", String(presetDropdown.value)])
                }
              }
            }
          }

          PanelSeparator { foreground: root.fg }
          PanelSectionHeader { text: "PREAMP & HEADROOM"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          Toggle {
            width: parent.width
            label: root.eqState.auto_preamp ? "Automatic headroom is ON" : "Automatic headroom is OFF"
            description: root.eqState.auto_preamp
              ? "Preamp is automatically reduced so the modeled peak stays at or below 0 dB."
              : "Use the manual preamp below. Positive values can clip."
            checked: root.eqState.auto_preamp === true
            foreground: root.fg
            accent: root.ac
            fontFamily: root.bar.fontFamily
            onClicked: root.execute(["set-auto-preamp", String(!root.eqState.auto_preamp)])
          }
          EqSlider {
            id: preampControl
            bar: root.bar
            title: root.equalizerEnabled ? "MANUAL PREAMP" : "MANUAL PREAMP · INACTIVE WHILE BYPASSED"
            manualValue: root.eqState.preamp_db || 0
            manualMinimum: -30
            manualMaximum: 12
            decimals: 1
            nudgeStep: 0.1
            suffix: "dB"
            minimum: -30; maximum: 12; step: 0.1
            value: root.eqState.preamp_db || 0
            enabled: root.equalizerEnabled && !root.eqState.auto_preamp
            opacity: enabled ? 1 : 0.4
            onCommitted: function(value) { root.execute(["set-preamp", String(value)]) }
            onManualCommitted: function(value) { root.execute(["set-preamp", String(value)]) }
          }
          Text {
            visible: !root.equalizerEnabled || root.eqState.auto_preamp
            width: parent.width
            text: !root.equalizerEnabled
              ? "Turn the master equalizer ON before the preamp can affect audio."
              : "Turn automatic headroom OFF to set the preamp manually."
            color: root.fg
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          BorderSurface {
            width: parent.width
            height: Style.space(180)
            radius: Style.cornerRadius
            color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.025)
            borderSpec: Border.flat(Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12), 1)
            ResponseGraph {
              anchors.fill: parent
              anchors.margins: Style.space(10)
              foreground: root.fg
              accent: root.ac
              warning: Color.urgent
              response: root.eqState.response || []
              bands: root.eqState.bands || []
              selectedBand: root.selectedBand
              processingEnabled: root.equalizerEnabled
              headroomWarning: root.eqState.headroom_warning === true
              onBandSelected: function(index) { root.selectBand(index) }
            }
            BorderSurface {
              visible: !root.equalizerEnabled
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(12)
              implicitWidth: bypassLabel.implicitWidth + Style.space(16)
              implicitHeight: bypassLabel.implicitHeight + Style.space(9)
              color: Color.popups.background
              borderSpec: Border.flat(root.fg, 1)
              radius: Style.cornerRadius
              Text {
                id: bypassLabel
                anchors.centerIn: parent
                text: "MASTER BYPASSED"
                color: root.fg
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1
              }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(10)
            Text {
              width: parent.width / 3
              text: "PREAMP  " + root.formatSigned(root.eqState.effective_preamp_db || 0)
              color: root.fg
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Text {
              width: parent.width / 3
              text: "PEAK  " + root.formatSigned(root.eqState.estimated_peak_db || 0)
              color: root.eqState.headroom_warning ? Color.urgent : root.fg
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Text {
              width: parent.width / 3 - Style.space(20)
              horizontalAlignment: Text.AlignRight
              text: root.eqState.headroom_warning ? "CLIPPING RISK" : "HEADROOM OK"
              color: root.eqState.headroom_warning ? Color.urgent : Qt.darker(root.fg, 1.35)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
          }

          PanelSeparator { foreground: root.fg }
          PanelSectionHeader { text: "BANDS"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          Flow {
            width: parent.width
            spacing: Style.space(7)
            Repeater {
              model: root.eqState.bands || []
              Button {
                required property var modelData
                required property int index
                text: String(index + 1) + "  " + (modelData.enabled ? "ON" : "OFF") + "  " + root.formatFrequency(modelData.frequency)
                active: index === root.selectedBand
                bordered: index === root.selectedBand || modelData.enabled
                opacity: modelData.enabled ? 1 : 0.55
                onClicked: root.selectBand(index)
              }
            }
          }

          BorderSurface {
            visible: root.selected !== null
            width: parent.width
            implicitHeight: bandControls.implicitHeight + Style.space(24)
            radius: Style.cornerRadius
            color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.035)
            borderSpec: Border.flat(Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.10), 1)

            Column {
              id: bandControls
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(14)
              anchors.rightMargin: Style.space(14)
              spacing: Style.space(9)

              Item {
                width: parent.width
                implicitHeight: selectedBandTitle.implicitHeight
                Text {
                  id: selectedBandTitle
                  anchors.left: parent.left
                  text: root.selected ? "BAND " + (root.selectedBand + 1) + "  ·  " + root.formatFrequency(root.selected.frequency) : "BAND"
                  color: root.fg
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.subtitle
                  font.bold: true
                }
                Text {
                  anchors.right: parent.right
                  text: root.selected && root.selected.enabled
                    ? (root.equalizerEnabled ? "ACTIVE" : "ON · MASTER OFF")
                    : "BYPASSED"
                  color: root.fg
                  opacity: root.selected && root.selected.enabled ? 1 : 0.5
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  font.letterSpacing: 1
                }
              }

              Toggle {
                width: parent.width
                label: root.selected && root.selected.enabled ? "Band is ON" : "Band is OFF"
                description: root.selected && root.selected.enabled
                  ? (root.equalizerEnabled
                    ? "This filter is actively affecting audio and contributes to the response curve."
                    : "This filter is ready, but cannot affect audio until the master equalizer is ON.")
                  : "This filter is bypassed and has no effect on audio."
                checked: root.selected && root.selected.enabled
                foreground: root.fg
                accent: root.ac
                fontFamily: root.bar.fontFamily
                onClicked: if (root.selected) root.commitBand(!root.selected.enabled, root.selected.type, root.selected.frequency, root.selected.gain, root.selected.q)
              }

              Row {
                width: parent.width
                spacing: Style.space(10)
                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  text: "FILTER SHAPE"
                  color: Qt.darker(root.fg, 1.35)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  width: Style.space(110)
                }
                Dropdown {
                  id: typeDropdown
                  width: parent.width - Style.space(120)
                  label: "Choose filter shape"
                  fontFamily: root.bar.fontFamily
                  options: [
                    { value: "bell", label: "Bell / peak" },
                    { value: "low-shelf", label: "Low shelf" },
                    { value: "high-shelf", label: "High shelf" },
                    { value: "low-pass", label: "Low pass" },
                    { value: "high-pass", label: "High pass" },
                    { value: "notch", label: "Notch" },
                    { value: "band-pass", label: "Band pass" }
                  ]
                  value: root.selected ? root.selected.type : "bell"
                  onChanged: function(value) { if (root.selected) root.commitBand(root.selected.enabled, value, root.selected.frequency, root.selected.gain, root.selected.q) }
                }
              }

              EqSlider {
                id: frequencyControl
                bar: root.bar
                title: "FREQUENCY"
                manualValue: root.selected ? root.selected.frequency : 1000
                manualMinimum: 20
                manualMaximum: 20000
                decimals: 0
                nudgeStep: 1
                suffix: "Hz"
                sliderToManual: function(value) { return root.logFrequency(value) }
                value: root.selected ? root.frequencySlider(root.selected.frequency) : 0.5
                onCommitted: function(value) { if (root.selected) root.commitBand(root.selected.enabled, root.selected.type, root.logFrequency(value), root.selected.gain, root.selected.q) }
                onManualCommitted: function(value) { if (root.selected) root.commitBand(root.selected.enabled, root.selected.type, value, root.selected.gain, root.selected.q) }
              }
              EqSlider {
                id: gainControl
                bar: root.bar
                title: "GAIN"
                manualValue: root.selected ? root.selected.gain : 0
                manualMinimum: -24
                manualMaximum: 20
                decimals: 1
                nudgeStep: 0.1
                suffix: "dB"
                minimum: -24; maximum: 20; step: 0.1
                value: root.selected ? root.selected.gain : 0
                enabled: root.selectedUsesGain
                opacity: root.selectedUsesGain ? 1 : 0.35
                onCommitted: function(value) { if (root.selected) root.commitBand(root.selected.enabled, root.selected.type, root.selected.frequency, value, root.selected.q) }
                onManualCommitted: function(value) { if (root.selected) root.commitBand(root.selected.enabled, root.selected.type, root.selected.frequency, value, root.selected.q) }
              }
              Text {
                visible: !root.selectedUsesGain
                text: "Gain is not used by this filter shape."
                color: Qt.darker(root.fg, 1.35)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
              }
              EqSlider {
                id: qControl
                bar: root.bar
                title: "Q / BANDWIDTH"
                manualValue: root.selected ? root.selected.q : 0.707
                manualMinimum: 0.1
                manualMaximum: 10
                decimals: 2
                nudgeStep: 0.01
                suffix: "Q"
                sliderToManual: function(value) { return root.logQ(value) }
                value: root.selected ? root.qSlider(root.selected.q) : 0.4
                onCommitted: function(value) { if (root.selected) root.commitBand(root.selected.enabled, root.selected.type, root.selected.frequency, root.selected.gain, root.logQ(value)) }
                onManualCommitted: function(value) { if (root.selected) root.commitBand(root.selected.enabled, root.selected.type, root.selected.frequency, root.selected.gain, value) }
              }

              Item {
                width: parent.width
                implicitHeight: Math.max(bandSaveHint.implicitHeight, resetBand.implicitHeight)
                Text {
                  id: bandSaveHint
                  anchors.left: parent.left
                  anchors.verticalCenter: parent.verticalCenter
                  text: "AUTO-SAVE · ENTER OR ✓ APPLIES NOW"
                  color: Qt.darker(root.fg, 1.35)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  font.letterSpacing: 0.6
                }
                Button {
                  id: resetBand
                  anchors.right: parent.right
                  text: "RESET BAND"
                  tooltipText: "Return this band to a neutral bell at the same frequency"
                  onClicked: if (root.selected) root.commitBand(true, "bell", root.selected.frequency, 0, 0.707)
                }
              }
            }
          }

          PanelSeparator { foreground: root.fg }
          PanelSectionHeader { text: "COMPARE & SAVE"; foreground: root.fg; fontFamily: root.bar.fontFamily }

          Row {
            width: parent.width
            spacing: Style.space(8)
            Button { text: "UNDO"; onClicked: root.execute(["undo"]) }
            Button { text: "RESET"; onClicked: root.execute(["reset"]) }
            Button { text: "SAVE A"; onClicked: root.execute(["snapshot", "save", "A"]) }
            Button { text: "A"; active: root.eqState.snapshots.A !== null; onClicked: root.execute(["snapshot", "apply", "A"]) }
            Button { text: "SAVE B"; onClicked: root.execute(["snapshot", "save", "B"]) }
            Button { text: "B"; active: root.eqState.snapshots.B !== null; onClicked: root.execute(["snapshot", "apply", "B"]) }
          }

          Row {
            width: parent.width
            spacing: Style.space(10)
            TextField {
              id: presetName
              width: parent.width - savePreset.implicitWidth - Style.space(10)
              placeholderText: "New preset name"
              foreground: root.fg
              accent: root.ac
              font.family: root.bar.fontFamily
            }
            Button {
              id: savePreset
              text: "SAVE PRESET"
              enabled: presetName.text.trim() !== ""
              onClicked: root.execute(["preset", "save", presetName.text.trim()])
            }
          }

          PanelSeparator { foreground: root.fg }
          PanelSectionHeader { text: "AUTOEQ IMPORT"; foreground: root.fg; fontFamily: root.bar.fontFamily }
          Text {
            width: parent.width
            text: "Import the standard ParametricEQ.txt format. The file is read locally and never uploaded."
            color: Qt.darker(root.fg, 1.35)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
          Row {
            width: parent.width
            spacing: Style.space(10)
            TextField {
              id: autoEqPath
              width: parent.width - importButton.implicitWidth - Style.space(10)
              placeholderText: "~/Downloads/ParametricEQ.txt"
              foreground: root.fg
              accent: root.ac
              font.family: root.bar.fontFamily
            }
            Button {
              id: importButton
              text: "IMPORT"
              bordered: true
              enabled: autoEqPath.text.trim() !== ""
              onClicked: root.execute(["import-autoeq", autoEqPath.text.trim()])
            }
          }
        }
      }
    }
  }
}
