pragma Singleton
import QtQuick

// Registered as a singleton (see main.py: qmlRegisterSingletonType). Every
// colour a component uses comes from here as a property binding, so
// switching `mode` (or the system scheme changing) live-rebinds the whole
// UI -- no reload, no restart.
QtObject {
    id: theme

    property string mode: "system" // "light" | "dark" | "system"
    property bool systemPrefersDark: false

    readonly property bool isDark: mode === "dark" || (mode === "system" && systemPrefersDark)

    function toggle() {
        mode = isDark ? "light" : "dark"
    }

    // Warm, muted palette in the spirit of Claude's brand: cream/paper
    // light mode, warm charcoal (not blue-black) dark mode, terracotta accent.
    readonly property color background: isDark ? "#262624" : "#F5F4ED"
    readonly property color backgroundGlass: isDark ? Qt.rgba(0.149, 0.149, 0.141, 0.72) : Qt.rgba(0.984, 0.980, 0.965, 0.72)
    readonly property color backgroundGlassFallback: isDark ? Qt.rgba(0.149, 0.149, 0.141, 0.95) : Qt.rgba(0.965, 0.961, 0.945, 0.97)
    readonly property color surface: isDark ? "#30302E" : "#FFFFFF"
    readonly property color surfaceElevated: isDark ? "#3A3936" : "#FFFFFF"
    readonly property color border: isDark ? "#43413C" : "#E5E3D8"
    readonly property color textPrimary: isDark ? "#F5F4ED" : "#1F1E1D"
    readonly property color textSecondary: isDark ? "#A8A69C" : "#87867F"
    readonly property color textOnAccent: "#ffffff"

    readonly property color accent: isDark ? "#E0815F" : "#D97757"
    readonly property color accentSoft: isDark ? Qt.rgba(0.878, 0.506, 0.373, 0.20) : Qt.rgba(0.851, 0.467, 0.341, 0.13)
    readonly property color chipBackground: isDark ? "#3A3936" : "#EEEDE2"
    readonly property color danger: isDark ? "#E58A76" : "#C0392B"
    readonly property color dangerSoft: isDark ? Qt.rgba(0.898, 0.541, 0.463, 0.18) : Qt.rgba(0.753, 0.224, 0.169, 0.10)
    readonly property color priority: isDark ? "#E0B84A" : "#C89B2C"

    readonly property color healthFresh: "#7A9070"
    readonly property color healthDue: "#D4A72C"
    readonly property color healthOverdue: "#C0533A"
    readonly property color healthDormant: isDark ? "#948F86" : "#9C9890"

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
    readonly property int radiusMedium: 14
    readonly property int radiusLarge: 20

    readonly property int animFast: 120
    readonly property int animMedium: 220
}
