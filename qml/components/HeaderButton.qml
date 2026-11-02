import QtQuick
import QtQuick.Controls
import PhotoSpool

Rectangle {
    id: root
    property string label: ""
    property bool prominent: false
    property string tooltip: ""
    signal clicked()

    readonly property color fill: prominent ? Theme.accent : Theme.chipBackground

    implicitWidth: labelText.implicitWidth + 32
    implicitHeight: 36
    radius: height / 2
    color: mouseArea.pressed ? Qt.darker(fill, 1.15) : mouseArea.containsMouse ? Qt.lighter(fill, 1.08) : fill

    Behavior on color { ColorAnimation { duration: Theme.animFast } }

    Text {
        id: labelText
        anchors.centerIn: parent
        text: root.label
        font.pixelSize: 14
        font.weight: Font.DemiBold
        color: root.prominent ? Theme.textOnAccent : Theme.textPrimary
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
