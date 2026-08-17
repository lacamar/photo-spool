import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PhotoSpool
import "../components"

Item {
    id: root
    signal sessionSelected(int sessionId)

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            Text {
                text: "Notifications"
                font.pixelSize: 15
                font.weight: Font.DemiBold
                color: Theme.textPrimary
                Layout.fillWidth: true
            }
            HeaderButton { label: "Mark all read"; tooltip: "Mark every notification as read"; onClicked: appController.markAllNotificationsRead() }
        }

        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 6
            model: notificationModel

            add: Transition {
                NumberAnimation { properties: "opacity"; from: 0; to: 1; duration: Theme.animMedium }
                NumberAnimation { properties: "y"; from: -16; duration: Theme.animMedium; easing.type: Easing.OutCubic }
            }
            displaced: Transition {
                NumberAnimation { properties: "x,y"; duration: Theme.animMedium; easing.type: Easing.OutCubic }
            }

            delegate: Rectangle {
                width: list.width
                implicitHeight: content.implicitHeight + 16
                radius: Theme.radiusSmall
                color: rowMouse.containsMouse ? Theme.chipBackground : (model.read ? "transparent" : Theme.accentSoft)

                Behavior on color { ColorAnimation { duration: Theme.animFast } }

                ColumnLayout {
                    id: content
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 2
                    Text {
                        text: model.text
                        color: Theme.textPrimary
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    Text {
                        text: Qt.formatDateTime(new Date(model.createdAt), "d MMM, HH:mm")
                        color: Theme.textSecondary
                        font.pixelSize: 10
                    }
                }
                MouseArea {
                    id: rowMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        // Capture before the call below -- markNotificationRead
                        // triggers a synchronous model reset that can destroy
                        // this delegate's `model` context before the next line runs.
                        var targetSessionId = model.sessionId
                        appController.markNotificationRead(model.notificationId)
                        if (targetSessionId >= 0) root.sessionSelected(targetSessionId)
                    }

                    ToolTip.visible: containsMouse
                    ToolTip.delay: 600
                    ToolTip.text: (model.read ? "Read" : "Unread") + (model.sessionId >= 0 ? " — click to view that import" : " — click to mark as read")
                }
            }

            Text {
                anchors.centerIn: parent
                visible: list.count === 0
                text: "No notifications yet"
                color: Theme.textSecondary
                font.pixelSize: 12
            }
        }

        HeaderButton {
            label: "Clear all"
            Layout.alignment: Qt.AlignRight
            tooltip: "Delete all notifications"
            onClicked: appController.clearNotifications()
        }
    }
}
