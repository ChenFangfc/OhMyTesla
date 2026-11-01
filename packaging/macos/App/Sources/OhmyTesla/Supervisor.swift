import Foundation
import ServiceManagement

/// What supervisor.py writes to state.json.
struct SupervisorState: Decodable, Equatable {
    struct Service: Decodable, Equatable {
        let title: String
        let state: String
        let healthy: Bool
        let pid: Int?
        let restarts: Int
        let lastError: String
    }
    struct KeepAwake: Decodable, Equatable {
        let enabled: Bool
        let sentryOn: Bool
        let active: Bool
    }
    let version: Int
    let pid: Int?
    let status: String
    let desired: String
    let error: String
    let firstRun: Bool
    let ports: [String: Int]
    let services: [String: Service]
    let keepAwake: KeepAwake?
    let updated: Double?
    let mode: String?

    static func load() -> SupervisorState? {
        guard let data = try? Data(contentsOf: AppPaths.state) else { return nil }
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return try? d.decode(SupervisorState.self, from: data)
    }

    /// The supervisor rewrites the file on every change and at least every 30 s
    /// (version 1 supervisors: every 5 s) while it is alive.
    static let freshFor: TimeInterval = 95

    /// Same content, whatever the heartbeat time: a heartbeat alone is no change.
    static func == (a: SupervisorState, b: SupervisorState) -> Bool {
        a.version == b.version && a.pid == b.pid && a.status == b.status && a.desired == b.desired
            && a.error == b.error && a.firstRun == b.firstRun && a.ports == b.ports && a.services == b.services
            && a.keepAwake == b.keepAwake && a.mode == b.mode
    }

    /// Wake the supervisor so it reads control.json now rather than within 5 s.
    /// Only a version-2 supervisor of this very app (others would die of SIGUSR1).
    static func poke() {
        guard let s = load(), s.version >= 2, let pid = s.pid, pid > 0 else { return }
        var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        guard proc_pidpath(pid_t(pid), &buffer, UInt32(buffer.count)) > 0 else { return }
        let path = URL(fileURLWithPath: String(cString: buffer)).resolvingSymlinksInPath().path
        let runtime = AppPaths.runtime.resolvingSymlinksInPath().path
        guard path.hasPrefix(runtime + "/") else { return }
        // The hub runs the same python3; the supervisor is the one started by
        // launchd (LaunchAgent) or by this app (fallback child).
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid_t(pid), PROC_PIDTBSDINFO, 0, &info, size) == size,
              info.pbi_ppid == 1 || info.pbi_ppid == UInt32(getpid()) else { return }
        kill(pid_t(pid), SIGUSR1)
    }

    var hubURL: URL? {
        guard let port = ports["hub"] else { return nil }
        return URL(string: "http://127.0.0.1:\(port)/")
    }

    var teslaMateURL: URL? {
        guard let port = ports["teslamate"] else { return nil }
        return URL(string: "http://127.0.0.1:\(port)/")
    }

    static let order = ["postgres", "mosquitto", "teslamate", "hub"]
}

/// The desired state the app hands to the supervisor (control.json).
struct ControlFile: Codable {
    var desired: String = "running"
    var keepAwakeSentry: Bool = false
    var restartSeq: Int = 0

    enum CodingKeys: String, CodingKey {
        case desired, keepAwakeSentry = "keep_awake_sentry", restartSeq = "restart_seq"
    }

    static func load() -> ControlFile {
        guard let data = try? Data(contentsOf: AppPaths.control),
              let c = try? JSONDecoder().decode(ControlFile.self, from: data) else { return ControlFile() }
        return c
    }

    func save() {
        try? FileManager.default.createDirectory(at: AppPaths.home, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(self) {
            try? data.write(to: AppPaths.control, options: .atomic)
        }
        SupervisorState.poke()
    }
}

/// Starts the supervisor: preferably as a per-user LaunchAgent registered through
/// SMAppService (keeps running with the app closed and starts at login), falling
/// back to a child process of the app when registration is impossible.
@MainActor
final class SupervisorController: ObservableObject {
    enum RunMode: Equatable { case none, agent, child }

    @Published private(set) var runMode: RunMode = .none
    /// Something the user should know about how the services are being run.
    @Published private(set) var notice: Notice?

    enum Notice: Equatable {
        case translocated, needsApproval, registerFailed(String), startFailed(String)

        var text: String {
            switch self {
            case .translocated:
                return tr("OhmyTesla 正从临时的只读位置运行。请把它移到“应用程序”文件夹再打开，关闭窗口后才能继续监控。",
                          "OhmyTesla is running from a temporary, read-only location. Move it to Applications and "
                          + "open it again to keep monitoring with the app closed.")
            case .needsApproval:
                return tr("请在“系统设置 › 通用 › 登录项”中允许 OhmyTesla，应用关闭后才能继续监控。在此之前，服务只在应用打开时运行。",
                          "Allow OhmyTesla under System Settings › General › Login Items to keep monitoring when the "
                          + "app is closed. Until then the services run only while the app is open.")
            case .registerFailed(let why):
                return tr("无法安装后台服务（\(why)）。服务只在应用打开时运行。",
                          "Could not install the background service (\(why)). The services run only while the app is open.")
            case .startFailed(let why):
                return tr("无法启动服务：\(why)", "Could not start the services: \(why)")
            }
        }
    }

