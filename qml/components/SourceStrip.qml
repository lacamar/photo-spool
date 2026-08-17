import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import PhotoSpool

Item {
    id: root

    property string expandedKey: ""
    signal sourceClicked(string key, string label)

    implicitHeight: 144

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
        case "blockdev": return "💾"
        case "mtp": return "📷"
        case "iphone": return "📱"
        case "folder": return "📁"
        default: return "🔌"
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
            width: model.mounted ? 172 : 96
            height: 144
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

                Text {
                    text: root.kindIcon(model.kind)
                    font.pixelSize: 26
                    Layout.alignment: Qt.AlignHCenter
                }
                Text {
                    text: "Tap to mount"
                    color: Theme.textSecondary
                    font.pixelSize: 10
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
                    font.pixelSize: 9
                    horizontalAlignment: Text.AlignHCenter
                    elide: Text.ElideRight
                    maximumLineCount: 1
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignHCenter
                }
                Text {
                    text: root.kindLabel(model.kind)
                    color: Theme.textSecondary
                    font.pixelSize: 8
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
                spacing: 5
                visible: model.mounted

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6

                    Text {
                        text: root.kindIcon(model.kind)
                        font.pixelSize: 18
                    }
                    Text {
                        text: model.label
                        color: Theme.textPrimary
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: root.kindLabel(model.kind)
                    color: Theme.textSecondary
                    font.pixelSize: 9
                    elide: Text.ElideRight
                }

                Text {
                    Layout.fillWidth: true
                    text: !model.statsLoaded ? "Scanning…"
                          : model.fileCount === 0 ? "No photos found"
                          : model.newCount > 0 ? model.fileCount + " photo" + (model.fileCount === 1 ? "" : "s") + " · " + model.newCount + " new"
                          : model.fileCount + " photo" + (model.fileCount === 1 ? "" : "s") + " · all imported"
                    color: (model.statsLoaded && model.newCount > 0) ? Theme.accent : Theme.textSecondary
                    font.pixelSize: 10
                    elide: Text.ElideRight
                }

                Item { Layout.fillHeight: true }

                Rectangle {
                    Layout.fillWidth: true
                    visible: model.statsLoaded && model.capacityBytes > 0
                    implicitHeight: 4
                    radius: 2
                    color: Theme.chipBackground

                    Rectangle {
                        height: parent.height
                        radius: parent.radius
                        color: (model.usedBytes / Math.max(model.capacityBytes, 1)) > 0.9 ? Theme.danger : Theme.textSecondary
                        width: parent.width * Math.min(1, model.usedBytes / Math.max(model.capacityBytes, 1))
                    }
                }
                Text {
                    Layout.fillWidth: true
                    visible: model.statsLoaded && model.capacityBytes > 0
                    text: root.formatBytes(model.freeBytes) + " free of " + root.formatBytes(model.capacityBytes)
                    color: Theme.textSecondary
                    font.pixelSize: 8
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
            Text {
                visible: model.removable
                text: "✕"
                color: removeMouse.containsMouse ? Theme.danger : Theme.textSecondary
                font.pixelSize: 11
                anchors.top: parent.top
                anchors.right: parent.right
                anchors.margins: 4
                MouseArea {
                    id: removeMouse
                    anchors.fill: parent
                    anchors.margins: -6
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: appController.removeFolder(model.sourceKey)

                    ToolTip.visible: containsMouse
                    ToolTip.delay: 500
                    ToolTip.text: "Stop watching this folder"
                }
            }
        }

        // ListView's own `spacing` doesn't reliably apply between the
        // last delegate and the footer -- confirmed visually, the
        // footer sat flush against the rightmost card with no gap at
        // all. Wrapped in a plain Item so the gap is explicit (x offset)
        // rather than relying on that.
        footer: Item {
            width: 84 + 10
            height: 144

            Rectangle {
                x: 10
                width: 84
                height: 144
                radius: Theme.radiusMedium
                color: addMouse.containsMouse ? Theme.chipBackground : "transparent"
                border.width: 1
                border.color: Theme.border

                Behavior on color { ColorAnimation { duration: Theme.animFast } }

                Text {
                    anchors.centerIn: parent
                    text: "+ Folder"
                    color: Theme.textSecondary
                    font.pixelSize: 12
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
