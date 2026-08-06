import QtQuick
import QtQuick.Effects

// A rounded-image tile whose corners follow a superellipse ("squircle")
// curve rather than Rectangle's circular-arc `radius` -- flatter sides,
// a smoother transition into the straight edges. The photo + placeholder
// are composited normally, then masked by a Canvas-drawn squircle path
// via MultiEffect (GPU shader mask) -- Canvas is only ever used to fill a
// path here, never to draw another item's texture: QtQuick's Canvas
// can't reliably grab a live Image item's texture (drawImage(Image) is a
// no-op in some renderer configurations, worth remembering if this ever
// gets "simplified" back to a plain Canvas composite).
Item {
    id: root

    property alias source: img.source
    property alias asynchronous: img.asynchronous
    property alias fillMode: img.fillMode
    property alias sourceSize: img.sourceSize
    property color placeholderColor: "transparent"
    property real cornerRadius: 8
    property real exponent: 4.2 // >2 reads as a squircle; 2 would be a plain circular corner

    Item {
        id: content
        anchors.fill: parent
        visible: false
        layer.enabled: true

        Rectangle { anchors.fill: parent; color: root.placeholderColor }
        Image { id: img; anchors.fill: parent }
    }

    Canvas {
        id: maskCanvas
        anchors.fill: parent
        visible: false

        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            var w = width, h = height
            if (w <= 0 || h <= 0) return
            var r = Math.min(root.cornerRadius, w / 2, h / 2)
            var n = root.exponent
            var steps = 10
            ctx.beginPath()
            _arc(ctx, w - r, r, r, n, steps, -Math.PI / 2, 0, true)
            _arc(ctx, w - r, h - r, r, n, steps, 0, Math.PI / 2, false)
            _arc(ctx, r, h - r, r, n, steps, Math.PI / 2, Math.PI, false)
            _arc(ctx, r, r, r, n, steps, Math.PI, 3 * Math.PI / 2, false)
            ctx.closePath()
            ctx.fillStyle = "white"
            ctx.fill()
        }

        // Superellipse |x/r|^n + |y/r|^n = 1, parametrized by angle so it
        // can be walked like a circular arc: x = r*sign(cos)*|cos|^(2/n).
        function _arc(ctx, cx, cy, r, n, steps, a0, a1, moveFirst) {
            for (var i = 0; i <= steps; i++) {
                var a = a0 + (a1 - a0) * (i / steps)
                var c = Math.cos(a), s = Math.sin(a)
                var x = cx + r * Math.sign(c) * Math.pow(Math.abs(c), 2 / n)
                var y = cy + r * Math.sign(s) * Math.pow(Math.abs(s), 2 / n)
                if (moveFirst && i === 0) ctx.moveTo(x, y)
                else ctx.lineTo(x, y)
            }
        }
    }

    MultiEffect {
        anchors.fill: parent
        source: content
        maskEnabled: true
        maskSource: maskCanvas
        maskThresholdMin: 0.5
        maskSpreadAtMin: 0.0
    }

    onWidthChanged: maskCanvas.requestPaint()
    onHeightChanged: maskCanvas.requestPaint()
}
