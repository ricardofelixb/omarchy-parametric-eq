import QtQuick

Item {
  id: root
  property color foreground: "white"
  property color accent: "cyan"
  property color warning: "#ff6b6b"
  property var response: []
  property var bands: []
  property int selectedBand: 0
  property bool processingEnabled: true
  property bool headroomWarning: false
  signal bandSelected(int index)

  implicitHeight: 190
  readonly property real plotLeft: 34
  readonly property real plotRight: 9
  readonly property real plotTop: 9
  readonly property real plotBottom: 20
  readonly property real plotWidth: Math.max(1, width - plotLeft - plotRight)
  readonly property real plotHeight: Math.max(1, height - plotTop - plotBottom)

  function xForFrequency(frequency) {
    return plotLeft + Math.log(Math.max(20, Math.min(20000, frequency)) / 20) / Math.log(1000) * plotWidth
  }
  function yForDb(db) {
    return plotTop + plotHeight / 2 - Math.max(-24, Math.min(24, db)) / 48 * plotHeight
  }

  onResponseChanged: plot.requestPaint()
  onBandsChanged: plot.requestPaint()
  onSelectedBandChanged: plot.requestPaint()
  onProcessingEnabledChanged: plot.requestPaint()
  onHeadroomWarningChanged: plot.requestPaint()
  onForegroundChanged: plot.requestPaint()
  onAccentChanged: plot.requestPaint()
  onWarningChanged: plot.requestPaint()
  onWidthChanged: plot.requestPaint()
  onHeightChanged: plot.requestPaint()

  Canvas {
    id: plot
    anchors.fill: parent
    renderTarget: Canvas.FramebufferObject
    antialiasing: true
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      ctx.fillStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.035)
      ctx.fillRect(0, 0, width, height)

      var frequencies = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
      var plotBottomY = height - root.plotBottom

      if (root.headroomWarning) {
        ctx.fillStyle = Qt.rgba(root.warning.r, root.warning.g, root.warning.b, 0.08)
        ctx.fillRect(root.plotLeft, root.plotTop, root.plotWidth, root.yForDb(0) - root.plotTop)
      }

      ctx.lineWidth = 1
      ctx.strokeStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.11)
      for (var i = 0; i < frequencies.length; i++) {
        var x = root.xForFrequency(frequencies[i])
        ctx.beginPath(); ctx.moveTo(x, root.plotTop); ctx.lineTo(x, plotBottomY); ctx.stroke()
      }
      for (var db = -24; db <= 24; db += 6) {
        var y = root.yForDb(db)
        ctx.strokeStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, db === 0 ? 0.30 : 0.09)
        ctx.beginPath(); ctx.moveTo(root.plotLeft, y); ctx.lineTo(width - root.plotRight, y); ctx.stroke()
      }

      ctx.fillStyle = Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.58)
      ctx.font = "9px sans-serif"
      ctx.textAlign = "center"
      ctx.textBaseline = "bottom"
      var labelFrequencies = [20, 100, 1000, 10000, 20000]
      var labelText = ["20", "100", "1k", "10k", "20k"]
      for (var labelIndex = 0; labelIndex < labelFrequencies.length; labelIndex++)
        ctx.fillText(labelText[labelIndex], root.xForFrequency(labelFrequencies[labelIndex]), height - 1)

      ctx.textAlign = "left"
      ctx.textBaseline = "middle"
      var labelDb = [12, 0, -12]
      for (var dbIndex = 0; dbIndex < labelDb.length; dbIndex++) {
        var dbValue = labelDb[dbIndex]
        ctx.fillText((dbValue > 0 ? "+" : "") + dbValue, 2, root.yForDb(dbValue))
      }

      if (root.response && root.response.length > 1) {
        ctx.strokeStyle = root.processingEnabled
          ? root.accent
          : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.34)
        ctx.lineWidth = root.processingEnabled ? 2.4 : 1.5
        ctx.beginPath()
        for (var point = 0; point < root.response.length; point++) {
          var pair = root.response[point]
          var px = root.xForFrequency(pair[0])
          var py = root.yForDb(pair[1])
          if (point === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py)
        }
        ctx.stroke()
      }

      if (root.bands) {
        for (var bandIndex = 0; bandIndex < root.bands.length; bandIndex++) {
          var band = root.bands[bandIndex]
          var bx = root.xForFrequency(band.frequency)
          var by = root.yForDb((band.type === "bell" || band.type.indexOf("shelf") >= 0) ? band.gain : 0)
          if (bandIndex === root.selectedBand) {
            ctx.beginPath(); ctx.arc(bx, by, 7, 0, Math.PI * 2)
            ctx.strokeStyle = root.accent
            ctx.lineWidth = 1.5
            ctx.stroke()
          }
          ctx.beginPath(); ctx.arc(bx, by, bandIndex === root.selectedBand ? 4 : 3.5, 0, Math.PI * 2)
          ctx.fillStyle = band.enabled ? (bandIndex === root.selectedBand ? root.accent : root.foreground) : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.3)
          ctx.fill()
        }
      }
    }
  }

  MouseArea {
    anchors.fill: parent
    cursorShape: Qt.PointingHandCursor
    onClicked: function(mouse) {
      if (!root.bands || root.bands.length === 0) return
      var best = 0
      var distance = Number.MAX_VALUE
      for (var i = 0; i < root.bands.length; i++) {
        var delta = Math.abs(mouse.x - root.xForFrequency(root.bands[i].frequency))
        if (delta < distance) { distance = delta; best = i }
      }
      root.bandSelected(best)
    }
  }
}
