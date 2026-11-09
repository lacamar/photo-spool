pragma Singleton
import QtQuick

// QML module singleton (build.rs). Every
// colour a component uses comes from here as a property binding, so
// switching `mode` (or the system scheme changing) live-rebinds the whole
// UI -- no reload, no restart.
QtObject {
    id: theme

    property string mode: "system" // "light" | "dark" | "system"
    property bool systemPrefersDark: false
    // Portal accent-color and theme.json overrides (src/desktop.rs, src/qt/controller.rs);
    // same palette and keys as tidings.
    property string systemAccent: ""
    property var custom: ({})

    readonly property bool isDark: mode === "dark" || (mode === "system" && systemPrefersDark)

    function toggle() {
        mode = isDark ? "light" : "dark"
    }

    function tint(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

    readonly property var base: isDark
        ? { bg: "#1c1c1f", side: "#151517", raised: "#2a2a2e", fg: "#f2f2f4", sub: "#9d9da6", line: "#2b2b30" }
        : { bg: "#ffffff", side: "#f4f4f6", raised: "#ffffff", fg: "#1b1b20", sub: "#6c6c76", line: "#e6e6ea" }
    // theme.json is generated for the system scheme, so a manual override ignores it
    function pick(key, fallback) {
        return mode === "system" && custom[key] !== undefined ? custom[key] : fallback
    }

    readonly property real opacity: pick("opacity", 0.7)
    readonly property color background: pick("side", base.side)
    readonly property color backgroundGlass: tint(background, opacity)
    readonly property color surface: pick("raised", base.raised)
    readonly property color surfaceElevated: surface
    readonly property color border: pick("line", base.line)
    readonly property color textPrimary: pick("fg", base.fg)
    readonly property color textSecondary: pick("sub", base.sub)
    readonly property color textOnAccent: "white"

    readonly property color accent: pick("accent", systemAccent || "#3584e4")
    readonly property color accentSoft: tint(accent, isDark ? 0.24 : 0.13)
    readonly property color hover: tint(textPrimary, 0.05)
    readonly property color chipBackground: tint(textPrimary, 0.08)
    readonly property color danger: isDark ? "#ff7b63" : "#e01b24"
    readonly property color dangerSoft: tint(danger, 0.12)
    readonly property color priority: "#e5a50a"

    readonly property color healthFresh: "#26a269"
    readonly property color healthDue: priority
    readonly property color healthOverdue: danger
    readonly property color healthDormant: textSecondary

    function healthColor(status) {
        switch (status) {
        case "fresh": return healthFresh
        case "due": return healthDue
        case "overdue": return healthOverdue
        case "dormant": return healthDormant
        default: return textSecondary
        }
    }

    readonly property int radiusSmall: 8
    readonly property int radiusMedium: 12
    readonly property int radiusLarge: 16

    readonly property int contentMaxWidth: 1100

    readonly property int animFast: 120
    readonly property int animMedium: 220
}
