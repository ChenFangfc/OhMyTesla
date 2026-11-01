import Foundation

/// What the menu-bar panel shows. Fetched from the hub (the endpoints the web UI
/// uses) when the panel opens, refreshed while it stays open — on the live event
/// stream and every 30 s — and never while it is closed.
@MainActor
final class PanelModel: ObservableObject {
    struct CarChoice: Identifiable, Equatable {
        let id: Int
        let name: String
    }

    struct Snapshot {
        var overview: JSON?
        var cars: [CarChoice] = []          // not archived
        var latestAlert: JSON?
        var snooze: JSON?
        var dataStoppedSeconds: Double?     // the hub's watchdog: no data although the car should report
        var miles = false
        var fahrenheit = false
        var fetched = Date()
    }

    enum Problem: Equatable { case unreachable, unauthorized }

    @Published private(set) var snapshot: Snapshot?
    @Published private(set) var problem: Problem?
    @Published private(set) var loading = false
    @Published private(set) var now = Date()
    @Published private(set) var snoozeBusy = false
    @Published private(set) var selectedCar: Int?

    private weak var app: AppModel?
    private(set) var isOpen = false
    private var ticker: Timer?
    private var fetchTask: Task<Void, Never>?
    private var nudge: Task<Void, Never>?
    private var fetchedFrom: URL?

    init(app: AppModel) {
        self.app = app
        selectedCar = UserDefaults.standard.object(forKey: "panelCar") as? Int
    }

    // MARK: open / close

    func opened() {
        guard !isOpen else { return }
        isOpen = true
        now = Date()
        if fetchedFrom != app?.endpoint?.base { snapshot = nil; problem = nil }
        app?.live.onActivity = { [weak self] _ in self?.liveActivity() }
        reload()
        ticker = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.now = Date()
                self?.reload()
            }
        }
    }

    func closed() {
        guard isOpen else { return }
        isOpen = false
        ticker?.invalidate()
        ticker = nil
        nudge?.cancel()
        nudge = nil
        app?.live.onActivity = nil
    }

    func reload() {
        fetchTask?.cancel()
        fetchTask = Task { [weak self] in await self?.fetch() }
    }

    func select(car id: Int) {
        guard id != selectedCar else { return }
        selectedCar = id
        UserDefaults.standard.set(id, forKey: "panelCar")
        reload()
    }

    /// The car's data or an alert changed: refetch, at most every 2 s.
    private func liveActivity() {
        guard isOpen, nudge == nil else { return }
        nudge = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard let self, !Task.isCancelled else { return }
            self.nudge = nil
            self.reload()
        }
    }

    // MARK: fetching

    private func fetch() async {
        guard let app, let hub = app.endpoint else {
            snapshot = nil
            problem = nil
            return
        }
        loading = true
        defer { loading = false }
        let tz = TimeZone.current.secondsFromGMT() / 60
        let carQuery = selectedCar.map { "car_id=\($0)&" } ?? ""
        async let prefs = try? HubAPI.object("api/prefs", from: hub)
        async let cars = try? HubAPI.array("api/cars", from: hub)
        async let alerts = try? HubAPI.array("api/alerts/history?limit=1", from: hub)
        async let snooze = try? HubAPI.object("api/alerts/snooze", from: hub)
        async let health = try? HubAPI.object("api/health/detail", from: hub)
        let overview: JSON
        do {
            overview = try await HubAPI.object("api/overview?\(carQuery)tz_offset_min=\(tz)", from: hub)
        } catch {
            guard !Task.isCancelled else { return }
            problem = (error as? HubError) == .unauthorized ? .unauthorized : .unreachable
            return
        }
        let p = await prefs, c = await cars, a = await alerts, s = await snooze, h = await health
        guard !Task.isCancelled else { return }

        app.adoptLanguage(p?.string("locale"))
        var snap = Snapshot()
        snap.overview = overview
        snap.cars = (c ?? []).filter { $0.bool("archived") != true }.compactMap { row in
            row.int("id").map { CarChoice(id: $0, name: row.string("name") ?? row.string("label") ?? "#\($0)") }
        }
        snap.latestAlert = a?.first
        snap.snooze = s ?? snapshot?.snooze
        if let d = h?.object("data"), d.bool("stale") == true {
            snap.dataStoppedSeconds = d.double("seconds_since_data")
        }
        snap.miles = p?.string("length") == "mi"
        snap.fahrenheit = p?.string("temp") == "F"
        if p == nil, let old = snapshot { snap.miles = old.miles; snap.fahrenheit = old.fahrenheit }
        snapshot = snap
        problem = nil
        fetchedFrom = hub.base
        now = Date()
        // A car that left the account (or was never there) is no longer offered.
        if let sel = selectedCar, !snap.cars.isEmpty, !snap.cars.contains(where: { $0.id == sel }) {
            selectedCar = nil
            UserDefaults.standard.removeObject(forKey: "panelCar")
        }
    }

    // MARK: alert snooze (the hub's own: pauses non-critical pushes everywhere)

    func setSnooze(minutes: Int) {
        guard let hub = app?.endpoint, !snoozeBusy else { return }
        snoozeBusy = true
        Task {
            defer { snoozeBusy = false }
            let answer = try? await HubAPI.post("api/alerts/snooze",
                                                body: ["minutes": minutes, "scope": "noncritical"], to: hub)
            if let state = JSON(answer) { snapshot?.snooze = state }
        }
    }
}

