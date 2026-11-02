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
    radius: Theme.radiusSmall
    color: mouseArea.pressed ? Theme.tint(Theme.textPrimary, 0.1) : mouseArea.containsMouse ? Theme.hover : "transparent"

    Behavior on color { ColorAnimation { duration: Theme.animFast } }

    Icon {
        anchors.centerIn: parent
        name: root.icon
        size: 18
        color: Theme.textPrimary
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }

    ToolTip.visible: root.tooltip.length > 0 && mouseArea.containsMouse
    ToolTip.delay: 600
    ToolTip.text: root.tooltip
}
