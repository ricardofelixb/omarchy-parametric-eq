import QtQuick

Item {
  property var bar: null
  property real minimum: 0
  property real maximum: 1
  property real step: 0
  property bool integer: false
  property int tickCount: 0
  property real value: 0
  property bool dragging: false
  property real liveValue: value
  signal released(real value)
  implicitHeight: 24
}
