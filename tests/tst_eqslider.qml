import QtQuick
import QtTest
import ".."

Item {
  id: harness
  width: 640
  height: 240

  QtObject {
    id: fakeBar
    property color foreground: "white"
    property color background: "#101315"
    property string fontFamily: "sans-serif"
  }

  Component {
    id: sliderComponent
    EqSlider {
      property real capturedValue: -1
      bar: fakeBar
      title: "FREQUENCY"
      manualValue: 1000
      manualMinimum: 20
      manualMaximum: 20000
      decimals: 0
      nudgeStep: 1
      suffix: "Hz"
      autoApplyDelay: 30
      onManualCommitted: function(value) { capturedValue = value }
    }
  }

  TestCase {
    id: testCase
    name: "EqSliderManualEntry"
    when: windowShown

    function createSlider() {
      var slider = createTemporaryObject(sliderComponent, harness)
      verify(slider !== null)
      wait(0)
      return slider
    }

    function replaceText(field, value) {
      field.forceActiveFocus()
      wait(0)
      keyClick(Qt.Key_A, Qt.ControlModifier)
      for (var index = 0; index < value.length; index++) keyClick(value.charAt(index))
    }

    function test_autoApplyAfterTyping() {
      var slider = createSlider()
      var field = findChild(slider, "manualInput")
      verify(field !== null)
      replaceText(field, "1234")
      verify(slider.inputDirty)
      tryCompare(slider, "capturedValue", 1234, 500)
      verify(!slider.inputDirty)
    }

    function test_switchCommitsThenRefreshesNewValue() {
      var slider = createSlider()
      var field = findChild(slider, "manualInput")
      verify(field !== null)
      replaceText(field, "4321")
      verify(slider.commitPending())
      compare(slider.capturedValue, 4321)

      slider.manualValue = 880
      slider.refreshInput()
      compare(field.text, "880")
      verify(!slider.inputDirty)
    }
  }
}
