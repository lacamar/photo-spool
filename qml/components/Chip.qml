import QtQuick
import PhotoImport

Rectangle {
    id: root
    property string text: ""
    property color bg: Theme.chipBackground
    property color fg: Theme.textSecondary

    implicitWidth: label.implicitWidth + 16
    implicitHeight: label.implicitHeight + 8
    radius: height / 2
    color: bg

    Behavior on color { ColorAnimation { duration: Theme.animMedium } }

    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: root.fg
        font.pixelSize: 11
        font.weight: Font.Medium

        Behavior on color { ColorAnimation { duration: Theme.animMedium } }
    }
}