// MARK: - presentation

/// Everything the car block says, in the current language and units.
struct CarSummary {
    enum Freshness: Equatable {
        case live
        case updated(String)
        case problem(String)
    }

    var name: String
    var sentence: String
    var freshness: Freshness?
    var soc: Int?
    var range: String?
    var charging: Bool
    var chargeLine: String?
    var chargeLimit: Int?
    var locked: Bool?
    var sentry: Bool?
    var climate: Bool?
    var driving: Bool
    var archived: Bool
}

struct AlertLine {
    let time: String
    let text: String
    let level: String
    let path: String
}

extension PanelModel.Snapshot {
    func summary(now: Date) -> CarSummary? {
        guard let o = overview, o.bool("connected") == true else { return nil }
        let state = o.string("state") ?? ""
        let archived = o.bool("archived") == true
        let place = o.string("place")
        let charge = o.object("charging")
        let charging = charge?.bool("active") == true || state == "charging"

        var parts: [String] = []
        if archived {
            parts.append(tr("已归档", "Archived"))
        } else if state == "driving" {
            parts.append(tr("行驶中", "Driving"))
        } else if charging {
            parts.append(place.map { tr("在\($0)充电", "Charging at \($0)") } ?? tr("充电中", "Charging"))
        } else if let place {
            parts.append(tr("停在\(place)", "Parked at \(place)"))
        } else if ["asleep", "offline", "online", "suspended"].contains(state) {
            parts.append(tr("停着", "Parked"))
        } else if state == "updating" {
            parts.append(tr("正在更新软件", "Updating software"))
        }
        if let t = o.double("inside_temp") {
            parts.append(tr("车内 \(temperature(t))", "\(temperature(t)) inside"))
        }

        var chargeLine: String?
        var limit: Int?
        if charging, !archived {
            limit = charge?.int("limit_soc")
            var line = charge?.double("power_kw").map { tr("正在充电 · \(Int($0.rounded())) kW", "Charging at \(Int($0.rounded())) kW") }
                ?? tr("正在充电", "Charging")
            if let mins = charge?.int("minutes_remaining"), mins > 0 {
                let t = Fmt.duration(minutes: mins), n = limit ?? 100
                line += " · " + tr("\(t)后充到 \(n)%", "\(n)% in \(t)")
            } else if let limit {
                line += " · " + tr("上限 \(limit)%", "limit \(limit)%")
            }
            chargeLine = line
        }

        return CarSummary(
            name: o.string("name") ?? o.string("model") ?? tr("车辆", "Vehicle"),
            sentence: parts.joined(separator: " · "),
            freshness: freshness(o, state: state, archived: archived, now: now),
            soc: o.double("soc").map { Int($0.rounded()) },
            range: o.double("range_km").map(distance),
            charging: charging && !archived,
            chargeLine: chargeLine,
            chargeLimit: limit,
            locked: archived ? nil : o.bool("locked"),
            sentry: archived ? nil : o.bool("sentry"),
            climate: archived ? nil : o.bool("climate"),
            driving: state == "driving" && !archived,
            archived: archived)
    }

