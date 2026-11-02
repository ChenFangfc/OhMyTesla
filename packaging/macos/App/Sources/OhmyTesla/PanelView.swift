import AppKit
import SwiftUI

/// The menu-bar panel: the car at a glance, a few quick settings, nothing else.
/// System font, SF Symbols, tabular figures; the accent colour only for what is
/// live this moment (charging, Sentry watching, climate running).
struct PanelView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var panel: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            switch model.mode {
            case .unset:
                SetupPrompt()
            case .embedded, .remote:
                CarBlock()
                if model.endpoint != nil, panel.snapshot != nil {
                    PanelDivider()
                    AlertBlock()
                }
                PanelDivider()
                SettingsBlock()
                if model.mode == .embedded {
                    PanelDivider()
                    ServicesBlock()
                }
            }
            PanelDivider()
            FooterBlock()
        }
        .padding(.vertical, 6)
        .frame(width: 320)
        .background(PanelVisibility { visible in visible ? panel.opened() : panel.closed() })
        .environment(\.locale, L10n.lang.locale)
    }
}

// MARK: - car

private struct CarBlock: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var panel: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let s = panel.snapshot?.summary(now: panel.now), let snap = panel.snapshot {
                header(s, snap)
                if let problem = problemText { Note(text: problem, symbol: "exclamationmark.triangle.fill", tint: .orange) }
                battery(s)
                states(s)
            } else if let text = waitingText {
                header(placeholder: text.title, detail: text.detail)
                if model.mode == .remote, panel.problem != nil {
                    PanelButton(title: tr("重试", "Try Again"), symbol: "arrow.clockwise") { panel.reload() }
                } else if text.openApp {
                    PanelButton(title: tr("打开 OhmyTesla", "Open OhmyTesla"), symbol: "macwindow") {
                        AppDelegate.shared?.showMainWindow()
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 8)
        .padding(.bottom, 10)
    }

    private var problemText: String? {
        switch panel.problem {
        case .unauthorized: return tr("服务器拒绝了密码", "The server rejected the password")
        case .unreachable: return tr("连不上服务器，显示的是上次的数据", "Can't reach the server — showing the last data")
        case nil: return nil
        }
    }

    /// Why there is no car to show yet.
    private var waitingText: (title: String, detail: String?, openApp: Bool)? {
        if model.mode == .embedded, !model.hubReady {
            return (model.statusLine, tr("服务就绪后显示车辆状态", "Vehicle status appears once the services are running"), false)
        }
        switch panel.problem {
        case .unauthorized:
            return (tr("服务器拒绝了密码", "The server rejected the password"),
                    tr("在“更多 › 更换服务器…”中重新输入", "Enter it again under More › Change Server…"), false)
        case .unreachable:
            return (tr("连不上服务器", "Can't reach the server"), model.remoteURL?.host, false)
        case nil:
            break
        }
        if panel.snapshot != nil {
            return (tr("还没有车辆数据", "No vehicle data yet"),
                    tr("在 OhmyTesla 中登录 Tesla 账号后开始记录", "Sign in to Tesla in OhmyTesla to start logging"), true)
        }
        return (tr("正在载入…", "Loading…"), nil, false)
    }

    private func header(_ s: CarSummary, _ snap: PanelModel.Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if snap.cars.count > 1 {
                    Menu {
                        ForEach(snap.cars) { car in
                            Button {
                                panel.select(car: car.id)
                            } label: {
                                if car.id == (panel.selectedCar ?? snap.overview?.int("car_id")) {
                                    Label(car.name, systemImage: "checkmark")
                                } else {
                                    Text(car.name)
                                }
                            }
                        }
                    } label: {
                        Text(s.name).font(.system(size: 15, weight: .semibold))
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    .help(tr("选择车辆", "Choose vehicle"))
                } else {
                    Text(s.name).font(.system(size: 15, weight: .semibold)).lineLimit(1)
                }
                Spacer(minLength: 8)
                freshness(s.freshness)
            }
            if !s.sentence.isEmpty {
                Text(s.sentence)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
    }

    private func header(placeholder: String, detail: String?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(placeholder).font(.system(size: 15, weight: .semibold))
            if let detail {
                Text(detail).font(.system(size: 12)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.bottom, 6)
    }

    @ViewBuilder
    private func freshness(_ f: CarSummary.Freshness?) -> some View {
        switch f {
        case .live:
            HStack(spacing: 4) {
                Circle().fill(Color.accentColor).frame(width: 6, height: 6)
                Text(tr("实时", "Live"))
            }
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(Color.accentColor)
        case .updated(let text):
            Text(text).font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit().lineLimit(1)
        case .problem(let text):
            Text(text).font(.system(size: 11, weight: .medium)).foregroundStyle(.orange).monospacedDigit().lineLimit(1)
        case nil:
            EmptyView()
        }
    }

    private func battery(_ s: CarSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 0) {
                Text(s.soc.map(String.init) ?? "–")
                    .font(.system(size: 32, weight: .regular))
                    .monospacedDigit()
                Text("%")
                    .font(.system(size: 15))
                    .foregroundStyle(.secondary)
                    .padding(.leading, 1)
                Spacer()
                if let range = s.range {
                    Text(tr("续航", "Range"))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .padding(.trailing, 5)
                    Text(range).font(.system(size: 15)).monospacedDigit()
                }
            }
            BatteryBar(fraction: Double(s.soc ?? 0) / 100, limit: s.chargeLimit.map { Double($0) / 100 }, live: s.charging)
                .frame(height: 4)
                .accessibilityLabel(tr("电量 \(s.soc ?? 0)%", "Battery \(s.soc ?? 0)%"))
            if let line = s.chargeLine {
                Label(line, systemImage: "bolt.fill")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Color.accentColor)
                    .monospacedDigit()
                    .lineLimit(1)
            }
        }
        .padding(.top, 12)
    }

    @ViewBuilder
    private func states(_ s: CarSummary) -> some View {
        let cells = stateCells(s)
        if !cells.isEmpty {
            // Three fixed columns, so a car that reports only some states keeps the grid.
            HStack(spacing: 6) {
                ForEach(cells, id: \.label) { cell in
                    StateCell(cell: cell).frame(width: 92)
                }
                Spacer(minLength: 0)
            }
            .padding(.top, 12)
        }
    }

    private func stateCells(_ s: CarSummary) -> [StateCell.Model] {
        var out: [StateCell.Model] = []
        if let locked = s.locked {
            // Unlocked while parked is worth a second look; while driving it is normal.
            out.append(.init(symbol: locked ? "lock.fill" : "lock.open.fill", label: tr("车门", "Doors"),
                             value: locked ? tr("已锁", "Locked") : tr("未锁", "Unlocked"),
                             tint: !locked && !s.driving ? .orange : nil))
        }
        if let sentry = s.sentry {
            out.append(.init(symbol: sentry ? "checkmark.shield.fill" : "shield.slash", label: tr("哨兵", "Sentry"),
                             value: sentry ? tr("已开启", "On") : tr("已关闭", "Off"),
                             tint: sentry ? .accentColor : nil))
        }
        if let climate = s.climate {
            out.append(.init(symbol: climate ? "fan" : "fan.slash", label: tr("空调", "Climate"),
                             value: climate ? tr("已开启", "On") : tr("已关闭", "Off"),
                             tint: climate ? .accentColor : nil))
        }
        return out
    }
}

