import QtQuick
import QtQuick.Controls
import PhotoSpool

Rectangle {
    id: root
    property string icon: ""
    property string tooltip: ""
    signal clicked()

    implicitWidth: 34
    implicitHeight: 34
    radius: 17
    color: mouseArea.containsMouse ? Theme.accentSoft : Theme.chipBackground
    scale: mouseArea.pressed ? 0.94 : 1

    Behavior on color { ColorAnimation { duration: Theme.animFast } }
    Behavior on scale { NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutCubic } }

    Icon {
        anchors.centerIn: parent
        name: root.icon
        size: 16
        color: mouseArea.containsMouse ? Theme.accent : Theme.textPrimary
    }

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
