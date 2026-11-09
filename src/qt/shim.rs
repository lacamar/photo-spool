#[cxx_qt::bridge]
mod ffi {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qqmlapplicationengine.h");
        type QQmlApplicationEngine = cxx_qt_lib::QQmlApplicationEngine;

        include!("shim.h");
        fn app_create();
        fn app_exec() -> i32;
        fn app_quit();
        fn app_destroy();
        fn load_main(engine: Pin<&mut QQmlApplicationEngine>) -> bool;
    }

    extern "Rust" {
        fn thumb_load(id: &str, from_cache: &mut bool) -> Vec<u8>;
        fn thumb_store(id: &str, data: &[u8]);
    }
}

use std::pin::Pin;

use cxx_qt_lib::QQmlApplicationEngine;

fn thumb_load(id: &str, from_cache: &mut bool) -> Vec<u8> {
    let (bytes, cached) = crate::thumbs::load(id);
    *from_cache = cached;
    bytes
}

fn thumb_store(id: &str, data: &[u8]) {
    crate::thumbs::store(id, data);
}

pub fn create_app() {
    ffi::app_create();
}

pub fn exec() -> i32 {
    ffi::app_exec()
}

/// Thread-safe: queued onto the Qt event loop.
pub fn quit() {
    ffi::app_quit();
}

pub fn destroy_app() {
    ffi::app_destroy();
}

pub fn load_main(engine: Pin<&mut QQmlApplicationEngine>) -> bool {
    ffi::load_main(engine)
}
