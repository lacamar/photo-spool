import QtQuick
import QtQuick.Controls
import PhotoSpool

Rectangle {
    id: root
    property string icon: ""
    property int badgeCount: 0
    property string tooltip: ""
    signal clicked()

    implicitWidth: 34
    implicitHeight: 34
    radius: 17
    color: mouseArea.containsMouse ? Theme.accentSoft : Theme.chipBackground
    scale: mouseArea.pressed ? 0.94 : 1

    Behavior on color { ColorAnimation { duration: Theme.animFast } }
    Behavior on scale { NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic } }

    Text {
        anchors.centerIn: parent
        text: root.icon
        font.pixelSize: 14
    }

    Rectangle {
        id: badge
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

        SequentialAnimation {
            id: popAnim
            NumberAnimation { target: badge; property: "scale"; to: 1.35; duration: Theme.animFast; easing.type: Easing.OutCubic }
            NumberAnimation { target: badge; property: "scale"; to: 1.0; duration: Theme.animFast; easing.type: Easing.OutCubic }
        }
    }

    onBadgeCountChanged: if (badgeCount > 0) popAnim.restart()

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }

    ToolTip.visible: root.tooltip.length > 0 && mouseArea.containsMouse
    ToolTip.delay: 500
    ToolTip.text: root.tooltip
}