private struct StateCell: View {
    struct Model {
        let symbol: String
        let label: String
        let value: String
        let tint: Color?
    }

    let cell: Model

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
                Image(systemName: cell.symbol)
                    .font(.system(size: 11))
                    .foregroundStyle(cell.tint ?? .secondary)
                    .frame(width: 14)
                Text(cell.label).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Text(cell.value)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(cell.tint ?? .primary)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.05)))
    }
}

private struct BatteryBar: View {
    let fraction: Double
    let limit: Double?
    let live: Bool

    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2).fill(Color.primary.opacity(0.1))
                RoundedRectangle(cornerRadius: 2)
                    .fill(live ? Color.accentColor : Color.primary.opacity(0.55))
                    .frame(width: max(0, min(1, fraction)) * g.size.width)
                if let limit, limit > 0, limit < 1 {
                    Rectangle().fill(Color.primary.opacity(0.45))
                        .frame(width: 1.5, height: g.size.height + 4)
                        .offset(x: limit * g.size.width - 0.75)
                }
            }
        }
    }
}

// MARK: - latest alert

private struct AlertBlock: View {
    @EnvironmentObject var panel: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            SectionLabel(text: tr("最近提醒", "Latest alert"))
            if let line = panel.snapshot?.alertLine(now: panel.now) {
                RowButton(action: { AppDelegate.shared?.showMainWindow(path: line.path) }) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Circle().fill(dot(line.level)).frame(width: 6, height: 6)
                            .alignmentGuide(.firstTextBaseline) { $0[.bottom] - 1 }
                        Text(line.time)
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                        Text(line.text).font(.system(size: 13)).lineLimit(1).truncationMode(.tail)
                        Spacer(minLength: 0)
                    }
                }
                .help(tr("在 OhmyTesla 中查看", "Show in OhmyTesla"))
            } else {
                Text(tr("暂无提醒", "No alerts yet"))
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 3)
            }
        }
        .padding(.vertical, 6)
    }

    private func dot(_ level: String) -> Color {
        switch level {
        case "critical": return .red
        case "warning": return .orange
        default: return Color.secondary.opacity(0.6)
        }
    }
}

