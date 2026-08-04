import QtQuick
import PhotoImport

Rectangle {
    id: root
    property string icon: ""
    property int badgeCount: 0
    signal clicked()

    implicitWidth: 34
    implicitHeight: 34
    radius: 17
    color: mouseArea.containsMouse ? Theme.accentSoft : Theme.chipBackground

    Behavior on color { ColorAnimation { duration: Theme.animFast } }

    Text {
        anchors.centerIn: parent
        text: root.icon
        font.pixelSize: 14
    }

    Rectangle {
        visible: root.badgeCount > 0
        width: 16; height: 16; radius: 8
        color: Theme.danger
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: -2
        anchors.rightMargin: -2

        Text {
            anchors.centerIn: parent
            text: root.badgeCount > 9 ? "9+" : root.badgeCount
            color: "white"
            font.pixelSize: 9
            font.weight: Font.Bold
        }
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
