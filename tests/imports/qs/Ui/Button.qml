import QtQuick

Item {
  property string text: ""
  property string tooltipText: ""
  property real horizontalPadding: 0
  property real verticalPadding: 0
  signal clicked()
  implicitWidth: 28
  implicitHeight: 24
}
