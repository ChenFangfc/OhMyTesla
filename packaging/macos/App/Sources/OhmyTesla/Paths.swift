import Foundation

/// Locations shared with supervisor.py. OHMYTESLA_HOME / OHMYTESLA_LOGS override
/// them for tests (the app passes its environment on to a child supervisor; a
/// LaunchAgent-started supervisor always uses the defaults).
enum AppPaths {
    static let agentLabel = "io.github.ohmytesla.supervisor"
    static let agentPlistName = "io.github.ohmytesla.supervisor.plist"
    static let portalUser = "ohmytesla"

    private static func override(_ key: String) -> URL? {
        guard let v = ProcessInfo.processInfo.environment[key], !v.isEmpty else { return nil }
        return URL(fileURLWithPath: v, isDirectory: true)
    }

    static var home: URL {
        override("OHMYTESLA_HOME") ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/OhmyTesla", isDirectory: true)
    }

    static var logs: URL {
        override("OHMYTESLA_LOGS") ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/OhmyTesla", isDirectory: true)
    }

    static var state: URL { home.appendingPathComponent("state.json") }
    static var control: URL { home.appendingPathComponent("control.json") }
    static var config: URL { home.appendingPathComponent("config.env") }

    static var runtime: URL {
        Bundle.main.resourceURL!.appendingPathComponent("runtime", isDirectory: true)
    }
    static var python: URL { runtime.appendingPathComponent("python/bin/python3") }
    static var supervisorScript: URL { runtime.appendingPathComponent("supervisor/supervisor.py") }

    /// KEY=VALUE pairs from config.env (0600, written by the supervisor).
    static func readConfig() -> [String: String] {
        guard let text = try? String(contentsOf: config, encoding: .utf8) else { return [:] }
        var out: [String: String] = [:]
        for raw in text.split(whereSeparator: \.isNewline) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.hasPrefix("#"), let eq = line.firstIndex(of: "=") else { continue }
            var key = String(line[..<eq]).trimmingCharacters(in: .whitespaces)
            if key.hasPrefix("export ") { key = String(key.dropFirst(7)) }
            var value = String(line[line.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            if value.count >= 2, let f = value.first, f == value.last, f == "\"" || f == "'" {
                value = String(value.dropFirst().dropLast())
            }
            out[key] = value
        }
        return out
    }
}
