import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PhotoImport

Rectangle {
    id: root

    property string sourceKey: ""
    property string label: ""
    property var items: []
    property var selected: ({})
    property bool loading: false
    property string errorText: ""

    signal closeRequested()

    readonly property int selectedCount: {
        var n = 0
        for (var key in selected) if (selected[key]) n++
        return n
    }

    function openFor(key, sourceLabel) {
        root.sourceKey = key
        root.label = sourceLabel
        root.items = []
        root.selected = ({})
        root.errorText = ""
        root.loading = true
        appController.requestPreview(key)
    }

    function toggle(filename) {
        var m = Object.assign({}, root.selected)
        m[filename] = !m[filename]
        root.selected = m
    }

    function setAll(value) {
        var m = {}
        for (var i = 0; i < root.items.length; i++) {
            var it = root.items[i]
            m[it.filename] = value
        }
        root.selected = m
    }

    // Records these as already-imported (dedup ledger only, nothing
    // copied/converted) and optimistically greys them out locally so the
    // grid doesn't wait on a re-scan to reflect it.
    function markOwned(filenames) {
        if (filenames.length === 0) return
        appController.markAlreadyImported(root.sourceKey, filenames)
        var nameSet = {}
        for (var i = 0; i < filenames.length; i++) nameSet[filenames[i]] = true
        var newItems = []
        for (var j = 0; j < root.items.length; j++) {
            var it = root.items[j]
            if (nameSet[it.filename]) {
                var copy = Object.assign({}, it)
                copy.alreadyImported = true
                newItems.push(copy)
            } else {
                newItems.push(it)
            }
        }
        root.items = newItems
        var m = Object.assign({}, root.selected)
        for (var k = 0; k < filenames.length; k++) delete m[filenames[k]]
        root.selected = m
    }

    Connections {
        target: appController
        function onPreviewReady(key, receivedItems) {
            if (key !== root.sourceKey) return
            root.loading = false
            root.items = receivedItems
            var m = {}
            for (var i = 0; i < receivedItems.length; i++) {
                var it = receivedItems[i]
                m[it.filename] = !it.alreadyImported
            }
            root.selected = m
        }
        function onPreviewFailed(key, message) {
            if (key !== root.sourceKey) return
            root.loading = false
            root.errorText = message
        }
    }

    radius: Theme.radiusMedium
    color: Theme.surfaceElevated
    border.width: 1
    border.color: Theme.border

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Text {
                text: root.label
                color: Theme.textPrimary
                font.pixelSize: 14
                font.weight: Font.DemiBold
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            Text {
                visible: !root.loading && root.errorText.length === 0
                text: root.selectedCount + " of " + root.items.length + " selected"
                color: Theme.textSecondary
                font.pixelSize: 11
            }
            Text {
                visible: !root.loading && root.items.length > 0
                text: "Select all"
                color: Theme.accent
                font.pixelSize: 11
                MouseArea { anchors.fill: parent; anchors.margins: -4; cursorShape: Qt.PointingHandCursor
                    onClicked: root.setAll(true) }
            }
            Text {
                visible: !root.loading && root.items.length > 0
                text: "Select none"
                color: Theme.accent
                font.pixelSize: 11
                MouseArea { anchors.fill: parent; anchors.margins: -4; cursorShape: Qt.PointingHandCursor
                    onClicked: root.setAll(false) }
            }
            Text {
                visible: !root.loading && root.items.length > 0
                text: "Mark unselected as already imported"
                color: Theme.textSecondary
                font.pixelSize: 11
                MouseArea {
                    anchors.fill: parent; anchors.margins: -4; cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        var names = []
                        for (var i = 0; i < root.items.length; i++) {
                            var it = root.items[i]
                            if (!it.alreadyImported && !root.selected[it.filename]) names.push(it.filename)
                        }
                        root.markOwned(names)
                    }
                }
            }
            HeaderButton {
                label: "Import " + root.selectedCount + " selected"
                prominent: true
                enabled: root.selectedCount > 0
                opacity: enabled ? 1.0 : 0.5
                onClicked: {
                    var names = []
                    for (var key in root.selected) if (root.selected[key]) names.push(key)
                    appController.importSelected(root.sourceKey, names)
                    root.closeRequested()
                }
            }
            HeaderIconButton { icon: "✕"; onClicked: root.closeRequested() }
        }

        Text {
            visible: root.loading
            text: "Scanning for photos…"
            color: Theme.textSecondary
            font.pixelSize: 12
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 20
        }

        Text {
            visible: !root.loading && root.errorText.length > 0
            text: root.errorText
            color: Theme.danger
            font.pixelSize: 12
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 20
        }

        Text {
            visible: !root.loading && root.errorText.length === 0 && root.items.length === 0
            text: "No raw files found here"
            color: Theme.textSecondary
            font.pixelSize: 12
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 20
        }

        GridView {
            id: grid
            visible: !root.loading && root.items.length > 0
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            cellWidth: 132
            cellHeight: 132
            model: root.items

            delegate: Item {
                width: grid.cellWidth
                height: grid.cellHeight

                Item {
                    anchors.fill: parent
                    anchors.margins: 5
                    opacity: modelData.alreadyImported ? 0.4 : 1.0

                    SquircleImage {
                        anchors.fill: parent
                        cornerRadius: Theme.radiusMedium
                        placeholderColor: Theme.chipBackground
                        source: "image://thumb/" + encodeURIComponent(modelData.path)
                        asynchronous: true
                        fillMode: Image.PreserveAspectCrop
                        sourceSize.width: 200
                        sourceSize.height: 200
                    }

                    // Whole-tile toggle -- declared before the small
                    // overlay buttons below so they stack on top of it and
                    // can intercept their own clicks instead of this one.
                    MouseArea {
                        anchors.fill: parent
                        enabled: !modelData.alreadyImported
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.toggle(modelData.filename)
                    }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 5
                        height: nameText.implicitHeight + 6
                        radius: Theme.radiusSmall
                        color: Qt.rgba(0, 0, 0, 0.55)
                        Text {
                            id: nameText
                            anchors.centerIn: parent
                            width: parent.width - 8
                            text: modelData.filename
                            color: "white"
                            font.pixelSize: 9
                            elide: Text.ElideMiddle
                            horizontalAlignment: Text.AlignHCenter
                        }
                    }

                    Rectangle {
                        visible: modelData.alreadyImported
                        anchors.centerIn: parent
                        implicitWidth: importedLabel.implicitWidth + 12
                        implicitHeight: importedLabel.implicitHeight + 6
                        radius: Theme.radiusSmall
                        color: Qt.rgba(0, 0, 0, 0.6)
                        Text {
                            id: importedLabel
                            anchors.centerIn: parent
                            text: "Imported"
                            color: "white"
                            font.pixelSize: 10
                        }
                    }

                    Rectangle {
                        visible: !modelData.alreadyImported
                        width: 20; height: 20; radius: 4
                        anchors.top: parent.top
                        anchors.right: parent.right
                        anchors.margins: 5
                        color: root.selected[modelData.filename] ? Theme.accent : Qt.rgba(0, 0, 0, 0.5)
                        border.width: 1
                        border.color: "white"

                        Text {
                            visible: !!root.selected[modelData.filename]
                            anchors.centerIn: parent
                            text: "✓"
                            color: "white"
                            font.pixelSize: 12
                            font.bold: true
                        }
                    }

                    Rectangle {
                        visible: !modelData.alreadyImported
                        anchors.top: parent.top
                        anchors.left: parent.left
                        anchors.margins: 5
                        implicitWidth: haveItLabel.implicitWidth + 8
                        implicitHeight: 16
                        radius: 4
                        color: haveItMouse.containsMouse ? Theme.accent : Qt.rgba(0, 0, 0, 0.5)

                        Text {
                            id: haveItLabel
                            anchors.centerIn: parent
                            text: "Have it"
                            color: "white"
                            font.pixelSize: 8
                        }

                        MouseArea {
                            id: haveItMouse
                            anchors.fill: parent
                            anchors.margins: -3
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.markOwned([modelData.filename])
                        }
                    }
                }
            }
        }
    }
}
