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

    /// The supervisor rewrites the file at least every 5 s while alive.
    var isFresh: Bool {
        guard let pid, let updated else { return false }
        return kill(pid_t(pid), 0) == 0 && Date().timeIntervalSince1970 - updated < 20
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
    @Published private(set) var notice: String?

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
            startChild(reason: "OhmyTesla is running from a temporary, read-only location (App Translocation). "
                + "Move it to the Applications folder and open it again to keep monitoring with the app closed.")
            return
        }
        switch agent.status {
        case .enabled:
            runMode = .agent
            notice = nil
        case .requiresApproval:
            startChild(reason: "Allow OhmyTesla under System Settings › General › Login Items so monitoring "
                + "continues when the app is closed. Until then the services run only while the app is open.")
        case .notRegistered, .notFound:
            fallthrough
        @unknown default:
            do {
                try agent.register()
                if agent.status == .requiresApproval {
                    startChild(reason: "Allow OhmyTesla under System Settings › General › Login Items so "
                        + "monitoring continues when the app is closed.")
                } else {
                    runMode = .agent
                    notice = nil
                }
            } catch {
                startChild(reason: "Could not install the background service (\(error.localizedDescription)). "
                    + "The services run only while the app is open.")
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

    private func startChild(reason: String?) {
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
            notice = "Could not start the services: \(error.localizedDescription)"
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
