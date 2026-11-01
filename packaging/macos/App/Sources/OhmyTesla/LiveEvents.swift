import Foundation

/// Follows a hub: its SSE stream `GET /api/live/stream`, and its alert history
/// for native notifications.
///
/// Alerts: the hub records every alert in its history (`GET /api/alerts/history`,
/// rows carry an `id`) and also emits `{"type": "alert", ...}` on the SSE stream.
/// The client
///   * treats an SSE alert as a nudge to read the history right away, so each
///     alert is notified exactly once, keyed by its history id;
///   * reads the history when the stream (re)connects, so nothing missed while
///     disconnected is lost, and at most every 10 min on a keepalive (older hubs
///     that emit no alert events) — no timer of its own;
///   * remembers the highest id per server, so nothing is repeated across
///     launches, and the first contact with a server notifies nothing;
///   * skips rows the hub itself kept in-app (channels == ["app"]: snoozed or
///     quiet hours), and falls back to the SSE payload only if the history has
///     no new row for it.
///
/// Car events are not kept or published: the menu-bar panel asks for a nudge
/// (`onActivity`) only while it is open, so a driving car costs the app nothing.
@MainActor
final class LiveEvents: ObservableObject {
    @Published private(set) var connected = false
    @Published private(set) var lastError: String?

    var onAlert: ((Alert) -> Void)?
    /// Called on every (re)connection.
    var onConnect: (() -> Void)?
    /// Set while someone wants to know that a car's data or an alert just arrived.
    var onActivity: ((Activity) -> Void)?

    enum Activity { case car(String), alert }

    struct Alert {
        let id: String
        let title: String
        let body: String
        let carName: String?
        let level: String
        let path: String?
    }

