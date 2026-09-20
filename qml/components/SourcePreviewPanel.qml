import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PhotoSpool

Rectangle {
    id: root

    property string sourceKey: ""
    property string label: ""
    property var selected: ({})
    property bool loading: false
    property string errorText: ""

    signal closeRequested()

    readonly property int selectedCount: {
        var n = 0
        for (var key in selected) if (selected[key]) n++
        return n
    }

    // A real ListModel instead of a plain `var` array: mark/unmark need
    // to flip one row's alreadyImported flag without resetting the
    // other ~700 -- a plain array has no granular change notification,
    // so any reassignment (even of an otherwise-identical array) is a
    // full GridView model reset, destroying and recreating every
    // delegate (Image included). Confirmed live as a visible whole-grid
    // flash on every mark/unmark click. ListModel.setProperty() updates
    // just the one row that actually changed.
    ListModel {
        id: itemsModel
    }

    function openFor(key, sourceLabel) {
        // Reopening the same source keeps showing its previous scan's
        // items (thumbnails and all -- already on disk, see
        // ThumbnailImageProvider's cache) while a fresh scan runs in the
        // background, instead of clearing to blank first. That "blank,
        // then repopulate" flash on every single reopen made the grid
        // look like it forgot everything it had already loaded, even
        // though the underlying thumbnails were genuinely still cached.
        // Switching to a genuinely different source has nothing to carry
        // over, so it still clears immediately.
        if (root.sourceKey !== key) {
            itemsModel.clear()
        }
        root.sourceKey = key
        root.label = sourceLabel
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
        for (var i = 0; i < itemsModel.count; i++) {
            m[itemsModel.get(i).filename] = value
        }
        root.selected = m
    }

    // Records these as already-imported (dedup ledger only, nothing
    // copied/converted) and optimistically greys them out locally so the
    // grid doesn't wait on a re-scan to reflect it. Updates only the
    // matching rows in place (ListModel.setProperty) instead of
    // rebuilding the whole model -- confirmed live that reassigning the
    // old plain-array `items` property (even just to flip one flag)
    // caused a visible whole-grid flash, destroying and recreating every
    // delegate (Image included), and reset GridView's scroll position to
    // the top. A row update touches only that row.
    function markOwned(filenames) {
        if (filenames.length === 0) return
        appController.markAlreadyImported(root.sourceKey, filenames)
        var nameSet = {}
        for (var i = 0; i < filenames.length; i++) nameSet[filenames[i]] = true
        for (var j = 0; j < itemsModel.count; j++) {
            if (nameSet[itemsModel.get(j).filename]) {
                itemsModel.setProperty(j, "alreadyImported", true)
            }
        }
        var m = Object.assign({}, root.selected)
        for (var k = 0; k < filenames.length; k++) delete m[filenames[k]]
        root.selected = m
    }

    // Reverses markOwned -- forgets the dedup-ledger entry so these show
    // as new again on the next scan, and optimistically un-greys them
    // locally (preselected, same as any other new file) rather than
    // waiting on a re-scan to reflect it. Takes paths (matched against
    // model.path), not filenames: appController.unmarkImported reads
    // metadata straight from these exact paths instead of re-scanning the
    // whole source directory to re-derive them, which would (and, before
    // this, did) freeze the UI for however long that scan takes.
    function unmarkOwned(paths) {
        if (paths.length === 0) return
        appController.unmarkImported(root.sourceKey, paths)
        var pathSet = {}
        for (var i = 0; i < paths.length; i++) pathSet[paths[i]] = true
        var m = Object.assign({}, root.selected)
        for (var j = 0; j < itemsModel.count; j++) {
            var it = itemsModel.get(j)
            if (pathSet[it.path]) {
                itemsModel.setProperty(j, "alreadyImported", false)
                m[it.filename] = true
            }
        }
        root.selected = m
    }

    Connections {
        target: appController
        function onPreviewReady(key, receivedItems) {
            if (key !== root.sourceKey) return
            root.loading = false
            itemsModel.clear()
            var m = {}
            for (var i = 0; i < receivedItems.length; i++) {
                var it = receivedItems[i]
                itemsModel.append(it)
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
                visible: root.errorText.length === 0 && itemsModel.count > 0
                text: root.selectedCount + " of " + itemsModel.count + " selected"
                color: Theme.textSecondary
                font.pixelSize: 11
            }
            Text {
                visible: itemsModel.count > 0
                text: "Select all"
                color: Theme.accent
                font.pixelSize: 11
                MouseArea { anchors.fill: parent; anchors.margins: -4; cursorShape: Qt.PointingHandCursor
                    onClicked: root.setAll(true)
                    hoverEnabled: true
                    ToolTip.visible: containsMouse
                    ToolTip.delay: 500
                    ToolTip.text: "Select every new photo shown below" }
            }
            Text {
                visible: itemsModel.count > 0
                text: "Select none"
                color: Theme.accent
                font.pixelSize: 11
                MouseArea { anchors.fill: parent; anchors.margins: -4; cursorShape: Qt.PointingHandCursor
                    onClicked: root.setAll(false)
                    hoverEnabled: true
                    ToolTip.visible: containsMouse
                    ToolTip.delay: 500
                    ToolTip.text: "Clear the current selection" }
            }
            Text {
                // Same selection the Import button below uses -- check the
                // ones you already have, then pick which of the two
                // actions applies to that selection, instead of the old
                // inverted "mark whatever's left unchecked" flow.
                visible: itemsModel.count > 0
                text: "Mark selected as already imported"
                color: Theme.textSecondary
                opacity: root.selectedCount > 0 ? 1.0 : 0.5
                font.pixelSize: 11
                MouseArea {
                    anchors.fill: parent; anchors.margins: -4; cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        var names = []
                        for (var key in root.selected) if (root.selected[key]) names.push(key)
                        root.markOwned(names)
                    }
                    hoverEnabled: true
                    ToolTip.visible: containsMouse
                    ToolTip.delay: 500
                    ToolTip.text: "Record the selected photos as already in your library, without copying them"
                }
            }
            HeaderButton {
                label: "Import " + root.selectedCount + " selected"
                prominent: true
                enabled: root.selectedCount > 0
                opacity: enabled ? 1.0 : 0.5
                tooltip: "Convert and copy the selected photos into your library"
                onClicked: {
                    var names = []
                    for (var key in root.selected) if (root.selected[key]) names.push(key)
                    appController.importSelected(root.sourceKey, names)
                    root.closeRequested()
                }
            }
            HeaderIconButton { icon: "close"; tooltip: "Close preview"; onClicked: root.closeRequested() }
        }

        Text {
            // Full-height "Scanning..." only for the true first-load
            // blank state; reopening a source that already has items
            // shown gets a much quieter inline hint instead (see below),
            // and the grid stays up throughout.
            visible: root.loading && itemsModel.count === 0
            text: "Scanning for photos…"
            color: Theme.textSecondary
            font.pixelSize: 12
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 20
        }

        Text {
            visible: root.loading && itemsModel.count > 0
            text: "Refreshing…"
            color: Theme.textSecondary
            font.pixelSize: 10
            Layout.alignment: Qt.AlignHCenter
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
            visible: !root.loading && root.errorText.length === 0 && itemsModel.count === 0
            text: "No raw files found here"
            color: Theme.textSecondary
            font.pixelSize: 12
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: 20
        }

        GridView {
            id: grid
            visible: itemsModel.count > 0
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            cellWidth: 132
            cellHeight: 132
            model: itemsModel
            ScrollBar.vertical: ThemedScrollBar {}
            // Without these, GridView destroys and recreates every
            // delegate (including its Image, discarding the already-
            // decoded pixmap) the instant it scrolls out of view, then
            // re-requests and re-decodes it the instant it scrolls back
            // in -- the "thumbnails load and unload while scrolling"
            // flicker. reuseItems keeps delegates alive and rebinds them
            // to new model data instead of tearing down/rebuilding;
            // cacheBuffer additionally keeps a margin of off-screen rows
            // warm so fast scrolling doesn't even hit that path.
            reuseItems: true
            cacheBuffer: 600

            delegate: Item {
                width: grid.cellWidth
                height: grid.cellHeight

                Item {
                    anchors.fill: parent
                    anchors.margins: 5
                    opacity: model.alreadyImported ? 0.4 : 1.0

                    SquircleImage {
                        anchors.fill: parent
                        cornerRadius: Theme.radiusMedium
                        placeholderColor: Theme.chipBackground
                        source: "image://thumb/" + encodeURIComponent(model.path)
                        asynchronous: true
                        fillMode: Image.PreserveAspectCrop
                        sourceSize.width: 200
                        sourceSize.height: 200
                    }

                    // Whole-tile toggle -- declared before the small
                    // overlay buttons below so they stack on top of it and
                    // can intercept their own clicks instead of this one.
                    MouseArea {
                        id: tileToggleMouse
                        anchors.fill: parent
                        enabled: !model.alreadyImported
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.toggle(model.filename)

                        ToolTip.visible: containsMouse
                        ToolTip.delay: 700
                        ToolTip.text: (root.selected[model.filename] ? "Deselect " : "Select ") + model.filename
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
                            text: model.filename
                            color: "white"
                            font.pixelSize: 9
                            elide: Text.ElideMiddle
                            horizontalAlignment: Text.AlignHCenter
                        }
                    }

                    Rectangle {
                        visible: model.alreadyImported
                        anchors.centerIn: parent
                        implicitWidth: importedLabel.implicitWidth + 12
                        implicitHeight: importedLabel.implicitHeight + 6
                        radius: Theme.radiusSmall
                        color: importedMouse.containsMouse ? Theme.accent : Qt.rgba(0, 0, 0, 0.6)
                        Behavior on color { ColorAnimation { duration: Theme.animFast } }
                        Text {
                            id: importedLabel
                            anchors.centerIn: parent
                            text: importedMouse.containsMouse ? "Unmark" : "Imported"
                            color: "white"
                            font.pixelSize: 10
                        }
                        MouseArea {
                            id: importedMouse
                            anchors.fill: parent
                            anchors.margins: -3
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.unmarkOwned([model.path])

                            ToolTip.visible: containsMouse
                            ToolTip.delay: 500
                            ToolTip.text: "Already marked as imported — click to undo"
                        }
                    }

                    Rectangle {
                        visible: !model.alreadyImported
                        width: 20; height: 20; radius: 4
                        anchors.top: parent.top
                        anchors.right: parent.right
                        anchors.margins: 5
                        color: root.selected[model.filename] ? Theme.accent : Qt.rgba(0, 0, 0, 0.5)
                        border.width: 1
                        border.color: "white"

                        Icon {
                            visible: !!root.selected[model.filename]
                            anchors.centerIn: parent
                            name: "check"
                            color: "white"
                            size: 13
                            strokeWidth: 3
                        }
                    }

                    Rectangle {
                        visible: !model.alreadyImported
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
                            onClicked: root.markOwned([model.filename])

                            ToolTip.visible: containsMouse
                            ToolTip.delay: 500
                            ToolTip.text: "Mark as already imported without copying it"
                        }
                    }
                }
            }
        }
    }
}