// MARK: - quick settings

private struct SettingsBlock: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var panel: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if model.endpoint != nil, let snap = panel.snapshot {
                snoozeRow(snap)
            }
            ToggleRow(title: tr("Mac 通知", "Mac notifications"), isOn: $model.notificationsEnabled)
            if model.mode == .embedded {
                ToggleRow(title: tr("哨兵开启时保持 Mac 唤醒", "Keep Mac awake during Sentry"),
                          detail: model.state?.keepAwake?.active == true
                              ? tr("哨兵已开启，正在保持唤醒", "Sentry is on — keeping this Mac awake") : nil,
                          isOn: Binding(get: { model.keepAwake }, set: { model.setKeepAwake($0) }))
            }
            ToggleRow(title: tr("登录时打开", "Open at login"),
                      isOn: Binding(get: { model.openAtLogin }, set: { model.setOpenAtLogin($0) }))
        }
        .padding(.vertical, 4)
    }

    private func snoozeRow(_ snap: PanelModel.Snapshot) -> some View {
        let until = snap.snoozedUntil
        return VStack(alignment: .leading, spacing: 1) {
            HStack {
                Text(tr("暂停提醒", "Pause alerts")).font(.system(size: 13))
                Spacer()
                Menu {
                    Button(tr("1 小时", "1 hour")) { panel.setSnooze(minutes: 60) }
                    Button(tr("8 小时", "8 hours")) { panel.setSnooze(minutes: 480) }
                    Button(tr("24 小时", "24 hours")) { panel.setSnooze(minutes: 1440) }
                    if until != nil {
                        Divider()
                        Button(tr("恢复提醒", "Resume alerts")) { panel.setSnooze(minutes: 0) }
                    }
                } label: {
                    Text(until.map { tr("至 \(Fmt.clock($0, now: panel.now))", "Until \(Fmt.clock($0, now: panel.now))") }
                         ?? tr("关", "Off"))
                        .font(.system(size: 13))
                        .monospacedDigit()
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .disabled(panel.snoozeBusy)
            }
            if until != nil {
                Text(snap.snoozeIncludesAlarms ? tr("包括入侵提醒", "Including intrusion alerts")
                                               : tr("入侵提醒仍会推送", "Intrusion alerts still come through"))
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 5)
    }
}

private struct ToggleRow: View {
    let title: String
    var detail: String? = nil
    @Binding var isOn: Bool

    var body: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 13))
                if let detail {
                    Text(detail).font(.system(size: 11)).foregroundStyle(Color.accentColor)
                }
            }
            Spacer(minLength: 12)
            Toggle("", isOn: $isOn)
                .toggleStyle(.switch)
                .controlSize(.mini)
                .labelsHidden()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 5)
    }
}

// MARK: - services (embedded)

private struct ServicesBlock: View {
    @EnvironmentObject var model: AppModel
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            RowButton(action: { expanded.toggle() }) {
                HStack(spacing: 8) {
                    Circle().fill(model.serviceHealth.color).frame(width: 7, height: 7)
                    Text(model.statusLine).font(.system(size: 13)).lineLimit(1)
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .rotationEffect(.degrees(expanded || model.serviceHealth.needsAttention ? 90 : 0))
                }
            }
            if expanded || model.serviceHealth.needsAttention {
                VStack(alignment: .leading, spacing: 3) {
                    if model.stateFresh, let state = model.state {
                        ForEach(SupervisorState.order, id: \.self) { name in
                            if let svc = state.services[name], svc.state != "disabled" {
                                HStack {
                                    Text(ServiceText.name(name)).font(.system(size: 12))
                                    Spacer()
                                    Text(ServiceText.state(svc.state))
                                        .font(.system(size: 12))
                                        .foregroundStyle(svc.state == "running" ? .secondary : Color.orange)
                                }
                                if svc.state != "running", !svc.lastError.isEmpty {
                                    Text(ServiceText.error(svc.lastError))
                                        .font(.system(size: 11))
                                        .foregroundStyle(.secondary)
                                        .lineLimit(2)
                                }
                            }
                        }
                    }
                    HStack(spacing: 14) {
                        if model.state?.desired == "stopped" || !model.stateFresh {
                            LinkButton(title: tr("启动服务", "Start services")) { model.startServices() }
                        } else {
                            LinkButton(title: tr("重启服务", "Restart services")) { model.restartServices() }
                            LinkButton(title: tr("停止服务", "Stop services")) { model.stopServices() }
                        }
                        LinkButton(title: tr("日志", "Logs")) { model.openLogs() }
                    }
                    .padding(.top, 3)
                }
                .padding(.leading, 29)
                .padding(.trailing, 14)
                .padding(.bottom, 4)
            }
        }
        .padding(.vertical, 4)
    }
}

