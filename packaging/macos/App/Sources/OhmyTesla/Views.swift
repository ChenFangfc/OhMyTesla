import AppKit
import SwiftUI

/// The main window: onboarding, then either the startup status or the hub itself.
struct RootView: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var web: WebController

    var body: some View {
        Group {
            switch model.mode {
            case .unset:
                OnboardingView()
            case .embedded:
                if model.hubReady { HubBrowser() } else { StartupView() }
            case .remote:
                HubBrowser()
            }
        }
        .frame(minWidth: 760, minHeight: 520)
        .environment(\.locale, L10n.lang.locale)
    }
}

// MARK: - onboarding

struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    @State private var choice: AppModel.Mode? = nil
    @State private var url = ""
    @State private var password = ""
    @State private var checking = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 20) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 76, height: 76)
            VStack(spacing: 6) {
                Text(tr("欢迎使用 OhmyTesla", "Welcome to OhmyTesla")).font(.system(size: 24, weight: .semibold))
                Text(tr("选择 OhmyTesla 在哪里运行。之后可以在 OhmyTesla 菜单中更改。",
                        "Choose where OhmyTesla runs. You can change this later from the OhmyTesla menu."))
                    .foregroundStyle(.secondary)
            }
            HStack(alignment: .top, spacing: 16) {
                card(.remote, symbol: "network", title: tr("连接已有服务器", "Connect to a server"),
                     note: tr("最省资源", "Lightest"),
                     text: tr("已在 NAS 或服务器上运行 OhmyTesla？连接它即可。这台 Mac 只显示窗口和通知，几乎不占资源。",
                              "Already run OhmyTesla on a NAS or server? Connect to it. This Mac only shows the "
                              + "window and notifications — almost no load."))
                card(.embedded, symbol: "desktopcomputer", title: tr("在这台 Mac 上运行", "Run on this Mac"),
                     note: tr("空闲时约 300–400 MB 内存", "About 300–400 MB of memory when idle"),
                     text: tr("在应用内装好全部服务：数据库、MQTT、TeslaMate 和 OhmyTesla，无需 Docker。Mac 唤醒时在后台持续记录。",
                              "Installs everything inside the app — database, MQTT, TeslaMate and OhmyTesla. "
                              + "No Docker. Keeps logging in the background while the Mac is awake."))
            }
            if choice == .remote {
                VStack(alignment: .leading, spacing: 8) {
                    TextField(tr("服务器地址，例如 http://nas.local:8800", "Server address, e.g. http://nas.local:8800"),
                              text: $url)
                        .textFieldStyle(.roundedBorder)
                    SecureField(tr("门户密码（OHMYTESLA_PASSWORD）", "Portal password (OHMYTESLA_PASSWORD)"), text: $password)
                        .textFieldStyle(.roundedBorder)
                    if let error { Text(error).foregroundStyle(.red).font(.callout) }
                }
                .frame(width: 460)
            }
            HStack {
                if checking { ProgressView().controlSize(.small) }
                Button(choice == .embedded ? tr("在这台 Mac 上设置", "Set Up on This Mac") : tr("连接", "Connect")) { go() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(choice == nil || checking || (choice == .remote && (url.isEmpty || password.isEmpty)))
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            // Back from "Change Server…": start from the server used so far.
            if url.isEmpty, let previous = model.remoteURL {
                url = previous.absoluteString
                choice = .remote
            }
        }
    }

    private func card(_ mode: AppModel.Mode, symbol: String, title: String, note: String, text: String) -> some View {
        Button { choice = mode; error = nil } label: {
            VStack(alignment: .leading, spacing: 8) {
                Image(systemName: symbol).font(.system(size: 26)).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.headline)
                    Text(note).font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Text(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .padding(18)
            .frame(width: 300, height: 196, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 10)
                .stroke(choice == mode ? Color.accentColor : Color.secondary.opacity(0.25),
                        lineWidth: choice == mode ? 2 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func go() {
        if choice == .embedded { model.chooseEmbedded(); return }
        checking = true
        error = nil
        Task {
            let result = await RemoteCheck.verify(url, password: password)
            checking = false
            switch result {
            case .success(let base): model.chooseRemote(url: base, password: password)
            case .failure(let message): error = message.text
            }
        }
    }
}

enum RemoteCheck {
    struct Failure: Error { let text: String }

    /// /healthz must look like a hub; /api/live/state must accept the password.
    static func verify(_ raw: String, password: String) async -> Result<URL, Failure> {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if !s.contains("://") { s = "http://" + s }
        if !s.hasSuffix("/") { s += "/" }
        guard let base = URL(string: s), base.host != nil else {
            return .failure(Failure(text: tr("这不是有效的地址。", "That isn't a valid address.")))
        }
        let session = URLSession(configuration: .ephemeral)
        do {
            let r = URLRequest(url: base.appendingPathComponent("healthz"), timeoutInterval: 8)
            let (data, resp) = try await session.data(for: r)
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                && ((try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["ok"] as? Bool ?? false)
            guard ok else {
                return .failure(Failure(text: tr("\(base.absoluteString) 上没有 OhmyTesla 服务。",
                                                 "No OhmyTesla hub answered at \(base.absoluteString).")))
            }
            guard let r2 = HubEndpoint(base: base, password: password).request("api/live/state") else {
                return .failure(Failure(text: tr("这不是有效的地址。", "That isn't a valid address.")))
            }
            let (_, resp2) = try await session.data(for: r2)
            switch (resp2 as? HTTPURLResponse)?.statusCode ?? 0 {
            case 200: return .success(base)
            case 401: return .failure(Failure(text: tr("服务器拒绝了这个密码。", "The server rejected that password.")))
            case 503: return .failure(Failure(text: tr("服务器没有设置 OHMYTESLA_PASSWORD。",
                                                       "The server has no OHMYTESLA_PASSWORD set.")))
            case let code: return .failure(Failure(text: tr("服务器的回应不对（HTTP \(code)）。",
                                                            "Unexpected answer from the server (HTTP \(code)).")))
            }
        } catch {
            return .failure(Failure(text: tr("连不上服务器：\(error.localizedDescription)",
                                             "Could not reach the server: \(error.localizedDescription)")))
        }
    }
}

// MARK: - startup status (embedded)

struct StartupView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                if model.state?.status != "error" && model.state?.status != "stopped" {
                    ProgressView().controlSize(.small)
                }
                Text(model.statusLine).font(.title2.weight(.semibold))
            }
            if model.state?.firstRun == true && model.state?.status == "starting" {
                Text(tr("首次启动：正在创建数据库并运行 TeslaMate 的迁移，大约需要一分钟。",
                        "First launch: creating the database and running TeslaMate's migrations. This takes about a minute."))
                    .foregroundStyle(.secondary)
            }
            if let notice = model.supervisor.notice {
                Label(notice.text, systemImage: "info.circle").foregroundStyle(.orange)
                if model.supervisor.agentStatus == .requiresApproval {
                    Button(tr("打开登录项设置…", "Open Login Items Settings…")) { model.supervisor.openLoginItemsSettings() }
                }
            }
            ServiceList()
            HStack {
                if model.state?.desired == "stopped" {
                    Button(tr("启动服务", "Start Services")) { model.startServices() }
                } else {
                    Button(tr("重启服务", "Restart Services")) { model.restartServices() }
                }
                Button(tr("打开日志", "Open Logs")) { model.openLogs() }
                if let title = model.changeModeTitle {
                    Button(title) { AppDelegate.shared?.changeMode() }
                }
            }
        }
        .padding(32)
        .frame(maxWidth: 620, maxHeight: .infinity, alignment: .topLeading)
        .frame(maxWidth: .infinity)
    }
}

