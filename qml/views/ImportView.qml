import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import PhotoImport
import "../components"

Item {
    id: root
    signal sessionOpened(int sessionId, string deviceLabel)

    FolderDialog {
        id: folderDialog
        title: "Choose a folder to import ARW files from"
        onAccepted: appController.importNow(folderDialog.selectedFolder)
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 22
        spacing: 16

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: statusRow.implicitHeight + 24
            radius: Theme.radiusMedium
            color: Theme.surfaceElevated
            border.width: 1
            border.color: Theme.border

            RowLayout {
                id: statusRow
                anchors.fill: parent
                anchors.margins: 12
                spacing: 12

                Rectangle {
                    width: 9; height: 9; radius: 4.5
                    color: appController.watchEnabled ? Theme.healthFresh : Theme.textSecondary
                    Layout.alignment: Qt.AlignVCenter
                }

                Text {
                    text: appController.watchEnabled
                          ? "Watching for SD card or camera…"
                          : "Auto-detect is off"
                    color: Theme.textPrimary
                    font.pixelSize: 13
                    Layout.fillWidth: true
                }

                Text {
                    visible: !appController.dnglabReady
                    text: "Setting up DNG converter…"
                    color: Theme.textSecondary
                    font.pixelSize: 11
                }

                HeaderButton {
                    label: "Import folder…"
                    onClicked: folderDialog.open()
                }
            }
        }

        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 10
            model: sessionModel

            delegate: SessionCard {
                width: list.width
                sessionId: model.sessionId
                deviceLabel: model.deviceLabel
                kindLabel: model.kindLabel
                startedAt: model.startedAt
                status: model.status
                foundCount: model.foundCount
                importedCount: model.importedCount
                duplicateCount: model.duplicateCount
                failedCount: model.failedCount
                bytesSaved: model.bytesSaved
                errorMessage: model.errorMessage
                ejectable: model.kind !== "manual" && model.ejectablePath.length > 0
                ejected: model.ejected
                progressDone: model.progressDone
                progressTotal: model.progressTotal
                progressFile: model.progressFile
                onOpened: root.sessionOpened(model.sessionId, model.deviceLabel)
                onEjectRequested: appController.ejectSession(model.sessionId)
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: list.count === 0
            spacing: 6

            Item { Layout.fillHeight: true }
            Text {
                text: "Plug in your SD card or Sony A7R3 to get started"
                color: Theme.textSecondary
                font.pixelSize: 14
                Layout.alignment: Qt.AlignHCenter
            }
            Text {
                text: "New ARW files are converted to lossless DNG and filed into your library automatically."
                color: Theme.textSecondary
                font.pixelSize: 11
                Layout.alignment: Qt.AlignHCenter
            }
            Item { Layout.fillHeight: true }
        }
    }
}
