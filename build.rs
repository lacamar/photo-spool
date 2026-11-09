use cxx_qt_build::{CxxQtBuilder, QmlFile, QmlModule};

fn qml_files(dir: &str, out: &mut Vec<String>) {
    for entry in std::fs::read_dir(dir).unwrap().flatten() {
        let path = entry.path();
        if path.is_dir() {
            qml_files(&path.to_string_lossy(), out);
        } else if path.extension().is_some_and(|e| e == "qml")
            && path.file_name().unwrap() != "Theme.qml"
        {
            out.push(path.to_string_lossy().into_owned());
        }
    }
}

fn main() {
    let mut views = Vec::new();
    qml_files("qml", &mut views);
    views.sort();
    println!("cargo::rerun-if-changed=qml");
    CxxQtBuilder::new_qml_module(
        QmlModule::new("PhotoSpool").qml_file(QmlFile::from("qml/Theme.qml").singleton(true)),
    )
    .files(["src/qt/bridge.rs", "src/qt/shim.rs"])
    .qrc_resources(views)
    .qt_module("Quick")
    .qt_module("Widgets")
    .include_dir("cpp")
    .cpp_file("cpp/shim.cpp")
    .build();
    // the shim's static lib links after Qt; keep these past --as-needed
    for arg in [
        "-Wl,--push-state,--no-as-needed",
        "-lQt6Quick",
        "-lQt6Widgets",
        "-Wl,--pop-state",
    ] {
        println!("cargo::rustc-link-arg={arg}");
    }
}
