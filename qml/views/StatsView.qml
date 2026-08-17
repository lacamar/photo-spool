import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PhotoSpool

Item {
    id: root
    property var stats: null

    function reload() { root.stats = appController.getLibraryStats() }
    Component.onCompleted: reload()

    function fmtBytes(n) {
        if (!n) return "0 MB"
        var gb = n / 1e9
        return gb >= 1 ? gb.toFixed(1) + " GB" : (n / 1e6).toFixed(0) + " MB"
    }

    function fmtDate(iso) {
        return iso ? Qt.formatDate(new Date(iso), "d MMM yyyy") : "—"
    }

    ScrollView {
        anchors.fill: parent
        anchors.margins: 20
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            width: parent.width - 16
            spacing: 14

            RowLayout {
                Layout.fillWidth: true
                Text {
                    text: "Library stats"
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                    color: Theme.textPrimary
                    Layout.fillWidth: true
                }
                Text {
                    text: "↻"
                    color: Theme.accent
                    font.pixelSize: 13
                    MouseArea {
                        anchors.fill: parent; anchors.margins: -6; cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: root.reload()
                        ToolTip.visible: containsMouse
                        ToolTip.delay: 500
                        ToolTip.text: "Refresh stats"
                    }
                }
            }

            Text {
                visible: root.stats !== null && root.stats.totalCount === 0
                text: "Nothing imported yet."
                color: Theme.textSecondary
                font.pixelSize: 12
            }

            GridLayout {
                Layout.fillWidth: true
                visible: root.stats !== null && root.stats.totalCount > 0
                columns: 2
                columnSpacing: 10
                rowSpacing: 10

                Repeater {
                    model: root.stats ? [
                        { big: String(root.stats.totalCount), small: "files in your library" },
                        { big: root.fmtBytes(root.stats.totalBytes), small: "on disk (−" + root.fmtBytes(root.stats.bytesSaved) + " vs. originals)" },
                        { big: root.stats.photoCount + " photos, " + root.stats.videoCount + " videos",
                          small: root.stats.markedOnlyCount > 0 ? ("+" + root.stats.markedOnlyCount + " marked as already had") : "by type" },
                        { big: root.stats.earliestCapturedAt ? root.fmtDate(root.stats.earliestCapturedAt) + " – " + root.fmtDate(root.stats.latestCapturedAt) : "—",
                          small: "date range" },
                    ] : []

                    delegate: Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        radius: Theme.radiusMedium
                        color: Theme.surfaceElevated
                        implicitHeight: tileContent.implicitHeight + 20

                        ColumnLayout {
                            id: tileContent
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 2
                            Text {
                                text: modelData.big
                                font.pixelSize: 16
                                font.weight: Font.Bold
                                color: Theme.textPrimary
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }
                            Text {
                                text: modelData.small
                                font.pixelSize: 11
                                color: Theme.textSecondary
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            Text {
                visible: root.stats && root.stats.byModel.length > 0
                text: "By camera"
                font.pixelSize: 12
                font.weight: Font.DemiBold
                color: Theme.textPrimary
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 8
                visible: root.stats && root.stats.byModel.length > 0

                Repeater {
                    model: root.stats ? root.stats.byModel : []

                    delegate: ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 3
                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                text: modelData.model
                                color: Theme.textPrimary
                                font.pixelSize: 12
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                            }
                            Text {
                                text: modelData.count + " · " + root.fmtBytes(modelData.bytes)
                                color: Theme.textSecondary
                                font.pixelSize: 11
                            }
                        }
                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: 5
                            radius: 2.5
                            color: Theme.chipBackground
                            Rectangle {
                                height: parent.height
                                radius: parent.radius
                                color: Theme.accent
                                width: root.stats && root.stats.totalCount > 0
                                       ? parent.width * Math.min(1, modelData.count / root.stats.totalCount)
                                       : 0
                            }
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: 4 }
        }
    }
}
