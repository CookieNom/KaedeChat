// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "flutter_webrtc",
    platforms: [
        .iOS("13.0")
    ],
    products: [
        .library(name: "flutter-webrtc", targets: ["flutter_webrtc"]),
        // Lets dependent plugins (e.g. livekit_client) import WebRTC without
        // declaring a second copy of the binary target.
        .library(name: "WebRTC", targets: ["WebRTC"])
    ],
    dependencies: [
        .package(name: "FlutterFramework", path: "../FlutterFramework")
    ],
    targets: [
        .binaryTarget(
            name: "WebRTC",
            // Match CocoaPods: never substitute an unpatched remote cryptor.
            path: "../../../../native/webrtc/WebRTC.xcframework"
        ),
        .target(
            name: "flutter_webrtc",
            dependencies: [
                "WebRTC",
                .product(name: "FlutterFramework", package: "FlutterFramework")
            ],
            cSettings: [
                .headerSearchPath("include/flutter_webrtc")
            ],
            linkerSettings: [
                .linkedLibrary("c++"),
                .linkedFramework("AVKit")
            ]
        )
    ],
    cxxLanguageStandard: .cxx14
)
