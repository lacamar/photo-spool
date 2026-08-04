import QtQuick
import QtQuick.Layouts
import PhotoImport

Rectangle {
    id: root

    property int sessionId: -1
    property string deviceLabel: ""
    property string kindLabel: ""
    property string startedAt: ""
    property string status: "running"
    property int foundCount: 0
    property int importedCount: 0
    property int duplicateCount: 0
    property int failedCount: 0
    property int bytesSaved: 0
    property string errorMessage: ""
    property bool ejectable: false
    property bool ejected: false
    property string progressPhase: ""
    property int progressDone: 0
    property int progressTotal: 0
    property string progressFile: ""
    property bool hovered: false

    signal opened()
    signal ejectRequested()

    function statusColor() {
        switch (root.status) {
        case "running": return Theme.accent
        case "completed": return root.failedCount > 0 ? Theme.healthDue : Theme.healthFresh
        case "failed": return Theme.danger
        default: return Theme.textSecondary
        }
    }

    function phaseLabel() {
        switch (root.progressPhase) {
        case "scanning": return "Found " + root.progressTotal + " photo" + (root.progressTotal === 1 ? "" : "s")
        case "checking": return "Checking " + root.progressDone + "/" + root.progressTotal
        case "converting": return "Converting " + root.progressDone + "/" + root.progressTotal
        case "placing": return "Filing " + root.progressDone + "/" + root.progressTotal
        default: return "Starting…"
        }
    }

    function statusLabel() {
        switch (root.status) {
        case "running": return root.phaseLabel()
        case "completed": return "Done"
        case "failed": return "Failed"
        case "cancelled": return "Cancelled"
        default: return root.status
        }
    }

    radius: Theme.radiusMedium
    color: Theme.surfaceElevated
    border.width: 1
    border.color: root.hovered ? Qt.lighter(statusColor(), 1.5) : Theme.border
    implicitHeight: content.implicitHeight + 28

    Behavior on border.color { ColorAnimation { duration: Theme.animFast } }

    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onEntered: root.hovered = true
        onExited: root.hovered = false
        onClicked: root.opened()
    }

    ColumnLayout {
        id: content
        anchors.fill: parent
        anchors.margins: 14
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Rectangle {
                width: 9; height: 9; radius: 4.5
                color: root.statusColor()
                Layout.alignment: Qt.AlignVCenter
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                Text {
                    text: root.deviceLabel
                    color: Theme.textPrimary
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Text {
                    text: root.kindLabel + " · " + Qt.formatDateTime(new Date(root.startedAt), "d MMM, HH:mm")
                    color: Theme.textSecondary
                    font.pixelSize: 11
                }
            }

            Chip {
                text: root.statusLabel()
                bg: Qt.rgba(root.statusColor().r, root.statusColor().g, root.statusColor().b, 0.16)
                fg: root.statusColor()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            visible: root.status === "running"
            implicitHeight: 5
            radius: 2.5
            color: Theme.chipBackground

            Rectangle {
                height: parent.height
                radius: parent.radius
                color: Theme.accent
                width: root.progressTotal > 0
                       ? parent.width * Math.min(1, root.progressDone / root.progressTotal)
                       : parent.width * 0.12
                Behavior on width { NumberAnimation { duration: Theme.animMedium } }
            }
        }

        Text {
            visible: root.status === "running" && root.progressFile.length > 0
            text: root.progressFile
            color: Theme.textSecondary
            font.pixelSize: 11
            elide: Text.ElideMiddle
            Layout.fillWidth: true
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            visible: root.status !== "running"

            Text {
                text: {
                    if (root.status === "failed") return root.errorMessage
                    var parts = []
                    if (root.importedCount > 0) parts.push(root.importedCount + " imported")
                    if (root.duplicateCount > 0) parts.push(root.duplicateCount + " already had copies")
                    if (root.failedCount > 0) parts.push(root.failedCount + " failed")
                    return parts.length > 0 ? parts.join(", ") : "Nothing new found"
                }
                color: root.status === "failed" ? Theme.danger : Theme.textSecondary
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Text {
                visible: root.bytesSaved > 0
                text: "−" + (root.bytesSaved / 1e6).toFixed(0) + " MB"
                color: Theme.textSecondary
                font.pixelSize: 11
            }

            Text {
                visible: root.ejectable && !root.ejected
                text: "Eject"
                color: ejectMouse.containsMouse ? Theme.accent : Theme.textSecondary
                font.pixelSize: 11
                font.weight: Font.Medium
                Behavior on color { ColorAnimation { duration: Theme.animFast } }
                MouseArea {
                    id: ejectMouse
                    anchors.fill: parent
                    anchors.margins: -6
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.ejectRequested()
                }
            }

            Text {
                visible: root.ejected
                text: "Ejected"
                color: Theme.textSecondary
                font.pixelSize: 11
            }
        }
    }
}
