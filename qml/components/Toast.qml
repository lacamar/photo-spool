import QtQuick
import PhotoImport

Item {
    id: root
    anchors.bottom: parent.bottom
    anchors.horizontalCenter: parent.horizontalCenter
    anchors.bottomMargin: 28
    width: bubble.implicitWidth
    height: bubble.implicitHeight
    z: 1000

    function show(message) {
        label.text = message
        hideTimer.restart()
        bubble.opacity = 1
    }

    Timer { id: hideTimer; interval: 3200; onTriggered: bubble.opacity = 0 }

    Rectangle {
        id: bubble
        anchors.centerIn: parent
        implicitWidth: label.implicitWidth + 28
        implicitHeight: label.implicitHeight + 18
        radius: Theme.radiusMedium
        color: Theme.isDark ? "#2c2f3a" : "#1c1e24"
        opacity: 0
        Behavior on opacity { NumberAnimation { duration: Theme.animMedium } }

        Text {
            id: label
            anchors.centerIn: parent
            color: "#ffffff"
            font.pixelSize: 13
        }
    }
}
