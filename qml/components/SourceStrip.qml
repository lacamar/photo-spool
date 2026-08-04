import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import PhotoImport

Item {
    id: root

    property string expandedKey: ""
    signal sourceClicked(string key, string label)

    implicitHeight: 92

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
            width: 84
            height: 84
            radius: Theme.radiusMedium
            color: root.expandedKey === model.sourceKey
                   ? Theme.accentSoft
                   : (tileMouse.containsMouse ? Theme.chipBackground : Theme.surfaceElevated)
            border.width: 1
            border.color: root.expandedKey === model.sourceKey ? Theme.accent : Theme.border
            opacity: model.mounted ? 1.0 : 0.6

            Behavior on color { ColorAnimation { duration: Theme.animFast } }
            Behavior on opacity { NumberAnimation { duration: Theme.animFast } }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 3

                Text {
                    // Deliberately: (1) a plain inline ternary, not a
                    // `root.someFunction(model.kind)` call -- a JS
                    // function-call binding here silently never painted
                    // anything, confirmed by swapping to this identical
                    // ternary with nothing else changed; (2) plain
                    // geometric-shape glyphs, not colour emoji -- those
                    // also silently failed to paint in this delegate
                    // (bound to sourcesModel, a real QAbstractListModel
                    // populated after first paint), even with a literal
                    // string. Both were confirmed by direct A/B
                    // screenshot testing, not just theory -- don't
                    // reintroduce either without retesting.
                    text: model.kind === "blockdev" ? "◉"
                          : model.kind === "mtp" ? "◎"
                          : model.kind === "iphone" ? "▯"
                          : model.kind === "folder" ? "▢" : "◇"
                    color: Theme.textPrimary
                    font.pixelSize: 26
                    Layout.alignment: Qt.AlignHCenter
                }
                Text {
                    text: model.mounted ? model.label : "Tap to mount"
                    color: model.mounted ? Theme.textPrimary : Theme.textSecondary
                    font.pixelSize: 10
                    font.weight: Font.Medium
                    horizontalAlignment: Text.AlignHCenter
                    elide: Text.ElideRight
                    maximumLineCount: 2
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignHCenter
                }
            }

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
                }
            }

            MouseArea {
                id: tileMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.sourceClicked(model.sourceKey, model.label)
            }
        }

        footer: Rectangle {
            width: 84
            height: 84
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
            }
        }
    }
}