    private var child: Process?
    private let agent = SMAppService.agent(plistName: AppPaths.agentPlistName)

    /// Test hook: OHMYTESLA_NO_AGENT=1 never touches launchd.
    private var agentDisabled: Bool { ProcessInfo.processInfo.environment["OHMYTESLA_NO_AGENT"] == "1" }

    var agentStatus: SMAppService.Status { agent.status }

    func ensureRunning() {
        var c = ControlFile.load()
        if c.desired != "running" {
            c.desired = "running"
            c.save()
        }
        if agentDisabled {
            startChild(reason: nil)
            return
        }
        if translocated {
            startChild(reason: .translocated)
            return
        }
        switch agent.status {
        case .enabled:
            runMode = .agent
            notice = nil
        case .requiresApproval:
            startChild(reason: .needsApproval)
        case .notRegistered, .notFound:
            fallthrough
        @unknown default:
            do {
                try agent.register()
                if agent.status == .requiresApproval {
                    startChild(reason: .needsApproval)
                } else {
                    runMode = .agent
                    notice = nil
                }
            } catch {
                startChild(reason: .registerFailed(error.localizedDescription))
            }
        }
    }

    /// Re-register after the app moved, or when the agent stopped responding.
    func reinstallAgent() async {
        guard !agentDisabled else { return }
        try? await agent.unregister()
        stopChild()
        ensureRunning()
    }

    func disableBackground() async {
        try? await agent.unregister()
        runMode = .none
    }

    func openLoginItemsSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }

    var translocated: Bool { Bundle.main.bundlePath.contains("/AppTranslocation/") }

    // MARK: child-process fallback

    private func startChild(reason: Notice?) {
        notice = reason
        if let child, child.isRunning {
            runMode = .child
            return
        }
        // If a LaunchAgent supervisor is alive it holds the lock; the child exits at once.
        let p = Process()
        p.executableURL = AppPaths.python
        p.arguments = ["-I", "-B", AppPaths.supervisorScript.path, "--runtime", AppPaths.runtime.path,
                       "--exit-with-parent", String(getpid())]
        p.environment = ProcessInfo.processInfo.environment
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            child = p
            runMode = .child
        } catch {
            notice = .startFailed(error.localizedDescription)
            runMode = .none
        }
    }

    /// Stops a child supervisor (it shuts the services down cleanly on SIGTERM).
    func stopChild() {
        guard let child, child.isRunning else { return }
        child.terminate()
        let deadline = Date().addingTimeInterval(90)
        while child.isRunning && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        self.child = nil
    }

    var hasChild: Bool { child?.isRunning == true }

    // MARK: control

    func setDesired(_ running: Bool) {
        var c = ControlFile.load()
        c.desired = running ? "running" : "stopped"
        c.save()
        if running, runMode == .none { ensureRunning() }
    }

    func restartServices() {
        var c = ControlFile.load()
        c.desired = "running"
        c.restartSeq += 1
        c.save()
    }

    func setKeepAwake(_ on: Bool) {
        var c = ControlFile.load()
        c.keepAwakeSentry = on
        c.save()
    }
}

/// `OhmyTesla --supervisor`: become the supervisor (used by the LaunchAgent).
enum SupervisorLauncher {
    static func execSupervisor(extraArguments: [String]) -> Never {
        let python = AppPaths.python.path
        let args = [python, "-I", "-B", AppPaths.supervisorScript.path, "--runtime", AppPaths.runtime.path]
            + extraArguments
        var cargs: [UnsafeMutablePointer<CChar>?] = args.map { strdup($0) }
        cargs.append(nil)
        execv(python, cargs)
        perror("OhmyTesla: execv \(python)")
        exit(127)
    }
}

/// Headless helpers: `--agent-status`, `--register-agent`, `--unregister-agent`.
enum AgentCLI {
    static func run(_ command: String) -> Int32 {
        let agent = SMAppService.agent(plistName: AppPaths.agentPlistName)
        func describe(_ s: SMAppService.Status) -> String {
            switch s {
            case .notRegistered: return "notRegistered"
            case .enabled: return "enabled"
            case .requiresApproval: return "requiresApproval"
            case .notFound: return "notFound"
            @unknown default: return "unknown(\(s.rawValue))"
            }
        }
        switch command {
        case "--register-agent":
            do { try agent.register() } catch {
                print("register failed: \(error.localizedDescription)")
                return 1
            }
        case "--unregister-agent":
            // Also drop the "open at login" item: this is the uninstall path.
            if SMAppService.mainApp.status == .enabled { try? SMAppService.mainApp.unregister() }
            guard agent.status != .notRegistered else { break }
            let sema = DispatchSemaphore(value: 0)
            var failure: Error?
            agent.unregister { error in failure = error; sema.signal() }
            sema.wait()
            if let failure {
                print("unregister failed: \(failure.localizedDescription)")
                return 1
            }
        default:
            break
        }
        print("agent \(AppPaths.agentLabel): \(describe(agent.status))")
        return 0
    }
}
