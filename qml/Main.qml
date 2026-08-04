import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import PhotoImport
import "views"
import "components"

Window {
    id: window
    visible: true
    width: 760
    height: 720
    minimumWidth: 480
    minimumHeight: 420
    title: "Photo Import"
    color: "transparent"

    Component.onCompleted: {
        Theme.mode = appController.getSetting("theme_mode")
        Theme.systemPrefersDark = appController.systemPrefersDark
    }

    Connections {
        target: appController
        function onSystemPrefersDarkChanged() { Theme.systemPrefersDark = appController.systemPrefersDark }
        function onNavigateToSession(sessionId, deviceLabel) {
            detailPopup.openForSession(sessionId, deviceLabel)
            window.raise()
            window.requestActivate()
        }
        function onToast(message) { toast.show(message) }
    }

    Rectangle {
        anchors.fill: parent
        color: appController.blurSupported ? Theme.backgroundGlass : Theme.backgroundGlassFallback
        Behavior on color { ColorAnimation { duration: Theme.animMedium } }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 64
            color: "transparent"

            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 1
                color: Theme.border
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 22
                anchors.rightMargin: 22
                spacing: 10

                Text {
                    text: "Photo Import"
                    font.pixelSize: 18
                    font.weight: Font.Bold
                    color: Theme.textPrimary
                }

                Item { Layout.fillWidth: true }

                HeaderIconButton {
                    icon: "🔔"
                    badgeCount: appController.unreadCount
                    onClicked: notifPopup.open()
                }

                HeaderIconButton {
                    icon: Theme.isDark ? "🌙" : "☀️"
                    onClicked: {
                        Theme.mode = Theme.isDark ? "light" : "dark"
                        appController.setSetting("theme_mode", Theme.mode)
                    }
                }

                HeaderIconButton {
                    icon: "⚙️"
                    onClicked: settingsPopup.open()
                }
            }
        }

        ImportView {
            id: importView
            Layout.fillWidth: true
            Layout.fillHeight: true
            onSessionOpened: (sessionId, deviceLabel) => detailPopup.openForSession(sessionId, deviceLabel)
        }
    }

    Popup {
        id: settingsPopup
        anchors.centerIn: parent
        width: Math.min(480, window.width - 60)
        height: Math.min(560, window.height - 60)
        modal: true
        focus: true
        padding: 0
        background: Rectangle { color: Theme.surface; radius: Theme.radiusLarge; border.color: Theme.border; border.width: 1 }

        SettingsView { anchors.fill: parent }
    }

    Popup {
        id: notifPopup
        x: window.width - width - 22
        y: 60
        width: Math.min(380, window.width - 40)
        height: Math.min(480, window.height - 100)
        modal: false
        focus: true
        padding: 0
        background: Rectangle { color: Theme.surface; radius: Theme.radiusLarge; border.color: Theme.border; border.width: 1 }

        NotificationHistoryView {
            anchors.fill: parent
            onSessionSelected: (sessionId) => { detailPopup.openForSession(sessionId); notifPopup.close() }
        }
    }

    Popup {
        id: detailPopup
        anchors.centerIn: parent
        width: Math.min(460, window.width - 60)
        height: Math.min(520, window.height - 60)
        modal: true
        focus: true
        padding: 0
        background: Rectangle { color: Theme.surface; radius: Theme.radiusLarge; border.color: Theme.border; border.width: 1 }

        function openForSession(sessionId, deviceLabel) {
            sessionDetail.openFor(sessionId, deviceLabel || "Session")
            detailPopup.open()
        }

        SessionDetailView { id: sessionDetail; anchors.fill: parent }
    }

    Toast { id: toast }
}
