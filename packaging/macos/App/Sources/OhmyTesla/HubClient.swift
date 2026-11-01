import Foundation

/// A hub and how to sign in to it: HTTP Basic, user `ohmytesla`, the portal password.
struct HubEndpoint: Equatable {
    let base: URL
    let password: String

    func request(_ path: String, timeout: TimeInterval = 8) -> URLRequest? {
        guard let url = URL(string: path, relativeTo: base) else { return nil }
        var r = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        let token = Data("\(AppPaths.portalUser):\(password)".utf8).base64EncodedString()
        r.setValue("Basic \(token)", forHTTPHeaderField: "Authorization")
        return r
    }
}

enum HubError: Error, Equatable {
    case unreachable(String)
    case unauthorized
    case status(Int)
    case badData
}

/// JSON calls against the same read-only endpoints the web UI uses.
enum HubAPI {
    static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 8
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        c.httpMaximumConnectionsPerHost = 4
        return URLSession(configuration: c)
    }()

    static func json(_ path: String, from hub: HubEndpoint) async throws -> Any {
        guard let req = hub.request(path) else { throw HubError.badData }
        return try await send(req)
    }

    static func object(_ path: String, from hub: HubEndpoint) async throws -> JSON {
        guard let o = JSON(try await json(path, from: hub)) else { throw HubError.badData }
        return o
    }

    static func array(_ path: String, from hub: HubEndpoint) async throws -> [JSON] {
        guard let a = try await json(path, from: hub) as? [Any] else { throw HubError.badData }
        return a.compactMap(JSON.init)
    }

    /// POST a JSON body; returns the decoded answer.
    static func post(_ path: String, body: [String: Any], to hub: HubEndpoint) async throws -> Any {
        guard var req = hub.request(path) else { throw HubError.badData }
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(req)
    }

    private static func send(_ req: URLRequest) async throws -> Any {
        let data: Data, response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw HubError.unreachable(error.localizedDescription)
        }
        switch (response as? HTTPURLResponse)?.statusCode ?? 0 {
        case 200..<300: break
        case 401: throw HubError.unauthorized
        case let code: throw HubError.status(code)
        }
        guard let obj = try? JSONSerialization.jsonObject(with: data) else { throw HubError.badData }
        return obj
    }
}

/// A forgiving view of a JSON object: the hub sends numbers as numbers or strings,
/// and three-state booleans (true / false / null = unknown).
struct JSON {
    let raw: [String: Any]

    init?(_ any: Any?) {
        guard let d = any as? [String: Any] else { return nil }
        raw = d
    }

    func string(_ key: String) -> String? {
        if let s = raw[key] as? String { return s.isEmpty ? nil : s }
        if let n = raw[key] as? NSNumber { return n.stringValue }
        return nil
    }

    func double(_ key: String) -> Double? {
        if let n = raw[key] as? NSNumber { return n.doubleValue }
        if let s = raw[key] as? String { return Double(s) }
        return nil
    }

    func int(_ key: String) -> Int? { double(key).map { Int($0.rounded()) } }

    /// nil = not known (never shown as "false").
    func bool(_ key: String) -> Bool? {
        switch raw[key] {
        case let b as Bool: return b
        case let s as String: return ["true", "1"].contains(s.lowercased()) ? true
            : ["false", "0"].contains(s.lowercased()) ? false : nil
        default: return nil
        }
    }

    func object(_ key: String) -> JSON? { JSON(raw[key]) }
}