    /// "Live" only while the car is awake and its data streams in; otherwise how
    /// old the shown state is (the web home page says the same).
    private func freshness(_ o: JSON, state: String, archived: Bool, now: Date) -> CarSummary.Freshness? {
        let elapsed = now.timeIntervalSince(fetched)
        if archived {
            guard let seen = o.string("last_seen").flatMap(parseISODate) else { return nil }
            return .updated(tr("最后在线 \(Fmt.ago(now.timeIntervalSince(seen)))",
                               "Last seen \(Fmt.ago(now.timeIntervalSince(seen)))"))
        }
        if let stopped = dataStoppedSeconds {
            let m = Int((stopped + elapsed) / 60)
            return .problem(tr("数据已中断 \(m) 分钟", "Data stopped \(m) min ago"))
        }
        if let seeded = o.double("stale_seconds") { return .updated(updated(seeded + elapsed)) }
        if ["online", "driving", "charging", "updating"].contains(state) { return .live }
        if let since = o.string("since").flatMap(parseISODate) { return .updated(updated(now.timeIntervalSince(since))) }
        return nil
    }

    private func updated(_ seconds: TimeInterval) -> String {
        seconds < 60 ? tr("刚刚更新", "Updated just now")
            : tr("\(Fmt.ago(seconds))更新", "Updated \(Fmt.ago(seconds))")
    }

    func temperature(_ celsius: Double) -> String {
        let v = fahrenheit ? celsius * 9 / 5 + 32 : celsius
        return "\(Int(v.rounded()))°"
    }

    func distance(_ km: Double) -> String {
        miles ? "\(Int((km * 0.621371).rounded())) mi" : "\(Int(km.rounded())) km"
    }

    var snoozedUntil: Date? {
        guard let s = snooze, s.bool("active") == true, let until = s.double("until") else { return nil }
        return Date(timeIntervalSince1970: until)
    }

    var snoozeIncludesAlarms: Bool { snooze?.string("scope") == "all" }

    func alertLine(now: Date) -> AlertLine? {
        guard let a = latestAlert, let ts = a.double("ts") else { return nil }
        var text = Fmt.withoutEmoji(a.string("title") ?? "")
        if cars.count <= 1, let car = a.string("car_name") {
            for sep in ["：", ": ", " — ", " – ", " - "] where text.hasPrefix(car + sep) {
                text = String(text.dropFirst(car.count + sep.count))
                break
            }
        }
        // The body is "place · time" (or a detail); its first part adds where.
        if let first = a.string("body")?.components(separatedBy: " · ").first?.trimmingCharacters(in: .whitespaces),
           !first.isEmpty, first.range(of: #"^\d{1,2}:\d{2}"#, options: .regularExpression) == nil, first != text {
            text = text.isEmpty ? first : "\(text) · \(first)"
        }
        guard !text.isEmpty else { return nil }
        if L10n.lang == .en, let first = text.first, first.isLowercase { text = first.uppercased() + text.dropFirst() }
        let path = a.string("url").flatMap { $0.hasPrefix("/") ? $0 : nil } ?? "/security"
        return AlertLine(time: Fmt.clock(Date(timeIntervalSince1970: ts), now: now), text: text,
                         level: a.string("level") ?? "info", path: path)
    }
}

/// TeslaMate / hub timestamps: "2026-10-31T22:53:20.319566+00:00" (any fraction length).
func parseISODate(_ s: String) -> Date? {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = f.date(from: s) { return d }
    f.formatOptions = [.withInternetDateTime]
    let trimmed = s.replacingOccurrences(of: #"\.\d+"#, with: "", options: .regularExpression)
    return f.date(from: trimmed)
}
