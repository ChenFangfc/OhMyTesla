import AppKit
import Combine
import ServiceManagement
import SwiftUI

/// Everything the menu and the window show, and every action they trigger.
@MainActor
final class AppModel: ObservableObject {
    enum Mode: String { case unset, embedded, remote }

    @Published var mode: Mode {
        didSet { UserDefaults.standard.set(mode.rawValue, forKey: "mode") }
    }
    @Published private(set) var state: SupervisorState?
    @Published private(set) var stateFresh = false
    @Published var remoteURL: URL? {
        didSet { UserDefaults.standard.set(remoteURL?.absoluteString, forKey: "remoteURL") }
    }
    @Published var notificationsEnabled: Bool {
        didSet { UserDefaults.standard.set(notificationsEnabled, forKey: "notifications") }
    }
    @Published private(set) var keepAwake = false
    @Published private(set) var openAtLogin = false

    let supervisor = SupervisorController()
    let live = LiveEvents()

    private var timer: Timer?
    private var downSince: [String: Date] = [:]
    private var outageNotified: Set<String> = []
    private var forwarders: [Any] = []

    init() {
        let d = UserDefaults.standard
        mode = Mode(rawValue: d.string(forKey: "mode") ?? "") ?? .unset
        remoteURL = d.string(forKey: "remoteURL").flatMap(URL.init(string:))
        notificationsEnabled = d.object(forKey: "notifications") as? Bool ?? true
        // re-publish nested objects' changes so SwiftUI views update
        forwarders = [supervisor.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() },
                      live.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }]
        live.onAlert = { [weak self] alert in
            // Pushes only for Sentry and other major events: the hub's "critical"
            // and "warning" levels. Info-level alerts stay in the hub's history.
            guard let self, self.notificationsEnabled,
                  alert.level == "critical" || alert.level == "warning" else { return }
            Notifier.shared.post(id: "alert-\(alert.id)", title: alert.title, subtitle: alert.carName,
                                 body: alert.body, critical: alert.level == "critical",
                                 path: alert.path.flatMap { $0.hasPrefix("/") ? $0 : nil } ?? "/security")
        }
    }

    // MARK: lifecycle

    func start() {
        refreshLoginItem()
        switch mode {
        case .embedded:
            keepAwake = ControlFile.load().keepAwakeSentry
            supervisor.ensureRunning()
        case .remote:
            connectLive()
        case .unset:
            break
        }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        tick()
    }

    private func tick() {
        guard mode == .embedded else { return }
        let s = SupervisorState.load()
        if s != state { state = s }
        let fresh = s?.isFresh ?? false
        if fresh != stateFresh { stateFresh = fresh }
        if let s, fresh {
            if s.services["hub"]?.state == "running" { connectLive() }
            notifyServiceChanges(s)
        }
    }

    /// A crash that the supervisor recovers from in seconds is not news. One
    /// notification only when monitoring has really stopped: a service that has
    /// not been running for two minutes (it counts as a major event).
    private func notifyServiceChanges(_ s: SupervisorState) {
        let now = Date()
        for (name, svc) in s.services {
            let down = s.desired == "running" && ["backoff", "unhealthy"].contains(svc.state)
            if !down {
                downSince[name] = nil
                outageNotified.remove(name)
                continue
            }
            let since = downSince[name] ?? now
            downSince[name] = since
            if notificationsEnabled, now.timeIntervalSince(since) > 120, !outageNotified.contains(name) {
                outageNotified.insert(name)
                Notifier.shared.post(id: "svc-\(name)-\(Int(now.timeIntervalSince1970))",
                                     title: "Monitoring interrupted",
                                     body: "\(svc.title) has not been running for 2 minutes"
                                         + (svc.lastError.isEmpty ? "." : ": \(svc.lastError).")
                                         + " OhmyTesla keeps retrying; see Open Logs Folder.",
                                     critical: true)
            }
        }
    }

    private func connectLive() {
        guard let base = hubURL, let pw = password else { return }
        live.start(base: base, password: pw)
    }

    // MARK: derived state

    var hubURL: URL? {
        switch mode {
        case .embedded: return state?.hubURL
        case .remote: return remoteURL
        case .unset: return nil
        }
    }

    var password: String? {
        switch mode {
        case .embedded: return AppPaths.readConfig()["OHMYTESLA_PASSWORD"]
        case .remote: return remoteURL.flatMap { Keychain.get(account: $0.absoluteString) }
        case .unset: return nil
        }
    }

    var hubReady: Bool {
        switch mode {
        case .embedded: return stateFresh && state?.services["hub"]?.state == "running"
        case .remote: return remoteURL != nil
        case .unset: return false
        }
    }

    var statusLine: String {
        switch mode {
        case .unset: return "Not set up yet"
        case .remote: return live.connected ? "Connected to \(remoteURL?.host ?? "server")"
                                            : "Connecting to \(remoteURL?.host ?? "server")…"
        case .embedded:
            guard let s = state, stateFresh else {
                return supervisor.runMode == .none ? "Services not running" : "Starting services…"
            }
            switch s.status {
            case "running": return "Running on this Mac"
            case "starting": return s.firstRun ? "First start — setting up…" : "Starting…"
            case "degraded": return "Some services are restarting"
            case "stopped": return "Services stopped"
            case "stopping": return "Stopping…"
            case "error": return "Error: \(s.error)"
            default: return s.status.capitalized
            }
        }
    }

    var menuBarSymbol: String {
        switch mode {
        case .unset: return "bolt.car"
        case .remote: return live.connected ? "bolt.car.fill" : "bolt.car"
        case .embedded:
            guard let s = state, stateFresh else { return "bolt.car" }
            switch s.status {
            case "running": return "bolt.car.fill"
            case "degraded", "error": return "exclamationmark.triangle.fill"
            default: return "bolt.car"
            }
        }
    }

    var servicesRunning: Bool {
        stateFresh && (state?.desired == "running")
    }

    // MARK: actions

    func chooseEmbedded() {
        mode = .embedded
        setOpenAtLogin(true)
        start()
    }

    func chooseRemote(url: URL, password: String) {
        if mode == .embedded { stopEmbedded(unregister: true) }
        Keychain.set(password, account: url.absoluteString)
        remoteURL = url
        mode = .remote
        setOpenAtLogin(true)
        live.stop()
        start()
    }

    func resetMode() {
        if mode == .embedded { stopEmbedded(unregister: true) }
        live.stop()
        mode = .unset
    }

    private func stopEmbedded(unregister: Bool) {
        supervisor.setDesired(false)
        supervisor.stopChild()
        if unregister { Task { await supervisor.disableBackground() } }
    }

    func startServices() { supervisor.setDesired(true) }
    func stopServices() { supervisor.setDesired(false) }
    func restartServices() { supervisor.restartServices() }

    func setKeepAwake(_ on: Bool) {
        keepAwake = on
        supervisor.setKeepAwake(on)
    }

    func setOpenAtLogin(_ on: Bool) {
        if on { try? SMAppService.mainApp.register() } else { try? SMAppService.mainApp.unregister() }
        refreshLoginItem()
    }

    private func refreshLoginItem() {
        openAtLogin = SMAppService.mainApp.status == .enabled
    }

    func copyPassword() {
        guard let pw = password else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(pw, forType: .string)
    }

    func openLogs() { NSWorkspace.shared.open(AppPaths.logs) }
    func openDataFolder() { NSWorkspace.shared.open(AppPaths.home) }
    func openConfig() { NSWorkspace.shared.open(AppPaths.config) }

    func openTeslaMate() {
        if let url = state?.teslaMateURL { NSWorkspace.shared.open(url) }
    }

    func openInBrowser() {
        if let url = hubURL { NSWorkspace.shared.open(url) }
    }

    /// Quit the app. With the LaunchAgent the services keep running; a child
    /// supervisor (fallback mode) stops with the app.
    func quit(stopServices: Bool) {
        if mode == .embedded {
            if stopServices { supervisor.setDesired(false) }
            supervisor.stopChild()
        }
        NSApp.terminate(nil)
    }
}
