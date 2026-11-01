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
    }
}

struct MenuBarLabel: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        Image(systemName: model.menuBarSymbol)
            .accessibilityLabel("OhmyTesla")
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
        Notifier.shared.onOpen = { [weak self] path in self?.showMainWindow(path: path) }
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
        installMainMenu()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
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

    /// The hub's language setting changed: menus and the window follow.
    func languageChanged() {
        installMainMenu()
    }

    /// Standard menus so ⌘C/⌘V, ⌘R and friends work inside the web view.
    private func installMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: tr("关于 OhmyTesla", "About OhmyTesla"),
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: tr("隐藏 OhmyTesla", "Hide OhmyTesla"), action: #selector(NSApplication.hide(_:)),
                        keyEquivalent: "h")
        appMenu.addItem(withTitle: tr("关闭窗口", "Close Window"), action: #selector(NSWindow.performClose(_:)),
                        keyEquivalent: "w")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: tr("退出 OhmyTesla", "Quit OhmyTesla"), action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: tr("编辑", "Edit"))
        edit.addItem(withTitle: tr("撤销", "Undo"), action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: tr("重做", "Redo"), action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: tr("剪切", "Cut"), action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: tr("拷贝", "Copy"), action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: tr("粘贴", "Paste"), action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: tr("全选", "Select All"), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: tr("显示", "View"))
        view.addItem(withTitle: tr("重新载入", "Reload"), action: #selector(reloadPage), keyEquivalent: "r").target = self
        view.addItem(withTitle: tr("后退", "Back"), action: #selector(goBack), keyEquivalent: "[").target = self
        view.addItem(withTitle: tr("前进", "Forward"), action: #selector(goForward), keyEquivalent: "]").target = self
        view.addItem(withTitle: tr("首页", "Home"), action: #selector(goHome), keyEquivalent: "H").target = self
        view.addItem(.separator())
        view.addItem(withTitle: tr("在浏览器中打开", "Open in Browser"), action: #selector(openInBrowser),
                     keyEquivalent: "b").target = self
        viewItem.submenu = view
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: tr("窗口", "Window"))
        windowMenu.addItem(withTitle: tr("最小化", "Minimize"), action: #selector(NSWindow.performMiniaturize(_:)),
                           keyEquivalent: "m")
        windowMenu.addItem(withTitle: tr("缩放", "Zoom"), action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = main
    }

    @objc private func reloadPage() { web.reload() }
    @objc private func goBack() { web.goBack() }
    @objc private func goForward() { web.goForward() }
    @objc private func goHome() { web.goHome() }
    @objc private func openInBrowser() { model.openInBrowser() }
}
