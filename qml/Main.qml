import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import PhotoSpool
import "views"
import "components"

Window {
    id: window
    // One-time read of the startHidden context property (main.py) --
    // avoids a visible flash-then-hide when launched via the systemd
    // service. Once shown/hidden imperatively (show_window() in main.py,
    // or the tray/close-button handlers below), this initial binding is
    // superseded like any other QML property binding.
    visible: !startHidden
    width: 760
    height: 720
    minimumWidth: 480
    minimumHeight: 420
    title: "Photo Spool"
    color: "transparent"

    onClosing: (close) => {
        // Keep importing in the background instead of quitting -- the
        // tray icon (main.py) is the only way to actually quit.
        close.accepted = false
        window.hide()
    }

    Component.onCompleted: {
        Theme.mode = appController.getSetting("theme_mode")
        Theme.systemPrefersDark = appController.systemPrefersDark
    }

    Connections {
        target: appController
        function onSystemPrefersDarkChanged() { Theme.systemPrefersDark = appController.systemPrefersDark }
        function onNavigateToSession(sessionId, deviceLabel) {
            detailPopup.openForSession(sessionId, deviceLabel)
            window.show()
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
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(parent.width - 44, Theme.contentMaxWidth)
                spacing: 10

                Text {
                    text: "Photo Spool"
                    font.pixelSize: 18
                    font.weight: Font.Bold
                    color: Theme.textPrimary
                }

                Item { Layout.fillWidth: true }

                HeaderIconButton {
                    icon: Theme.isDark ? "moon" : "sun"
                    tooltip: Theme.isDark ? "Switch to light mode" : "Switch to dark mode"
                    onClicked: {
                        Theme.mode = Theme.isDark ? "light" : "dark"
                        appController.setSetting("theme_mode", Theme.mode)
                    }
                }

                HeaderIconButton {
                    icon: "chart"
                    tooltip: "Library stats"
                    onClicked: { statsView.reload(); statsPopup.open() }
                }

                HeaderIconButton {
                    icon: "settings"
                    tooltip: "Settings"
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

        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Theme.animMedium; easing.type: Easing.OutCubic }
            NumberAnimation { property: "scale"; from: 0.96; to: 1; duration: Theme.animMedium; easing.type: Easing.OutCubic }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: Theme.animFast; easing.type: Easing.InCubic }
            NumberAnimation { property: "scale"; from: 1; to: 0.96; duration: Theme.animFast; easing.type: Easing.InCubic }
        }

        SettingsView { anchors.fill: parent }
    }

    Popup {
        id: statsPopup
        anchors.centerIn: parent
        width: Math.min(480, window.width - 60)
        height: Math.min(560, window.height - 60)
        modal: true
        focus: true
        padding: 0
        background: Rectangle { color: Theme.surface; radius: Theme.radiusLarge; border.color: Theme.border; border.width: 1 }

        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Theme.animMedium; easing.type: Easing.OutCubic }
            NumberAnimation { property: "scale"; from: 0.96; to: 1; duration: Theme.animMedium; easing.type: Easing.OutCubic }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: Theme.animFast; easing.type: Easing.InCubic }
            NumberAnimation { property: "scale"; from: 1; to: 0.96; duration: Theme.animFast; easing.type: Easing.InCubic }
        }

        StatsView { id: statsView; anchors.fill: parent }
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

        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Theme.animMedium; easing.type: Easing.OutCubic }
            NumberAnimation { property: "scale"; from: 0.96; to: 1; duration: Theme.animMedium; easing.type: Easing.OutCubic }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: Theme.animFast; easing.type: Easing.InCubic }
            NumberAnimation { property: "scale"; from: 1; to: 0.96; duration: Theme.animFast; easing.type: Easing.InCubic }
        }

        function openForSession(sessionId, deviceLabel) {
            sessionDetail.openFor(sessionId, deviceLabel || "Session")
            detailPopup.open()
        }

        SessionDetailView { id: sessionDetail; anchors.fill: parent }
    }

    Toast { id: toast }
}
