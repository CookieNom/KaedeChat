fn main() {
    println!("cargo:rerun-if-changed=native");
    let platform = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let mut build = cc::Build::new();
    build.cpp(true).std("c++17").include("native");
    match platform.as_str() {
        "linux" => {
            build.file("native/linux.cpp");
            println!("cargo:rustc-link-lib=pulse");
            println!("cargo:rustc-link-lib=xcb");
        }
        "windows" => {
            build
                .file("native/windows.cpp")
                .file("native/windows_camera.cpp")
                .flag("/EHsc");
            for library in ["ole32", "oleaut32", "strmiids", "mmdevapi", "user32"] {
                println!("cargo:rustc-link-lib={library}");
            }
        }
        "macos" => {
            build.file("native/macos.mm").flag("-fobjc-arc");
            for framework in [
                "Foundation",
                "ScreenCaptureKit",
                "CoreMedia",
                "CoreVideo",
                "AudioToolbox",
            ] {
                println!("cargo:rustc-link-lib=framework={framework}");
            }
        }
        _ => return,
    }
    build.compile("kaede-share-capture");
}
