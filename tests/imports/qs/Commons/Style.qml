pragma Singleton
import QtQuick

QtObject {
  function space(value) { return value }
  property QtObject font: QtObject {
    property int caption: 12
  }
}
