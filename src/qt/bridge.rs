#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qvariant.h");
        type QVariant = cxx_qt_lib::QVariant;
        include!("cxx-qt-lib/qlist.h");
        type QList_QVariant = cxx_qt_lib::QList<cxx_qt_lib::QVariant>;
        include!("cxx-qt-lib/qhash.h");
        type QHash_i32_QByteArray = cxx_qt_lib::QHash<cxx_qt_lib::QHashPair_i32_QByteArray>;
        include!("cxx-qt-lib/qmodelindex.h");
        type QModelIndex = cxx_qt_lib::QModelIndex;
        include!("cxx-qt-lib/qvector.h");
        type QVector_i32 = cxx_qt_lib::QVector<i32>;
    }

    unsafe extern "C++Qt" {
        include!(<QtCore/QAbstractListModel>);
        #[qobject]
        type QAbstractListModel;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[base = QAbstractListModel]
        type SessionListModel = super::SessionRows;

        #[qobject]
        #[qml_element]
        #[base = QAbstractListModel]
        type SourceListModel = super::SourceRows;
    }

    // QAbstractListModel plumbing, identical for both models
    unsafe extern "RustQt" {
        #[inherit]
        #[qsignal]
        #[cxx_name = "dataChanged"]
        fn data_changed(
            self: Pin<&mut SessionListModel>,
            top_left: &QModelIndex,
            bottom_right: &QModelIndex,
            roles: &QVector_i32,
        );
        #[inherit]
        #[cxx_name = "beginInsertRows"]
        fn begin_insert_rows(
            self: Pin<&mut SessionListModel>,
            parent: &QModelIndex,
            first: i32,
            last: i32,
        );
        #[inherit]
        #[cxx_name = "endInsertRows"]
        fn end_insert_rows(self: Pin<&mut SessionListModel>);
        #[inherit]
        #[cxx_name = "beginRemoveRows"]
        fn begin_remove_rows(
            self: Pin<&mut SessionListModel>,
            parent: &QModelIndex,
            first: i32,
            last: i32,
        );
        #[inherit]
        #[cxx_name = "endRemoveRows"]
        fn end_remove_rows(self: Pin<&mut SessionListModel>);
        #[inherit]
        #[cxx_name = "beginResetModel"]
        fn begin_reset_model(self: Pin<&mut SessionListModel>);
        #[inherit]
        #[cxx_name = "endResetModel"]
        fn end_reset_model(self: Pin<&mut SessionListModel>);
        #[inherit]
        fn index(
            self: &SessionListModel,
            row: i32,
            column: i32,
            parent: &QModelIndex,
        ) -> QModelIndex;

        #[inherit]
        #[qsignal]
        #[cxx_name = "dataChanged"]
        fn data_changed(
            self: Pin<&mut SourceListModel>,
            top_left: &QModelIndex,
            bottom_right: &QModelIndex,
            roles: &QVector_i32,
        );
        #[inherit]
        #[cxx_name = "beginInsertRows"]
        fn begin_insert_rows(
            self: Pin<&mut SourceListModel>,
            parent: &QModelIndex,
            first: i32,
            last: i32,
        );
        #[inherit]
        #[cxx_name = "endInsertRows"]
        fn end_insert_rows(self: Pin<&mut SourceListModel>);
        #[inherit]
        #[cxx_name = "beginRemoveRows"]
        fn begin_remove_rows(
            self: Pin<&mut SourceListModel>,
            parent: &QModelIndex,
            first: i32,
            last: i32,
        );
        #[inherit]
        #[cxx_name = "endRemoveRows"]
        fn end_remove_rows(self: Pin<&mut SourceListModel>);
        #[inherit]
        fn index(
            self: &SourceListModel,
            row: i32,
            column: i32,
            parent: &QModelIndex,
        ) -> QModelIndex;
    }

    extern "RustQt" {
        #[qinvokable]
        #[cxx_override]
        fn data(self: &SessionListModel, index: &QModelIndex, role: i32) -> QVariant;
        #[qinvokable]
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &SessionListModel) -> QHash_i32_QByteArray;
        #[qinvokable]
        #[cxx_override]
        #[cxx_name = "rowCount"]
        fn row_count(self: &SessionListModel, parent: &QModelIndex) -> i32;

        #[qinvokable]
        #[cxx_override]
        fn data(self: &SourceListModel, index: &QModelIndex, role: i32) -> QVariant;
        #[qinvokable]
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &SourceListModel) -> QHash_i32_QByteArray;
        #[qinvokable]
        #[cxx_override]
        #[cxx_name = "rowCount"]
        fn row_count(self: &SourceListModel, parent: &QModelIndex) -> i32;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(*mut SessionListModel, session_model, cxx_name = "sessionModel")]
        #[qproperty(*mut SourceListModel, sources_model, cxx_name = "sourcesModel")]
        #[qproperty(i32, active_session_id, cxx_name = "activeSessionId", READ, NOTIFY = active_session_changed)]
        #[qproperty(QString, active_label, cxx_name = "activeLabel", READ, NOTIFY = active_session_changed)]
        #[qproperty(QString, active_phase, cxx_name = "activePhase", READ, NOTIFY = active_session_changed)]
        #[qproperty(i32, active_done, cxx_name = "activeDone", READ, NOTIFY = active_session_changed)]
        #[qproperty(i32, active_total, cxx_name = "activeTotal", READ, NOTIFY = active_session_changed)]
        #[qproperty(QString, active_file, cxx_name = "activeFile", READ, NOTIFY = active_session_changed)]
        #[qproperty(i32, active_eta_seconds, cxx_name = "activeEtaSeconds", READ, NOTIFY = active_session_changed)]
        #[qproperty(i32, queued_count, cxx_name = "queuedCount", READ, NOTIFY = active_session_changed)]
        #[qproperty(bool, imports_paused, cxx_name = "importsPaused", READ, NOTIFY = imports_paused_changed)]
        #[qproperty(bool, system_prefers_dark, cxx_name = "systemPrefersDark", READ, NOTIFY = system_prefers_dark_changed)]
        #[qproperty(bool, dnglab_ready, cxx_name = "dnglabReady", READ, NOTIFY = dnglab_ready_changed)]
        #[qproperty(bool, watch_enabled, cxx_name = "watchEnabled", READ, NOTIFY = watch_enabled_changed)]
        #[qproperty(QString, theme_accent, cxx_name = "themeAccent", READ, NOTIFY = palette_changed)]
        #[qproperty(QVariant, theme_custom, cxx_name = "themeCustom", READ, NOTIFY = palette_changed)]
        #[qproperty(bool, start_hidden, cxx_name = "startHidden", READ, CONSTANT)]
        #[qproperty(QString, app_version, cxx_name = "appVersion", READ, CONSTANT)]
        type AppController = super::Controller;

        #[qsignal]
        #[cxx_name = "activeSessionChanged"]
        fn active_session_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "importsPausedChanged"]
        fn imports_paused_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "systemPrefersDarkChanged"]
        fn system_prefers_dark_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "dnglabReadyChanged"]
        fn dnglab_ready_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "watchEnabledChanged"]
        fn watch_enabled_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "paletteChanged"]
        fn palette_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "navigateToSession"]
        fn navigate_to_session(
            self: Pin<&mut AppController>,
            session_id: i32,
            device_label: QString,
        );
        #[qsignal]
        #[cxx_name = "previewReady"]
        fn preview_ready(self: Pin<&mut AppController>, source_key: QString, items: QVariant);
        #[qsignal]
        #[cxx_name = "previewFailed"]
        fn preview_failed(self: Pin<&mut AppController>, source_key: QString, message: QString);
        #[qsignal]
        fn toast(self: Pin<&mut AppController>, message: QString);
        #[qsignal]
        #[cxx_name = "showRequested"]
        fn show_requested(self: Pin<&mut AppController>);

        #[qinvokable]
        fn start(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "refreshDevices"]
        fn refresh_devices(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "getSessionFiles"]
        fn get_session_files(self: &AppController, session_id: i32) -> QVariant;
        #[qinvokable]
        #[cxx_name = "getLibraryStats"]
        fn get_library_stats(self: &AppController) -> QVariant;
        #[qinvokable]
        #[cxx_name = "openFile"]
        fn open_file(self: Pin<&mut AppController>, dest_path: QString);
        #[qinvokable]
        #[cxx_name = "revealInFileBrowser"]
        fn reveal_in_file_browser(self: Pin<&mut AppController>, dest_path: QString);
        #[qinvokable]
        #[cxx_name = "ejectSource"]
        fn eject_source(self: Pin<&mut AppController>, source_key: QString);
        #[qinvokable]
        #[cxx_name = "clearSession"]
        fn clear_session(self: Pin<&mut AppController>, session_id: i32);
        #[qinvokable]
        #[cxx_name = "clearHistory"]
        fn clear_history(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "addFolder"]
        fn add_folder(self: Pin<&mut AppController>, folder_url: QString);
        #[qinvokable]
        #[cxx_name = "removeFolder"]
        fn remove_folder(self: Pin<&mut AppController>, source_key: QString);
        #[qinvokable]
        #[cxx_name = "requestPreview"]
        fn request_preview(self: Pin<&mut AppController>, source_key: QString);
        #[qinvokable]
        #[cxx_name = "importSelected"]
        fn import_selected(
            self: Pin<&mut AppController>,
            source_key: QString,
            filenames: QList_QVariant,
        );
        #[qinvokable]
        #[cxx_name = "markAlreadyImported"]
        fn mark_already_imported(
            self: Pin<&mut AppController>,
            source_key: QString,
            filenames: QList_QVariant,
        );
        #[qinvokable]
        #[cxx_name = "unmarkImported"]
        fn unmark_imported(
            self: Pin<&mut AppController>,
            source_key: QString,
            file_paths: QList_QVariant,
        );
        #[qinvokable]
        #[cxx_name = "setImportsPaused"]
        fn set_imports_paused(self: Pin<&mut AppController>, paused: bool);
        #[qinvokable]
        #[cxx_name = "retryDnglabSetup"]
        fn retry_dnglab_setup(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "getSetting"]
        fn get_setting(self: &AppController, key: QString) -> QVariant;
        #[qinvokable]
        #[cxx_name = "setSetting"]
        fn set_setting(self: Pin<&mut AppController>, key: QString, value: QVariant);
        #[qinvokable]
        #[cxx_name = "defaultLibraryRoot"]
        fn default_library_root(self: &AppController) -> QString;
        #[qinvokable]
        #[cxx_name = "setLibraryRoot"]
        fn set_library_root(self: Pin<&mut AppController>, folder_url: QString);
    }

    impl cxx_qt::Initialize for AppController {}
    impl cxx_qt::Threading for AppController {}
}

pub use crate::qt::controller::Controller;
pub use crate::qt::models::{SessionRows, SourceRows};