// MARK: - footer

private struct FooterBlock: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            RowButton(action: { AppDelegate.shared?.showMainWindow() }) {
                MenuRowLabel(title: model.mode == .unset ? tr("开始设置…", "Set Up…") : tr("打开 OhmyTesla", "Open OhmyTesla"),
                             shortcut: "⌘O")
            }
            .keyboardShortcut("o")
            if model.mode != .unset {
                MoreMenu()
            }
            if model.mode == .embedded && model.supervisor.runMode == .agent {
                RowButton(action: { model.quit(stopServices: false) }) {
                    MenuRowLabel(title: tr("退出（后台继续监控）", "Quit — monitoring continues"), shortcut: "⌘Q")
                }
                .keyboardShortcut("q")
            } else {
                RowButton(action: { model.quit(stopServices: false) }) {
                    MenuRowLabel(title: tr("退出 OhmyTesla", "Quit OhmyTesla"), shortcut: "⌘Q")
                }
                .keyboardShortcut("q")
            }
        }
        .padding(.vertical, 2)
    }
}

/// Less frequent actions, in a native menu that opens from the row.
private struct MoreMenu: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        RowButton(action: popUp) {
            MenuRowLabel(title: tr("更多", "More"), shortcut: nil, chevron: true)
        }
    }

    private func popUp() {
        let menu = NSMenu()
        menu.autoenablesItems = false
        func add(_ title: String, enabled: Bool = true, _ run: @escaping () -> Void) {
            let item = NSMenuItem(title: title, action: #selector(MenuAction.run), keyEquivalent: "")
            let action = MenuAction(run)
            item.target = action
            item.representedObject = action          // the item keeps its action alive
            item.isEnabled = enabled
            menu.addItem(item)
        }
        add(tr("复制门户密码", "Copy Portal Password")) { model.copyPassword() }
        if model.mode == .embedded {
            add(tr("打开 TeslaMate", "Open TeslaMate"), enabled: model.stateFresh) { model.openTeslaMate() }
            menu.addItem(.separator())
            add(tr("打开日志文件夹", "Open Logs Folder")) { model.openLogs() }
            add(tr("打开数据文件夹", "Open Data Folder")) { model.openDataFolder() }
            add(tr("编辑设置（config.env）…", "Edit Settings (config.env)…")) { model.openConfig() }
            if model.supervisor.runMode == .child {
                add(tr("安装后台服务", "Install Background Service")) {
                    Task { await model.supervisor.reinstallAgent() }
                }
            }
        }
        menu.addItem(.separator())
        if let title = model.changeModeTitle {
            add(title) { AppDelegate.shared?.changeMode() }
        }
        if model.mode == .embedded && model.supervisor.runMode == .agent {
            menu.addItem(.separator())
            add(tr("停止服务并退出", "Stop Services and Quit")) { model.quit(stopServices: true) }
        }
        menu.popUp(positioning: nil, at: NSEvent.mouseLocation, in: nil)
    }
}

final class MenuAction: NSObject {
    private let handler: () -> Void
    init(_ handler: @escaping () -> Void) { self.handler = handler }
    @objc func run() { handler() }
}

// MARK: - not set up

private struct SetupPrompt: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(tr("OhmyTesla 尚未设置", "OhmyTesla isn't set up yet")).font(.system(size: 15, weight: .semibold))
            Text(tr("连接已有服务器，或在这台 Mac 上运行。", "Connect to your server, or run it on this Mac."))
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }
}

// MARK: - building blocks

private struct SectionLabel: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 14)
    }
}

private struct Note: View {
    let text: String
    let symbol: String
    let tint: Color
    var body: some View {
        Label(text, systemImage: symbol)
            .font(.system(size: 11))
            .foregroundStyle(tint)
            .padding(.top, 6)
    }
}

private struct PanelDivider: View {
    var body: some View {
        Divider().padding(.horizontal, 14).padding(.vertical, 2)
    }
}

