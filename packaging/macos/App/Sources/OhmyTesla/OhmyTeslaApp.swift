import AppKit
import SwiftUI

struct OhmyTeslaApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            StatusMenu()
                .environmentObject(delegate.model)
        } label: {
            MenuBarLabel().environmentObject(delegate.model)
        }
        .menuBarExtraStyle(.menu)
    }
}

struct MenuBarLabel: View {
    @EnvironmentObject var model: AppModel
    var body: some View { Image(systemName: model.menuBarSymbol) }
}

/// The menu-bar menu.
struct StatusMenu: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Text(model.statusLine)
        if model.mode == .embedded, model.stateFresh, let state = model.state {
            ForEach(SupervisorState.order, id: \.self) { name in
                if let svc = state.services[name], svc.state != "disabled" {
                    Text("\(glyph(svc.state))  \(svc.title) — \(svc.state)")
                }
            }
        }
        Divider()
        Button("Open OhmyTesla") { AppDelegate.shared?.showMainWindow() }
            .keyboardShortcut("o")
        if model.mode == .embedded {
            Button("Open TeslaMate") { model.openTeslaMate() }.disabled(!model.stateFresh)
            Divider()
            if model.state?.desired == "stopped" || !model.stateFresh {
                Button("Start Services") { model.startServices() }
            } else {
                Button("Stop Services") { model.stopServices() }
                Button("Restart Services") { model.restartServices() }
            }
            Toggle("Keep Mac Awake While Sentry Mode Is On", isOn: Binding(
                get: { model.keepAwake }, set: { model.setKeepAwake($0) }))
            if model.state?.keepAwake?.active == true {
                Text("☕︎  Sentry Mode is on — keeping this Mac awake")
            }
        }
        if model.mode != .unset {
            Divider()
            Toggle("Notifications", isOn: $model.notificationsEnabled)
            Toggle("Open at Login", isOn: Binding(get: { model.openAtLogin }, set: { model.setOpenAtLogin($0) }))
            Button("Copy Portal Password") { model.copyPassword() }
            if model.mode == .embedded {
                Button("Open Logs Folder") { model.openLogs() }
                Button("Open Data Folder") { model.openDataFolder() }
                Button("Edit Settings (config.env)…") { model.openConfig() }
                if model.supervisor.runMode == .child {
                    Button("Install Background Service") {
                        Task { await model.supervisor.reinstallAgent() }
                    }
                }
            }
            Menu("Mode") {
                Button(model.mode == .embedded ? "✓ Running on this Mac" : "Run on this Mac…") {
                    model.resetMode()
                    AppDelegate.shared?.showMainWindow()
                }
                Button(model.mode == .remote ? "✓ Connected to \(model.remoteURL?.host ?? "server")"
                                              : "Connect to a server…") {
                    model.resetMode()
                    AppDelegate.shared?.showMainWindow()
                }
            }
        }
        Divider()
        if model.mode == .embedded && model.supervisor.runMode == .agent {
            Button("Quit (services keep running)") { model.quit(stopServices: false) }
                .keyboardShortcut("q")
            Button("Stop Services and Quit") { model.quit(stopServices: true) }
        } else {
            Button("Quit OhmyTesla") { model.quit(stopServices: false) }
                .keyboardShortcut("q")
        }
    }

    private func glyph(_ state: String) -> String {
        switch state {
        case "running": return "●"
        case "backoff", "unhealthy": return "▲"
        case "stopped": return "○"
        default: return "◐"
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    static weak var shared: AppDelegate?
    let model = AppModel()
    let web = WebController()
    private var window: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        AppDelegate.shared = self
        installMainMenu()
        Notifier.shared.setUp()
        Notifier.shared.onOpen = { [weak self] path in
            self?.showMainWindow()
            if let path, let base = self?.model.hubURL, let pw = self?.model.password {
                self?.web.load(base: base, password: pw, path: path)
            }
        }
        model.start()
        if !launchedAsLoginItem || model.mode == .unset { showMainWindow() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        // A child supervisor (fallback mode) would notice we're gone, but stop it
        // explicitly so the database shuts down before we disappear.
        model.supervisor.stopChild()
    }

    /// Launched by SMAppService.mainApp at login: stay in the menu bar.
    private var launchedAsLoginItem: Bool {
        guard let event = NSAppleEventManager.shared().currentAppleEvent else { return false }
        return event.eventID == kAEOpenApplication
            && event.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }

    func showMainWindow() {
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                             backing: .buffered, defer: false)
            w.title = "OhmyTesla"
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.setFrameAutosaveName("OhmyTeslaMain")
            w.contentView = NSHostingView(rootView: RootView().environmentObject(model).environmentObject(web))
            if w.frame.origin == .zero { w.center() }
            window = w
        }
        NSApp.setActivationPolicy(.regular)
        installMainMenu()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        // Back to a menu-bar-only app; the services keep running.
        NSApp.setActivationPolicy(.accessory)
    }

    /// Standard menus so ⌘C/⌘V, ⌘R and friends work inside the web view.
    private func installMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About OhmyTesla", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide OhmyTesla", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit OhmyTesla", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reloadPage), keyEquivalent: "r").target = self
        view.addItem(withTitle: "Back", action: #selector(goBack), keyEquivalent: "[").target = self
        view.addItem(withTitle: "Forward", action: #selector(goForward), keyEquivalent: "]").target = self
        view.addItem(withTitle: "Home", action: #selector(goHome), keyEquivalent: "H").target = self
        view.addItem(.separator())
        view.addItem(withTitle: "Open in Browser", action: #selector(openInBrowser), keyEquivalent: "b").target = self
        viewItem.submenu = view
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = main
    }

    @objc private func reloadPage() { web.reload() }
    @objc private func goBack() { web.webView.goBack() }
    @objc private func goForward() { web.webView.goForward() }
    @objc private func goHome() { web.goHome() }
    @objc private func openInBrowser() { model.openInBrowser() }
}
