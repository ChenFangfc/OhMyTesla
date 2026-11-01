// swift-tools-version:5.9
// OhmyTesla for macOS — the menu-bar app around the embedded TeslaMate stack.
// Built by ../scripts/build_app.sh, which also assembles the .app bundle.
import PackageDescription

let package = Package(
    name: "OhmyTesla",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "OhmyTesla",
            path: "Sources/OhmyTesla",
            linkerSettings: [
                .linkedFramework("ServiceManagement"),
                .linkedFramework("WebKit"),
                .linkedFramework("UserNotifications"),
                .linkedFramework("Security"),
            ]
        ),
    ]
)
