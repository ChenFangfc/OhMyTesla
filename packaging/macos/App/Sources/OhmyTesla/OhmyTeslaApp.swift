import AppKit
import SwiftUI

struct OhmyTeslaApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            PanelView()
                .environmentObject(delegate.model)
                .environmentObject(delegate.model.panel)
        } label: {
            MenuBarLabel().environmentObject(delegate.model)
        }
        .menuBarExtraStyle(.window)
        .commands { AppCommands(model: delegate.model, web: delegate.web) }
    }
}

struct MenuBarLabel: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        Image(systemName: model.menuBarSymbol)
            .accessibilityLabel("OhmyTesla")
    }
}

/// Our items in the menu bar, next to the standard ones (Edit gives ⌘C/⌘V/⌘A in the
/// window and the web view). SwiftUI owns the main menu: one set with NSApp.mainMenu
/// was rewritten each time the app came forward, which dropped Edit, so ⌘V did nothing.
struct AppCommands: Commands {
    @ObservedObject var model: AppModel
    let web: WebController

    var body: some Commands {
        CommandGroup(after: .appInfo) {
            // Run on this Mac ⇄ a server; absent until a mode is chosen
            if let title = model.changeModeTitle {
                Divider()
                Button(title) { AppDelegate.shared?.changeMode() }
            }
        }
        CommandGroup(before: .toolbar) {
            Button(tr("重新载入", "Reload")) { web.reload() }.keyboardShortcut("r")
            Button(tr("后退", "Back")) { web.goBack() }.keyboardShortcut("[")
            Button(tr("前进", "Forward")) { web.goForward() }.keyboardShortcut("]")
            Button(tr("首页", "Home")) { web.goHome() }.keyboardShortcut("h", modifiers: [.command, .shift])
            Divider()
            Button(tr("在浏览器中打开", "Open in Browser")) { model.openInBrowser() }.keyboardShortcut("b")
        }
        CommandGroup(replacing: .help) {}
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
        NSWindow.allowsAutomaticWindowTabbing = false    // one window: no tab items in View
        if AppPaths.runsFromDiskImage { askToInstall(); return }
        Notifier.shared.setUp()
        Notifier.shared.onOpen = { [weak self] path in self?.showMainWindow(path: path) }
        model.start()
        if !launchedAsLoginItem || model.mode == .unset { showMainWindow() }
    }

    /// Opened straight from the DMG: start nothing, say how to install, quit. Whatever mode
    /// was chosen before, its background service or login item would point into the disk
    /// image and stop working once it is ejected.
    private func askToInstall() {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = tr("先把 OhmyTesla 拖到「应用程序」", "Move OhmyTesla to Applications First")
        alert.informativeText = tr("它现在是从磁盘映像里打开的。退出后，在访达里把它拖到「应用程序」文件夹，再从那里打开。",
                                   "It is running from the disk image. Quit, drag it to the Applications folder in "
                                   + "Finder, then open it from there.")
        alert.addButton(withTitle: tr("退出", "Quit"))
        alert.runModal()
        NSApp.terminate(nil)
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

    /// Open (or bring forward) the window; `path` opens that page of the hub.
    func showMainWindow(path: String? = nil) {
        if let path, let base = model.hubURL, let pw = model.password {
            web.load(base: base, password: pw, path: path)
        }
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
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Back to the setup screen — from the app menu, the panel, the startup screen or the
    /// setup guide. Leaving "Run on this Mac" stops its services; its data stays, so
    /// choosing it again carries on where it left off.
    @objc func changeMode() {
        web.tearDown()      // the next page is built for the next mode (see makeWebView)
        model.resetMode()
        showMainWindow()
    }

    func windowWillClose(_ notification: Notification) {
        // Back to a menu-bar-only app; the services keep running. The window, its
        // SwiftUI tree and the web view (with its WebKit process) are released;
        // the next showMainWindow builds them again.
        NSApp.setActivationPolicy(.accessory)
        web.tearDown()
        DispatchQueue.main.async { [weak self] in
            guard let self, let w = self.window, !w.isVisible else { return }
            w.contentView = nil
            w.delegate = nil
            self.window = nil
        }
    }
}
