import AppKit
import Combine
import ServiceManagement
import SwiftUI

/// Everything the menu-bar panel and the window show, and every action they trigger.
///
/// Idle costs nothing: no timers. The embedded supervisor's state.json is watched
/// (it is rewritten on every change and every 30 s as a heartbeat), its process is
/// watched for exit, and remote hubs are followed on their live event stream.
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
    /// The UI language (see L10n); views redraw when it changes.
    @Published private(set) var lang = L10n.lang

    let supervisor = SupervisorController()
    let live = LiveEvents()
    private(set) lazy var panel = PanelModel(app: self)

    private let watcher = StateWatcher()
    private var heartbeat: Double = 0
    /// Fires only if a heartbeat is overdue (a hung supervisor); every heartbeat
    /// moves it further out, so normally it never fires.
    private lazy var staleCheck: DispatchSourceTimer = {
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.setEventHandler { [weak self] in self?.readState() }
        t.schedule(deadline: .distantFuture)
        t.resume()
        return t
    }()
    private var outageChecks: [String: DispatchWorkItem] = [:]
    private var outageNotified: Set<String> = []
    private var forwarders: [Any] = []
    private var credential: (key: String, password: String)?

    init() {
        let d = UserDefaults.standard
        mode = Mode(rawValue: d.string(forKey: "mode") ?? "") ?? .unset
        remoteURL = d.string(forKey: "remoteURL").flatMap(URL.init(string:))
        notificationsEnabled = d.object(forKey: "notifications") as? Bool ?? true
        // Re-publish the nested objects' (rare) changes so SwiftUI views update.
        forwarders = [supervisor.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() },
                      live.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }]
        live.onAlert = { [weak self] alert in
            // Pushes only for Sentry and other major events: the hub's "critical"
            // and "warning" levels. Info-level alerts stay in the hub's history.
            guard let self, self.notificationsEnabled,
                  alert.level == "critical" || alert.level == "warning" else { return }
            // Hub titles already read "{icon} {car}：{event}"; a subtitle repeating the car is noise.
            let car = alert.carName.flatMap { alert.title.contains($0) ? nil : $0 }
            Notifier.shared.post(id: "alert-\(alert.id)", title: alert.title, subtitle: car,
                                 body: alert.body, path: alert.path.flatMap { $0.hasPrefix("/") ? $0 : nil } ?? "/security")
        }
        live.onConnect = { [weak self] in self?.syncLanguage() }
        watcher.onChange = { [weak self] in self?.readState() }
    }

    // MARK: lifecycle

    func start() {
        refreshLoginItem()
        credential = nil
        switch mode {
        case .embedded:
            keepAwake = ControlFile.load().keepAwakeSentry
            supervisor.ensureRunning()
            watcher.watch(directory: AppPaths.home)
            readState()
        case .remote:
            watcher.stop()
            connectLive()
        case .unset:
            watcher.stop()
        }
    }

    /// state.json changed, the supervisor exited, or its heartbeat is overdue.
    private func readState() {
        guard mode == .embedded else { return }
        let s = SupervisorState.load()
        if s != state { state = s }          // `==` ignores the heartbeat timestamp
        heartbeat = s?.updated ?? 0
        let fresh = s.map(isAlive) ?? false
        if fresh != stateFresh { stateFresh = fresh }
        watcher.watch(process: fresh ? s?.pid.map(pid_t.init) : nil)
        guard let s, fresh else {
            staleCheck.schedule(deadline: .distantFuture)
            return
        }
        let due = heartbeat + SupervisorState.freshFor - Date().timeIntervalSince1970 + 1
        staleCheck.schedule(deadline: .now() + max(1, due), leeway: .seconds(5))
        if s.services["hub"]?.state == "running" { connectLive() }
        scheduleOutageChecks(s)
    }

    private func isAlive(_ s: SupervisorState) -> Bool {
        guard let pid = s.pid, kill(pid_t(pid), 0) == 0, let updated = s.updated else { return false }
        return Date().timeIntervalSince1970 - updated < SupervisorState.freshFor
    }

    /// A crash that the supervisor recovers from in seconds is not news. One
    /// notification only when monitoring has really stopped: a service that has
    /// not been running for two minutes (it counts as a major event).
    private func scheduleOutageChecks(_ s: SupervisorState) {
        for (name, svc) in s.services {
            let down = s.desired == "running" && ["backoff", "unhealthy"].contains(svc.state)
            if !down {
                outageChecks.removeValue(forKey: name)?.cancel()
                outageNotified.remove(name)
                continue
            }
            guard outageChecks[name] == nil, !outageNotified.contains(name) else { continue }
            let check = DispatchWorkItem { [weak self] in self?.outageDue(name) }
            outageChecks[name] = check
            DispatchQueue.main.asyncAfter(deadline: .now() + 120, execute: check)
        }
    }

    private func outageDue(_ name: String) {
        outageChecks[name] = nil
        guard let s = state, stateFresh, s.desired == "running", let svc = s.services[name],
              ["backoff", "unhealthy"].contains(svc.state) else { return }
        outageNotified.insert(name)
        guard notificationsEnabled else { return }
        let service = ServiceText.name(name)
        let gap = service.last?.isASCII == true ? " " : ""     // "TeslaMate 已停止" but "数据库已停止"
        Notifier.shared.post(id: "svc-\(name)-\(Int(Date().timeIntervalSince1970))",
                             title: tr("监控中断", "Monitoring stopped"),
                             body: tr("\(service)\(gap)已停止 2 分钟，正在自动重试。",
                                      "\(service) has been down for 2 minutes. Retrying."))
    }

    private func connectLive() {
        guard let hub = endpoint else { return }
        live.start(base: hub.base, password: hub.password)
    }

    // MARK: language

    /// Follow the hub's language setting ("zh" | "en"); remembered for the next launch.
    func adoptLanguage(_ locale: String?) {
        guard let l = locale.flatMap(Lang.init(rawValue:)) else { return }
        if UserDefaults.standard.string(forKey: L10n.defaultsKey) != l.rawValue {
            UserDefaults.standard.set(l.rawValue, forKey: L10n.defaultsKey)
        }
        guard l != lang else { return }
        L10n.lang = l
        lang = l
        AppDelegate.shared?.languageChanged()
    }

    /// Ask the hub for its language once per connection.
    private func syncLanguage() {
        guard let hub = endpoint else { return }
        Task { [weak self] in
            if let prefs = try? await HubAPI.object("api/prefs", from: hub) {
                self?.adoptLanguage(prefs.string("locale"))
            }
        }
    }

    // MARK: derived state

    var hubURL: URL? {
        switch mode {
        case .embedded: return state?.hubURL
        case .remote: return remoteURL
        case .unset: return nil
        }
    }

    /// The portal password: config.env (re-read when the file changes) or the keychain.
    var password: String? {
        let key: String
        switch mode {
        case .embedded:
            let changed = (try? AppPaths.config.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate?.timeIntervalSince1970 ?? 0
            key = "embedded:\(AppPaths.config.path):\(changed)"
        case .remote: key = "remote:\(remoteURL?.absoluteString ?? "")"
        case .unset: return nil
        }
        if let credential, credential.key == key { return credential.password }
        let pw: String?
        switch mode {
        case .embedded: pw = AppPaths.readConfig()["OHMYTESLA_PASSWORD"]
        case .remote: pw = remoteURL.flatMap { Keychain.get(account: $0.absoluteString) }
        case .unset: pw = nil
        }
        if let pw { credential = (key, pw) }
        return pw
    }

    /// The hub to talk to, once it can answer.
    var endpoint: HubEndpoint? {
        guard hubReady, let base = hubURL, let pw = password else { return nil }
        return HubEndpoint(base: base, password: pw)
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
        case .unset: return tr("尚未设置", "Not set up yet")
        case .remote:
            let host = remoteURL?.host ?? tr("服务器", "server")
            return live.connected ? tr("已连接 \(host)", "Connected to \(host)") : tr("正在连接 \(host)…", "Connecting to \(host)…")
        case .embedded:
            guard let s = state, stateFresh else {
                return supervisor.runMode == .none ? tr("服务未运行", "Services not running") : tr("正在启动服务…", "Starting services…")
            }
            switch s.status {
            case "running": return tr("服务运行中", "Services running")
            case "starting": return s.firstRun ? tr("首次启动，正在设置…", "First start — setting up…") : tr("正在启动…", "Starting…")
            case "degraded": return tr("部分服务正在重启", "Some services are restarting")
            case "stopped": return tr("服务已停止", "Services stopped")
            case "stopping": return tr("正在停止…", "Stopping…")
            case "error": return tr("出错：\(s.error)", "Error: \(s.error)")
            default: return s.status
            }
        }
    }

    enum ServiceHealth {
        case ok, busy, warning, error, off

        var color: Color {
            switch self {
            case .ok: return .green
            case .busy: return .yellow
            case .warning: return .orange
            case .error: return .red
            case .off: return .secondary
            }
        }

        /// Shown expanded in the panel without being asked.
        var needsAttention: Bool { self == .warning || self == .error }
    }

    var serviceHealth: ServiceHealth {
        guard let s = state, stateFresh else { return supervisor.runMode == .none ? .off : .busy }
        switch s.status {
        case "running": return .ok
        case "degraded": return .warning
        case "error": return .error
        case "stopped": return .off
        default: return .busy
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
        watcher.stop()
        mode = .unset
        state = nil
        stateFresh = false
        credential = nil
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

/// Watches a directory for entries being written or replaced (state.json is
/// replaced atomically) and one process for its exit. Event-driven: kqueue, no polling.
final class StateWatcher {
    var onChange: (() -> Void)?
    private var directory: DispatchSourceFileSystemObject?
    private var directoryPath: String?
    private var process: DispatchSourceProcess?
    private var processID: pid_t?

    func watch(directory url: URL) {
        guard directoryPath != url.path || directory == nil else { return }
        directory?.cancel()
        directory = nil
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        let fd = open(url.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .rename, .delete],
                                                               queue: .main)
        source.setEventHandler { [weak self] in
            guard let self else { return }
            if source.data.contains(.delete) || source.data.contains(.rename) {
                self.directory?.cancel()             // the folder itself went away; watch it anew
                self.directory = nil
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.watch(directory: url) }
            }
            self.onChange?()
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        directory = source
        directoryPath = url.path
    }

    func watch(process pid: pid_t?) {
        guard pid != processID else { return }
        process?.cancel()
        process = nil
        processID = pid
        guard let pid else { return }
        let source = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: .main)
        source.setEventHandler { [weak self] in
            self?.process?.cancel()
            self?.process = nil
            self?.processID = nil
            self?.onChange?()
        }
        source.resume()
        process = source
    }

    func stop() {
        directory?.cancel()
        directory = nil
        directoryPath = nil
        watch(process: nil)
    }
}