struct ServiceList: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(SupervisorState.order, id: \.self) { name in
                if let svc = model.state?.services[name], svc.state != "disabled" {
                    HStack(alignment: .firstTextBaseline) {
                        Circle().fill(color(svc)).frame(width: 9, height: 9)
                        Text(ServiceText.name(name))
                        Spacer()
                        Text(ServiceText.state(svc.state)).foregroundStyle(.secondary)
                    }
                    if !svc.lastError.isEmpty && svc.state != "running" {
                        Text(ServiceText.error(svc.lastError)).font(.caption).foregroundStyle(.secondary)
                            .padding(.leading, 17)
                    }
                }
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
    }

    private func color(_ s: SupervisorState.Service) -> Color {
        switch s.state {
        case "running": return .green
        case "backoff", "unhealthy": return .orange
        case "stopped": return .gray
        default: return .yellow
        }
    }
}

// MARK: - the hub

struct HubBrowser: View {
    @EnvironmentObject var model: AppModel
    @EnvironmentObject var web: WebController

    var body: some View {
        VStack(spacing: 0) {
            if web.authFailed {
                banner(tr("服务器拒绝了密码。", "The hub rejected the password."), action: tr("重试", "Retry")) { web.reload() }
            } else if let err = web.loadError {
                banner(err, action: tr("重新载入", "Reload")) { web.reload() }
            } else if let notice = model.supervisor.notice, model.mode == .embedded {
                banner(notice.text, action: model.supervisor.agentStatus == .requiresApproval ? tr("打开设置", "Open Settings") : nil) {
                    model.supervisor.openLoginItemsSettings()
                }
            }
            HubWebView(controller: web, appMode: model.mode)
        }
        .toolbar {
            ToolbarItemGroup(placement: .navigation) {
                Button { web.goBack() } label: { Image(systemName: "chevron.left") }
                    .disabled(!web.canGoBack).help(tr("后退", "Back"))
                Button { web.goForward() } label: { Image(systemName: "chevron.right") }
                    .disabled(!web.canGoForward).help(tr("前进", "Forward"))
                Button { web.reload() } label: { Image(systemName: "arrow.clockwise") }.help(tr("重新载入", "Reload"))
                Button { web.goHome() } label: { Image(systemName: "house") }.help(tr("首页", "Home"))
            }
            ToolbarItem(placement: .primaryAction) {
                Button { model.openInBrowser() } label: { Image(systemName: "safari") }
                    .help(tr("在浏览器中打开", "Open in your browser"))
            }
        }
        .onAppear { loadIfNeeded() }
        .onChange(of: model.hubURL) { _ in loadIfNeeded() }
    }

    private func loadIfNeeded() {
        guard let url = model.hubURL, let pw = model.password else { return }
        web.load(base: url, password: pw)
    }

    private func banner(_ text: String, action: String?, perform: @escaping () -> Void) -> some View {
        HStack {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            Text(text).lineLimit(2)
            Spacer()
            if let action { Button(action, action: perform) }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(Color.orange.opacity(0.12))
    }
}
