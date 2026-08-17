import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PhotoSpool
import "../components"

Item {
    id: root

    property int sessionId: -1
    property string deviceLabel: ""
    property var files: []

    function openFor(id, label) {
        root.sessionId = id
        root.deviceLabel = label
        root.files = appController.getSessionFiles(id)
    }

    // openFor() is a one-shot snapshot -- if the user reaches this view for
    // a session that's still running (e.g. clicking a "just started"
    // notification), the file list would otherwise never update again
    // until reopened. activeSessionChanged already fires on every progress
    // tick for whichever session is currently active, so piggyback on it
    // rather than adding a new backend signal just for this.
    Connections {
        target: appController
        function onActiveSessionChanged() {
            if (root.sessionId !== -1 && root.sessionId === appController.activeSessionId) {
                root.files = appController.getSessionFiles(root.sessionId)
            }
        }
    }

    function statusColor(status) {
        switch (status) {
        case "imported": return Theme.healthFresh
        case "duplicate": return Theme.textSecondary
        case "failed": return Theme.danger
        default: return Theme.textSecondary
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 10

        Text {
            text: root.deviceLabel
            font.pixelSize: 15
            font.weight: Font.DemiBold
            color: Theme.textPrimary
            Layout.fillWidth: true
            elide: Text.ElideRight
        }

        ListView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 4
            model: root.files
            ScrollBar.vertical: ThemedScrollBar {}

            delegate: Rectangle {
                width: ListView.view.width
                implicitHeight: rowContent.implicitHeight + 12
                radius: Theme.radiusSmall
                color: rowMouse.containsMouse && rowMouse.enabled ? Theme.accentSoft : Theme.chipBackground
                Behavior on color { ColorAnimation { duration: Theme.animFast } }

                MouseArea {
                    id: rowMouse
                    anchors.fill: parent
                    enabled: !!modelData.destPath
                    hoverEnabled: true
                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: appController.openFile(modelData.destPath)

                    ToolTip.visible: enabled && containsMouse
                    ToolTip.delay: 600
                    ToolTip.text: "Open " + (modelData.destPath ? modelData.destPath.split("/").pop() : "")
                }

                RowLayout {
                    id: rowContent
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 8

                    SquircleImage {
                        Layout.preferredWidth: 36
                        Layout.preferredHeight: 36
                        Layout.alignment: Qt.AlignVCenter
                        cornerRadius: Theme.radiusSmall
                        placeholderColor: Theme.surfaceElevated
                        source: modelData.destPath ? "image://thumb/" + encodeURIComponent(modelData.destPath) : ""
                        asynchronous: true
                        fillMode: Image.PreserveAspectCrop
                        sourceSize.width: 72
                        sourceSize.height: 72
                    }

                    Rectangle {
                        width: 7; height: 7; radius: 3.5
                        color: root.statusColor(modelData.status)
                        Layout.alignment: Qt.AlignVCenter
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 1
                        Text {
                            text: modelData.sourceFilename
                            color: Theme.textPrimary
                            font.pixelSize: 12
                            elide: Text.ElideMiddle
                            Layout.fillWidth: true
                        }
                        Text {
                            visible: text.length > 0
                            text: modelData.status === "failed" ? modelData.errorMessage
                                  : (modelData.destPath ? modelData.destPath.split("/").pop() : "")
                            color: modelData.status === "failed" ? Theme.danger : Theme.textSecondary
                            font.pixelSize: 10
                            elide: Text.ElideMiddle
                            Layout.fillWidth: true
                        }
                    }

                    Rectangle {
                        id: revealButton
                        visible: !!modelData.destPath
                        Layout.preferredWidth: 26
                        Layout.preferredHeight: 26
                        Layout.alignment: Qt.AlignVCenter
                        radius: 13
                        color: revealMouse.containsMouse ? Theme.accentSoft : "transparent"
                        Behavior on color { ColorAnimation { duration: Theme.animFast } }

                        Text {
                            anchors.centerIn: parent
                            text: "📁"
                            font.pixelSize: 12
                        }

                        MouseArea {
                            id: revealMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: appController.revealInFileBrowser(modelData.destPath)

                            ToolTip.visible: containsMouse
                            ToolTip.delay: 500
                            ToolTip.text: "Reveal in file browser"
                        }
                    }
                }
            }

            Text {
                anchors.centerIn: parent
                visible: root.files.length === 0
                text: "Nothing new imported this time"
                color: Theme.textSecondary
                font.pixelSize: 12
            }
        }
    }
}
