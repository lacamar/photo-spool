import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PhotoImport
import "../components"

Item {
    id: root
    signal sessionOpened(int sessionId, string deviceLabel)

    function phaseLabel(phase, done, total) {
        switch (phase) {
        case "scanning": return "Found " + total + " photo" + (total === 1 ? "" : "s")
        case "checking": return "Checking " + done + "/" + total
        case "converting": return "Converting " + done + "/" + total
        case "placing": return "Filing " + done + "/" + total
        default: return "Starting…"
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 22
        spacing: 14

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: statusColumn.implicitHeight + 24
            radius: Theme.radiusMedium
            color: appController.activeSessionId >= 0 ? Theme.accentSoft : Theme.surfaceElevated
            border.width: 1
            border.color: appController.activeSessionId >= 0 ? Theme.accent : Theme.border

            Behavior on color { ColorAnimation { duration: Theme.animMedium } }
            Behavior on border.color { ColorAnimation { duration: Theme.animMedium } }

            ColumnLayout {
                id: statusColumn
                anchors.fill: parent
                anchors.margins: 12
                spacing: 8

                RowLayout {
                    id: statusRow
                    Layout.fillWidth: true
                    spacing: 12

                    Rectangle {
                        width: 9; height: 9; radius: 4.5
                        color: appController.activeSessionId >= 0
                               ? Theme.accent
                               : (appController.watchEnabled ? Theme.healthFresh : Theme.textSecondary)
                        Layout.alignment: Qt.AlignVCenter
                    }

                    Text {
                        text: {
                            if (appController.activeSessionId >= 0) {
                                var t = "Importing " + appController.activeLabel + ": "
                                       + root.phaseLabel(appController.activePhase, appController.activeDone,
                                                          appController.activeTotal)
                                if (appController.queuedCount > 0)
                                    t += " (+" + appController.queuedCount + " more queued)"
                                return t
                            }
                            return appController.watchEnabled
                                   ? "Auto-import is on — new cards import automatically"
                                   : "Auto-import is off — click a device below to import"
                        }
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

                    HeaderIconButton {
                        icon: "⟳"
                        onClicked: appController.refreshDevices()
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    visible: appController.activeSessionId >= 0
                    implicitHeight: 5
                    radius: 2.5
                    color: Theme.chipBackground

                    Rectangle {
                        height: parent.height
                        radius: parent.radius
                        color: Theme.accent
                        width: appController.activeTotal > 0
                               ? parent.width * Math.min(1, appController.activeDone / appController.activeTotal)
                               : parent.width * 0.12
                        Behavior on width { NumberAnimation { duration: Theme.animMedium } }
                    }
                }

                Text {
                    visible: appController.activeSessionId >= 0 && appController.activeFile.length > 0
                    text: appController.activeFile
                    color: Theme.textSecondary
                    font.pixelSize: 11
                    elide: Text.ElideMiddle
                    Layout.fillWidth: true
                }
            }
        }

        SourceStrip {
            id: sourceStrip
            Layout.fillWidth: true
            expandedKey: previewPanel.visible ? previewPanel.sourceKey : ""
            onSourceClicked: (key, label) => {
                if (previewPanel.visible && previewPanel.sourceKey === key) {
                    previewPanel.visible = false
                    return
                }
                previewPanel.openFor(key, label)
                previewPanel.visible = true
            }
        }

        SourcePreviewPanel {
            id: previewPanel
            Layout.fillWidth: true
            Layout.preferredHeight: 320
            visible: false
            onCloseRequested: previewPanel.visible = false
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
                progressPhase: model.progressPhase
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
                text: "Plug in your SD card, camera, or iPhone to get started"
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
