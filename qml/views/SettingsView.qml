import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import PhotoImport
import "../components"

Item {
    id: root

    FolderDialog {
        id: libraryDialog
        title: "Choose your photo library folder"
        onAccepted: {
            appController.setLibraryRoot(libraryDialog.selectedFolder)
            libraryField.text = appController.getSetting("library_root")
        }
    }

    Component.onCompleted: {
        themeGroup.select(appController.getSetting("theme_mode"))
        libraryField.text = appController.getSetting("library_root")
        watchToggle.checked = appController.getSetting("watch_enabled")
        mtpToggle.checked = appController.getSetting("mtp_enabled")
        deleteToggle.checked = appController.getSetting("delete_originals_after_import")
        notifyToggle.checked = appController.getSetting("notify_on_complete")
        embedRawToggle.checked = appController.getSetting("embed_raw_in_dng")
        compressionGroup.select(appController.getSetting("dng_compression"))
    }

    ScrollView {
        anchors.fill: parent
        anchors.margins: 20
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            // Leave room for the Basic style's overlay scrollbar so it
            // doesn't sit on top of right-edge controls (Switch).
            width: parent.width - 16
            spacing: 20

            Text { text: "Settings"; font.pixelSize: 17; font.weight: Font.Bold; color: Theme.textPrimary }

            ColumnLayout {
                spacing: 8
                Layout.fillWidth: true
                Text { text: "Appearance"; color: Theme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }
                RowLayout {
                    id: themeGroup
                    spacing: 8
                    property string selected: "system"
                    function select(mode) {
                        selected = mode
                        Theme.mode = mode
                        appController.setSetting("theme_mode", mode)
                    }
                    Repeater {
                        model: [
                            { key: "light", label: "Light" },
                            { key: "dark", label: "Dark" },
                            { key: "system", label: "Follow system" }
                        ]
                        delegate: HeaderButton {
                            label: modelData.label
                            prominent: themeGroup.selected === modelData.key
                            onClicked: themeGroup.select(modelData.key)
                        }
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.border }

            ColumnLayout {
                spacing: 8
                Layout.fillWidth: true
                Text { text: "Library"; color: Theme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }
                Text {
                    text: "New DNGs are filed as YYYY/YYYY-MM/YYYY-MM-DD/YYYY.MM.DD_Model_NNNNN.dng, matching how the library is already organized."
                    color: Theme.textSecondary
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    TextField {
                        id: libraryField
                        Layout.fillWidth: true
                        onEditingFinished: appController.setSetting("library_root", text)
                    }
                    HeaderButton { label: "Browse…"; onClicked: libraryDialog.open() }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.border }

            ColumnLayout {
                spacing: 10
                Layout.fillWidth: true
                Text { text: "Import"; color: Theme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }

                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "Auto-import when a card or camera is detected"; color: Theme.textPrimary; font.pixelSize: 13; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    Switch { id: watchToggle; onToggled: appController.setSetting("watch_enabled", checked) }
                }
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "Also watch for cameras and iPhones plugged in over USB"; color: Theme.textPrimary; font.pixelSize: 13; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    Switch { id: mtpToggle; onToggled: appController.setSetting("mtp_enabled", checked) }
                }
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "Delete originals from the card after a verified import"; color: Theme.textPrimary; font.pixelSize: 13; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    Switch { id: deleteToggle; onToggled: appController.setSetting("delete_originals_after_import", checked) }
                }
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "Notify when an import finishes"; color: Theme.textPrimary; font.pixelSize: 13; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    Switch { id: notifyToggle; onToggled: appController.setSetting("notify_on_complete", checked) }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.border }

            ColumnLayout {
                spacing: 10
                Layout.fillWidth: true
                Text { text: "DNG conversion"; color: Theme.textSecondary; font.pixelSize: 12; font.weight: Font.DemiBold }

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: appController.dnglabReady ? "Converter ready" : "Converter not installed yet"
                        color: appController.dnglabReady ? Theme.healthFresh : Theme.textSecondary
                        font.pixelSize: 13
                        Layout.fillWidth: true
                    }
                    HeaderButton {
                        visible: !appController.dnglabReady
                        label: "Retry download"
                        onClicked: appController.retryDnglabSetup()
                    }
                }

                RowLayout {
                    spacing: 8
                    id: compressionGroup
                    property string selected: "lossless"
                    function select(mode) {
                        selected = mode
                        appController.setSetting("dng_compression", mode)
                    }
                    Repeater {
                        model: [
                            { key: "lossless", label: "Lossless" },
                            { key: "uncompressed", label: "Uncompressed" }
                        ]
                        delegate: HeaderButton {
                            label: modelData.label
                            prominent: compressionGroup.selected === modelData.key
                            onClicked: compressionGroup.select(modelData.key)
                        }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: "Embed original raw data in the DNG"
                        color: Theme.textPrimary
                        font.pixelSize: 13
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                    }
                    Switch { id: embedRawToggle; onToggled: appController.setSetting("embed_raw_in_dng", checked) }
                }
                Text {
                    text: "Off keeps DNGs roughly half the size of the original ARW (matches Lightroom's default). On preserves the exact original raw bytes inside the DNG, at close to full size."
                    color: Theme.textSecondary
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.border }

            Text {
                text: "Photo Import v" + appController.appVersion
                color: Theme.textSecondary
                font.pixelSize: 11
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
            }
        }
    }
}