/// A full-width row that highlights on hover, like a menu item.
private struct RowButton<Label: View>: View {
    let action: () -> Void
    @ViewBuilder let label: () -> Label
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            label()
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 5).fill(hover ? Color.primary.opacity(0.08) : .clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 6)
        .onHover { hover = $0 }
    }
}

private struct MenuRowLabel: View {
    let title: String
    let shortcut: String?
    var chevron = false

    var body: some View {
        HStack {
            Text(title).font(.system(size: 13))
            Spacer()
            if let shortcut {
                Text(shortcut).font(.system(size: 12)).foregroundStyle(.secondary)
            }
            if chevron {
                Image(systemName: "chevron.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct PanelButton: View {
    let title: String
    let symbol: String
    let action: () -> Void

    var body: some View {
        RowButton(action: action) {
            Label(title, systemImage: symbol).font(.system(size: 13))
        }
        .padding(.horizontal, -14)      // sits in a 14 pt inset block; its text lines up with the block's
    }
}

private struct LinkButton: View {
    let title: String
    let action: () -> Void
    var body: some View {
        Button(title, action: action)
            .buttonStyle(.link)
            .font(.system(size: 12))
    }
}

/// Tells the panel when its window is on screen and when it is put away.
struct PanelVisibility: NSViewRepresentable {
    let onChange: (Bool) -> Void

    func makeNSView(context: Context) -> Probe {
        let v = Probe()
        v.onChange = onChange
        return v
    }

    func updateNSView(_ v: Probe, context: Context) { v.onChange = onChange }

    final class Probe: NSView {
        var onChange: ((Bool) -> Void)?
        private var tokens: [NSObjectProtocol] = []
        private var visible = false

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            tokens.forEach(NotificationCenter.default.removeObserver)
            tokens = []
            guard let w = window else { return report(false) }
            for name in [NSWindow.didChangeOcclusionStateNotification, NSWindow.didBecomeKeyNotification,
                         NSWindow.didResignKeyNotification, NSWindow.willCloseNotification] {
                tokens.append(NotificationCenter.default.addObserver(forName: name, object: w, queue: .main) {
                    [weak self] note in
                    self?.evaluate(closing: note.name == NSWindow.willCloseNotification)
                })
            }
            evaluate(closing: false)
        }

        deinit { tokens.forEach(NotificationCenter.default.removeObserver) }

        private func evaluate(closing: Bool) {
            guard let w = window, !closing else { return report(false) }
            report(w.isVisible && w.occlusionState.contains(.visible))
        }

        private func report(_ v: Bool) {
            guard v != visible else { return }
            visible = v
            onChange?(v)
        }
    }
}

/// Names and states of the embedded services, in the app's language.
enum ServiceText {
    static func name(_ key: String) -> String {
        switch key {
        case "postgres": return tr("数据库", "Database")
        case "mosquitto": return tr("MQTT 服务", "MQTT broker")
        case "teslamate": return "TeslaMate"
        case "hub": return tr("OhmyTesla 服务", "OhmyTesla hub")
        default: return key
        }
    }

    static func state(_ s: String) -> String {
        switch s {
        case "running": return tr("运行中", "Running")
        case "preparing": return tr("正在迁移数据库…", "Migrating database…")
        case "starting": return tr("正在启动…", "Starting…")
        case "waiting": return tr("等待中…", "Waiting…")
        case "backoff": return tr("正在重启…", "Restarting…")
        case "unhealthy": return tr("无响应", "Not responding")
        case "stopped": return tr("已停止", "Stopped")
        default: return s
        }
    }

    /// The supervisor logs English reasons; the common ones, translated.
    static func error(_ e: String) -> String {
        guard L10n.lang == .zh else { return e }
        let rules: [(String, String)] = [
            (#"^preparation exited with code (-?\d+)$"#, "准备步骤退出（代码 $1）"),
            (#"^preparation took longer than (\d+)s$"#, "准备步骤超过 $1 秒"),
            (#"^exited with code (-?\d+)$"#, "已退出（代码 $1）"),
            (#"^not healthy after (\d+)s$"#, "$1 秒内未就绪"),
            (#"^health check failed (\d+) times in a row$"#, "连续 $1 次无响应"),
            (#"^could not start: (.*)$"#, "无法启动：$1"),
        ]
        for (pattern, template) in rules where e.range(of: pattern, options: .regularExpression) != nil {
            return e.replacingOccurrences(of: pattern, with: template, options: .regularExpression)
        }
        return e
    }
}