    private var base: URL?
    private var password = ""
    private var streamTask: Task<Void, Never>?
    private var notified: [String] = []
    private var lastPoll = Date.distantPast
    private static let safetyPoll: TimeInterval = 600
    private let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 90          // the hub sends a keepalive every 25 s
        c.timeoutIntervalForResource = 365 * 24 * 3600
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: c)
    }()

    func start(base: URL, password: String) {
        if self.base == base, self.password == password, streamTask != nil { return }
        stop()
        self.base = base
        self.password = password
        streamTask = Task { [weak self] in await self?.streamLoop() }
    }

    func stop() {
        streamTask?.cancel()
        streamTask = nil
        connected = false
    }

    private func request(_ path: String) -> URLRequest? {
        guard let base else { return nil }
        return HubEndpoint(base: base, password: password).request(path, timeout: 90)
    }

    // MARK: SSE

    private func streamLoop() async {
        var backoff: UInt64 = 1
        while !Task.isCancelled {
            do {
                guard var req = request("api/live/stream") else { return }
                req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                let (bytes, response) = try await session.bytes(for: req)
                guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                    let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                    throw URLError(.badServerResponse, userInfo: [NSLocalizedDescriptionKey: "HTTP \(code)"])
                }
                connected = true
                lastError = nil
                backoff = 1
                onConnect?()
                await pollOnce()                      // whatever happened while we were away
                var line = Data()
                var eventName = ""
                var dataLines: [String] = []
                for try await byte in bytes {
                    if byte != 0x0A {
                        line.append(byte)
                        continue
                    }
                    var text = String(decoding: line, as: UTF8.self)
                    line.removeAll(keepingCapacity: true)
                    if text.hasSuffix("\r") { text.removeLast() }
                    if text.isEmpty {
                        if !dataLines.isEmpty { handle(event: eventName, data: dataLines.joined(separator: "\n")) }
                        eventName = ""
                        dataLines.removeAll()
                    } else if text.hasPrefix(":") {
                        // keepalive comment (every 25 s while nothing happens)
                        if Date().timeIntervalSince(lastPoll) > Self.safetyPoll { await pollOnce() }
                    } else if text.hasPrefix("data:") {
                        dataLines.append(String(text.dropFirst(5)).trimmingLeadingSpace())
                    } else if text.hasPrefix("event:") {
                        eventName = String(text.dropFirst(6)).trimmingLeadingSpace()
                    }
                }
            } catch is CancellationError {
                break
            } catch {
                if Task.isCancelled { break }
                lastError = error.localizedDescription
            }
            connected = false
            try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
            backoff = min(backoff * 2, 30)
        }
        connected = false
    }

    private func handle(event: String, data: String) {
        // Most events are one car value: {"car_id": "1", "key": ..., "value": ...}.
        // Only parse them when someone listens.
        if event != "alert", !data.contains("\"type\"") {
            guard let onActivity,
                  let obj = try? JSONSerialization.jsonObject(with: Data(data.utf8)) as? [String: Any],
                  let car = obj["car_id"].map({ "\($0)" }) else { return }
            onActivity(.car(car))
            return
        }
        guard let obj = try? JSONSerialization.jsonObject(with: Data(data.utf8)) as? [String: Any] else { return }
        let type = obj["type"] as? String
        if event == "alert" || type == "alert" {
            let payload = (obj["alert"] as? [String: Any]) ?? obj
            onActivity?(.alert)
            Task { [weak self] in
                guard let self else { return }
                let delivered = await self.pollOnce()
                if delivered == 0, !self.recentlyDelivered(payload), !self.keptInApp(payload) {
                    self.deliver(payload)
                }
            }
        } else if type == nil, let car = obj["car_id"].map({ "\($0)" }) {
            onActivity?(.car(car))
        }
    }

    // MARK: alert history

    private var watermarkKey: String { "alertWatermark:\(base?.absoluteString ?? "")" }

    /// Returns how many alerts were delivered.
    @discardableResult
    private func pollOnce() async -> Int {
        lastPoll = Date()
        guard let req = request("api/alerts/history?limit=20"),
              let (data, response) = try? await session.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return 0 }
        let ids = rows.compactMap { ($0["id"] as? NSNumber)?.intValue }
        let defaults = UserDefaults.standard
        guard defaults.object(forKey: watermarkKey) != nil else {
            // First contact with this server: remember where history ends, notify nothing.
            defaults.set(ids.max() ?? 0, forKey: watermarkKey)
            return 0
        }
        let mark = defaults.integer(forKey: watermarkKey)
        let fresh = rows.filter { (($0["id"] as? NSNumber)?.intValue ?? 0) > mark }
            .sorted { (($0["id"] as? NSNumber)?.intValue ?? 0) < (($1["id"] as? NSNumber)?.intValue ?? 0) }
        if let top = ids.max(), top > mark { defaults.set(top, forKey: watermarkKey) }
        var n = 0
        for row in fresh {
            if keptInApp(row) { remember(row); continue }
            if deliver(row) { n += 1 }
        }
        return n
    }

    /// The hub stores `channels` as a JSON list; ["app"] means it chose not to push.
    /// The live event says the same with `in_app_only`.
    private func keptInApp(_ row: [String: Any]) -> Bool {
        if let inApp = row["in_app_only"] as? Bool { return inApp }
        guard let raw = row["channels"] as? String, let data = raw.data(using: .utf8),
              let list = try? JSONSerialization.jsonObject(with: data) as? [String] else { return false }
        return list == ["app"]
    }

    private var recent: [String: Date] = [:]

    private func key(_ row: [String: Any]) -> String {
        "\(row["car_id"].map { "\($0)" } ?? "")|\(row["rule"] as? String ?? "")|\(row["title"] as? String ?? "")"
    }

    private func remember(_ row: [String: Any]) {
        let now = Date()
        recent = recent.filter { now.timeIntervalSince($0.value) < 300 }
        recent[key(row)] = now
    }

    private func recentlyDelivered(_ row: [String: Any]) -> Bool {
        guard let at = recent[key(row)] else { return false }
        return Date().timeIntervalSince(at) < 120
    }

    @discardableResult
    private func deliver(_ row: [String: Any]) -> Bool {
        let id = row["id"].flatMap { $0 is NSNull ? nil : "\($0)" } ?? UUID().uuidString
        guard !notified.contains(id) else { return false }
        notified.append(id)
        remember(row)
        if notified.count > 200 { notified.removeFirst(100) }
        if let n = (row["id"] as? NSNumber)?.intValue, n > UserDefaults.standard.integer(forKey: watermarkKey) {
            UserDefaults.standard.set(n, forKey: watermarkKey)
        }
        let alert = Alert(id: id,
                          title: (row["title"] as? String) ?? "OhmyTesla",
                          body: (row["body"] as? String) ?? "",
                          carName: row["car_name"] as? String,
                          level: (row["level"] as? String) ?? "info",
                          path: row["url"] as? String)
        onAlert?(alert)
        return true
    }
}

private extension String {
    func trimmingLeadingSpace() -> String {
        hasPrefix(" ") ? String(dropFirst()) : self
    }
}
