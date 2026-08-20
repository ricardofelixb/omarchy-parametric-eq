import QtQuick
import qs.Commons
import qs.Ui

Column {
  id: root
  required property var bar
  property string title: ""
  property string displayValue: ""
  property real manualValue: 0
  property real manualMinimum: minimum
  property real manualMaximum: maximum
  property int decimals: 1
  property real nudgeStep: step > 0 ? step : 1
  property string suffix: ""
  property var sliderToManual: function(v) { return v }
  property real minimum: 0
  property real maximum: 1
  property real value: 0
  property real step: 0
  property bool integer: false
  property int tickCount: 0
  property int autoApplyDelay: 650
  property bool inputDirty: false
  property alias inputActiveFocus: manualInput.activeFocus
  signal committed(real value)
  signal manualCommitted(real value)

  width: parent ? parent.width : implicitWidth
  spacing: Style.space(4)

  readonly property real shownManualValue: slider.dragging ? sliderToManual(slider.liveValue) : manualValue

  function rounded(value) {
    var factor = Math.pow(10, decimals)
    return Math.round(value * factor) / factor
  }

  function formatted(value) {
    return Number(value).toFixed(decimals)
  }

  function syncInput(force) {
    if (force || !manualInput.activeFocus) {
      autoApply.stop()
      inputDirty = false
      manualInput.text = formatted(shownManualValue)
    }
  }

  function commitTyped(keepFocus) {
    autoApply.stop()
    var parsed = Number(manualInput.text)
    if (isNaN(parsed) || !isFinite(parsed)) {
      if (!keepFocus) {
        inputDirty = false
        manualInput.focus = false
        Qt.callLater(function() { root.syncInput(true) })
      }
      return false
    }
    var bounded = Math.max(manualMinimum, Math.min(manualMaximum, parsed))
    var next = rounded(bounded)
    inputDirty = false
    manualInput.text = formatted(next)
    manualCommitted(next)
    if (!keepFocus) manualInput.focus = false
    return true
  }

  function commitPending() {
    if (!inputDirty) return true
    return commitTyped(false)
  }

  function refreshInput() {
    syncInput(true)
  }

  function nudge(direction) {
    autoApply.stop()
    var parsed = Number(manualInput.text)
    var base = inputDirty && !isNaN(parsed) && isFinite(parsed) ? parsed : manualValue
    var next = rounded(Math.max(manualMinimum, Math.min(manualMaximum, base + direction * nudgeStep)))
    inputDirty = false
    manualInput.text = formatted(next)
    manualCommitted(next)
  }

  onShownManualValueChanged: syncInput()
  Component.onCompleted: syncInput()

  Timer {
    id: autoApply
    interval: root.autoApplyDelay
    repeat: false
    onTriggered: if (root.inputDirty) root.commitTyped(true)
  }

  Item {
    width: parent.width
    implicitHeight: Math.max(titleLabel.implicitHeight, entryRow.implicitHeight)

    Text {
      id: titleLabel
      anchors.left: parent.left
      text: root.title
      color: root.bar.foreground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }
    Row {
      id: entryRow
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(5)

      Button {
        text: "−"
        horizontalPadding: Style.space(7)
        verticalPadding: Style.space(3)
        tooltipText: "Decrease " + root.title.toLowerCase()
        onClicked: root.nudge(-1)
      }

      TextField {
        id: manualInput
        objectName: "manualInput"
        width: Style.space(86)
        foreground: root.bar.foreground
        accent: root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: TextInput.AlignRight
        validator: DoubleValidator {
          bottom: root.manualMinimum
          top: root.manualMaximum
          decimals: root.decimals
          notation: DoubleValidator.StandardNotation
          locale: "C"
        }
        onTextEdited: {
          root.inputDirty = true
          autoApply.restart()
        }
        onEditingFinished: {
          if (root.inputDirty) root.commitTyped(false)
          else root.syncInput()
        }
        onActiveFocusChanged: if (!activeFocus) {
          autoApply.stop()
          if (root.inputDirty) root.commitTyped(false)
          else root.syncInput()
        }
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        text: root.suffix
        color: Qt.darker(root.bar.foreground, 1.35)
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        width: Style.space(28)
      }

      Button {
        text: "+"
        horizontalPadding: Style.space(7)
        verticalPadding: Style.space(3)
        tooltipText: "Increase " + root.title.toLowerCase()
        onClicked: root.nudge(1)
      }

      Button {
        text: "✓"
        horizontalPadding: Style.space(7)
        verticalPadding: Style.space(3)
        enabled: root.inputDirty
        opacity: enabled ? 1 : 0.28
        tooltipText: root.inputDirty ? "Apply typed " + root.title.toLowerCase() : root.title + " is saved"
        onClicked: root.commitTyped(false)
      }
    }
  }

  PanelSlider {
    id: slider
    bar: root.bar
    width: parent.width
    minimum: root.minimum
    maximum: root.maximum
    step: root.step
    integer: root.integer
    tickCount: root.tickCount
    value: root.value
    onReleased: function(v) { root.committed(v) }
  }
}
