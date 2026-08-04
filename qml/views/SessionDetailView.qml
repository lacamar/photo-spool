import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PhotoImport
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

            delegate: Rectangle {
                width: ListView.view.width
                implicitHeight: rowContent.implicitHeight + 12
                radius: Theme.radiusSmall
                color: Theme.chipBackground

                RowLayout {
                    id: rowContent
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 8

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
                }
            }

            Text {
                anchors.centerIn: parent
                visible: root.files.length === 0
                text: "No file details for this session"
                color: Theme.textSecondary
                font.pixelSize: 12
            }
        }
    }
}
