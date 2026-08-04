import QtQuick
import PhotoImport

Rectangle {
    id: root
    property string label: ""
    property bool prominent: false
    signal clicked()

    implicitWidth: labelText.implicitWidth + 26
    implicitHeight: 34
    radius: height / 2
    color: prominent
           ? (mouseArea.containsMouse ? Qt.darker(Theme.accent, 1.15) : Theme.accent)
           : (mouseArea.containsMouse ? Theme.accentSoft : Theme.chipBackground)

    Behavior on color { ColorAnimation { duration: Theme.animFast } }

    Text {
        id: labelText
        anchors.centerIn: parent
        text: root.label
        font.pixelSize: 13
        font.weight: Font.Medium
        color: root.prominent ? Theme.textOnAccent : Theme.textPrimary
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
