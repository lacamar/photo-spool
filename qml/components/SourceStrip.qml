import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import PhotoSpool

Item {
    id: root

    property string expandedKey: ""
    signal sourceClicked(string key, string label)

    implicitHeight: 104

    function kindLabel(kind) {
        switch (kind) {
        case "blockdev": return "SD card / storage"
        case "mtp": return "Camera (MTP)"
        case "iphone": return "iPhone"
        case "folder": return "Folder"
        default: return "Device"
        }
    }

    function kindIcon(kind) {
        switch (kind) {
        case "blockdev": return "sdcard"
        case "mtp": return "camera"
        case "iphone": return "phone"
        case "folder": return "folder"
        default: return "plug"
        }
    }

    function formatBytes(n) {
        if (n <= 0) return "0 B"
        var units = ["B", "KB", "MB", "GB", "TB"]
        var i = 0
        var v = n
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
        return (v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i]
    }

    FolderDialog {
        id: addFolderDialog
        title: "Add a folder to watch"
        onAccepted: appController.addFolder(addFolderDialog.selectedFolder)
    }

    ListView {
        anchors.fill: parent
        orientation: ListView.Horizontal
        spacing: 10
        clip: true
        model: sourcesModel

        delegate: Rectangle {
            id: tile
            width: model.mounted ? 196 : 104
            height: root.implicitHeight
            radius: Theme.radiusMedium
            color: root.expandedKey === model.sourceKey
                   ? Theme.accentSoft
                   : (tileMouse.containsMouse ? Theme.chipBackground : Theme.surfaceElevated)
            border.width: 1
            border.color: root.expandedKey === model.sourceKey ? Theme.accent : Theme.border
            opacity: model.mounted ? 1.0 : 0.6

            Behavior on width { NumberAnimation { duration: Theme.animFast } }
            Behavior on color { ColorAnimation { duration: Theme.animFast } }
            Behavior on opacity { NumberAnimation { duration: Theme.animFast } }

            // Unmounted: the original compact "tap to mount" tile -- no
            // stats exist yet, so there's nothing to show a card for.
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 3
                visible: !model.mounted

                Icon {
                    name: root.kindIcon(model.kind)
                    size: 24
                    color: Theme.textSecondary
                    Layout.alignment: Qt.AlignHCenter
                }
                Text {
                    text: "Tap to mount"
                    color: Theme.textSecondary
                    font.pixelSize: 11
                    font.weight: Font.Medium
                    horizontalAlignment: Text.AlignHCenter
                    elide: Text.ElideRight
                    maximumLineCount: 2
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignHCenter
                }
                Text {
                    text: model.label
                    color: Theme.textSecondary
                    font.pixelSize: 11
                    horizontalAlignment: Text.AlignHCenter
                    elide: Text.ElideRight
                    maximumLineCount: 1
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignHCenter
                }
            }

            // Mounted: a full stat card -- icon/label header, photo counts,
            // and a capacity bar (when the mount reports one; gvfs/MTP
            // mounts often don't support statvfs, which is fine).
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 10
                spacing: 3
                visible: model.mounted

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6

                    Icon {
                        name: root.kindIcon(model.kind)
                        size: 16
                        color: Theme.textPrimary
                    }
                    Text {
                        text: model.label
                        color: Theme.textPrimary
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.rightMargin: 16
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: model.cameraModel.length > 0 ? model.cameraModel : root.kindLabel(model.kind)
                    color: Theme.textSecondary
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }

                Text {
                    Layout.fillWidth: true
                    text: !model.statsLoaded ? "Scanning…"
                          : model.fileCount === 0 ? "No photos found"
                          : model.newCount > 0 ? model.fileCount + " photo" + (model.fileCount === 1 ? "" : "s") + " · " + model.newCount + " new"
                          : model.fileCount + " photo" + (model.fileCount === 1 ? "" : "s") + " · all imported"
                    color: (model.statsLoaded && model.newCount > 0) ? Theme.accent : Theme.textSecondary
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }

                Item { Layout.fillHeight: true }

                Rectangle {
                    Layout.fillWidth: true
                    visible: model.statsLoaded && model.capacityBytes > 0
                    implicitHeight: 4
                    radius: 2
                    color: Theme.border

                    Rectangle {
                        height: parent.height
                        radius: parent.radius
                        color: (model.usedBytes / Math.max(model.capacityBytes, 1)) > 0.9 ? Theme.healthDue : Theme.textSecondary
                        width: parent.width * Math.min(1, model.usedBytes / Math.max(model.capacityBytes, 1))
                    }
                }
                Text {
                    Layout.fillWidth: true
                    visible: model.statsLoaded && model.capacityBytes > 0
                    text: root.formatBytes(model.freeBytes) + " free of " + root.formatBytes(model.capacityBytes)
                    color: Theme.textSecondary
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
            }

            MouseArea {
                id: tileMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.sourceClicked(model.sourceKey, model.label)

                ToolTip.visible: containsMouse
                ToolTip.delay: 500
                ToolTip.text: model.mounted
                              ? "Browse " + model.label + " (" + root.kindLabel(model.kind) + ")"
                              : "Tap to mount " + model.label
            }

            // Declared after tileMouse (and so stacked on top of it) --
            // otherwise the whole-tile MouseArea above swallows every
            // click in this corner, including the remove button's own,
            // making it look clickable but do nothing.
            Icon {
                readonly property bool ejectable: !model.removable && model.mounted
                visible: model.removable || (ejectable && (tileMouse.containsMouse || cornerMouse.containsMouse))
                name: model.removable ? "close" : "eject"
                size: 12
                color: cornerMouse.containsMouse ? (model.removable ? Theme.danger : Theme.accent) : Theme.textSecondary
                anchors.top: parent.top
                anchors.right: parent.right
                anchors.margins: 8
                MouseArea {
                    id: cornerMouse
                    anchors.fill: parent
                    anchors.margins: -6
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: model.removable ? appController.removeFolder(model.sourceKey)
                                               : appController.ejectSource(model.sourceKey)

                    ToolTip.visible: containsMouse
                    ToolTip.delay: 500
                    ToolTip.text: model.removable ? "Stop watching this folder" : "Safely eject this device"
                }
            }
        }

        // ListView's own `spacing` doesn't reliably apply between the
        // last delegate and the footer -- confirmed visually, the
        // footer sat flush against the rightmost card with no gap at
        // all. Wrapped in a plain Item so the gap is explicit (x offset)
        // rather than relying on that.
        footer: Item {
            width: 40 + 10
            height: root.implicitHeight

            Rectangle {
                x: 10
                width: 40
                height: parent.height
                radius: Theme.radiusMedium
                color: addMouse.containsMouse ? Theme.chipBackground : "transparent"
                border.width: 1
                border.color: Theme.border

                Behavior on color { ColorAnimation { duration: Theme.animFast } }

                Icon {
                    anchors.centerIn: parent
                    name: "plus"
                    size: 16
                    color: Theme.textSecondary
                }

                MouseArea {
                    id: addMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: addFolderDialog.open()

                    ToolTip.visible: containsMouse
                    ToolTip.delay: 500
                    ToolTip.text: "Watch an extra folder for new photos"
                }
            }
        }
    }
}
