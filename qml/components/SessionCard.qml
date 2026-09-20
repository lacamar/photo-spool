import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PhotoSpool

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
    property real bytesSaved: 0
    property string errorMessage: ""
    property string progressPhase: ""
    property int progressDone: 0
    property int progressTotal: 0
    property bool hovered: cardMouse.containsMouse || clearMouse.containsMouse

    // Finished without importing or failing anything: one muted line
    readonly property bool compact: status === "completed" && importedCount === 0 && failedCount === 0

    signal opened()
    signal clearRequested()

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
        case "completed": return root.failedCount > 0 ? "Partial" : ""
        case "failed": return "Failed"
        case "cancelled": return "Cancelled"
        default: return root.status
        }
    }

    function skippedLabel() {
        return root.duplicateCount + " duplicate" + (root.duplicateCount === 1 ? "" : "s") + " skipped"
    }

    function formatBytes(n) {
        return n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : (n / 1e6).toFixed(0) + " MB"
    }

    radius: compact ? Theme.radiusSmall : Theme.radiusMedium
    color: compact ? (root.hovered ? Theme.surfaceElevated : "transparent") : Theme.surfaceElevated
    border.width: 1
    border.color: root.hovered ? Qt.lighter(statusColor(), 1.5) : Theme.border
    implicitHeight: compact ? 34 : content.implicitHeight + 24

    Behavior on border.color { ColorAnimation { duration: Theme.animFast } }
    Behavior on color { ColorAnimation { duration: Theme.animFast } }

    MouseArea {
        id: cardMouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.opened()

        ToolTip.visible: containsMouse
        ToolTip.delay: 700
        ToolTip.text: "View files from this import"
    }

    RowLayout {
        id: content
        anchors.fill: parent
        anchors.leftMargin: 14
        anchors.rightMargin: 10
        spacing: 10

        Rectangle {
            width: root.compact ? 7 : 9
            height: width
            radius: width / 2
            color: root.statusColor()
            opacity: root.compact ? 0.6 : 1
            Layout.alignment: Qt.AlignVCenter
        }

        Text {
            visible: root.compact
            text: root.deviceLabel
            color: Theme.textSecondary
            font.pixelSize: 12
            font.weight: Font.Medium
            elide: Text.ElideRight
            Layout.maximumWidth: 220
        }

        Text {
            visible: root.compact
            text: "Nothing new" + (root.duplicateCount > 0 ? " · " + root.skippedLabel() : "")
            color: Theme.textSecondary
            font.pixelSize: 12
            elide: Text.ElideRight
            Layout.fillWidth: true
        }

        ColumnLayout {
            visible: !root.compact
            Layout.fillWidth: true
            spacing: 3

            Text {
                text: root.deviceLabel
                color: Theme.textPrimary
                font.pixelSize: 14
                font.weight: Font.DemiBold
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            Text {
                visible: root.status === "running"
                text: root.kindLabel
                color: Theme.textSecondary
                font.pixelSize: 12
            }

            Text {
                visible: root.status !== "running"
                textFormat: Text.StyledText
                text: {
                    if (root.status === "failed") return root.errorMessage
                    var parts = []
                    if (root.importedCount > 0)
                        parts.push("<font color=\"" + Theme.textPrimary + "\">" + root.importedCount + " imported</font>")
                    if (root.failedCount > 0)
                        parts.push("<font color=\"" + Theme.danger + "\">" + root.failedCount + " failed</font>")
                    if (root.duplicateCount > 0) parts.push(root.skippedLabel())
                    if (root.bytesSaved >= 1e6) parts.push(root.formatBytes(root.bytesSaved) + " saved")
                    return parts.length > 0 ? parts.join(" · ") : "Nothing new"
                }
                color: root.status === "failed" ? Theme.danger : Theme.textSecondary
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }

        Chip {
            visible: root.statusLabel().length > 0
            text: root.statusLabel()
            bg: Qt.rgba(root.statusColor().r, root.statusColor().g, root.statusColor().b, 0.16)
            fg: root.statusColor()
        }

        Text {
            text: Qt.formatDateTime(new Date(root.startedAt), "HH:mm")
            color: Theme.textSecondary
            font.pixelSize: 11
        }

        Rectangle {
            Layout.preferredWidth: 22
            Layout.preferredHeight: 22
            Layout.alignment: Qt.AlignVCenter
            radius: 11
            opacity: root.status !== "running" && root.hovered ? 1 : 0
            color: clearMouse.containsMouse ? Theme.accentSoft : "transparent"
            Behavior on color { ColorAnimation { duration: Theme.animFast } }
            Behavior on opacity { NumberAnimation { duration: Theme.animFast } }

            Icon {
                anchors.centerIn: parent
                name: "close"
                size: 11
                color: Theme.textSecondary
            }

            MouseArea {
                id: clearMouse
                anchors.fill: parent
                enabled: root.status !== "running"
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.clearRequested()

                ToolTip.visible: containsMouse
                ToolTip.delay: 500
                ToolTip.text: "Remove this entry from history"
            }
        }
    }
}
