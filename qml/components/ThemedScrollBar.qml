import QtQuick
import QtQuick.Controls
import PhotoSpool

// A slim, always-visible, theme-colored scrollbar -- QtQuick Controls'
// Basic-style default only shows itself transiently while scrolling/
// hovering, easy to miss entirely on a tall photo grid/list.
ScrollBar {
    id: root
    policy: ScrollBar.AlwaysOn
    interactive: true

    contentItem: Rectangle {
        implicitWidth: 6
        implicitHeight: 6
        radius: width / 2
        color: root.pressed ? Theme.accent : Theme.textSecondary
        opacity: root.pressed ? 0.9 : (root.hovered ? 0.7 : 0.45)
        Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
        Behavior on color { ColorAnimation { duration: Theme.animFast } }
    }

    background: Rectangle {
        implicitWidth: 6
        implicitHeight: 6
        radius: width / 2
        color: Theme.chipBackground
        opacity: 0.5
    }
}
