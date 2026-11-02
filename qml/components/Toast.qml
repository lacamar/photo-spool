import QtQuick
import PhotoSpool

Item {
    id: root
    anchors.bottom: parent.bottom
    anchors.horizontalCenter: parent.horizontalCenter
    anchors.bottomMargin: 24
    width: bubble.implicitWidth
    height: bubble.implicitHeight
    z: 1000

    function show(message) {
        label.text = message
        hideTimer.restart()
        bubble.opacity = 1
    }

    Timer { id: hideTimer; interval: 3000; onTriggered: bubble.opacity = 0 }

    Rectangle {
        id: bubble
        anchors.centerIn: parent
        implicitWidth: label.implicitWidth + 36
        implicitHeight: 40
        radius: 20
        color: Theme.isDark ? "#3a3a40" : "#26262b"
        opacity: 0
        scale: opacity === 0 ? 0.92 : 1
        Behavior on opacity { NumberAnimation { duration: Theme.animMedium } }
        Behavior on scale { NumberAnimation { duration: Theme.animMedium; easing.type: Easing.OutCubic } }

        Text {
            id: label
            anchors.centerIn: parent
            color: "white"
            font.pixelSize: 14
        }
    }
}
