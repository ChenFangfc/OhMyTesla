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
    }
}

// MARK: - onboarding

struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    @State private var choice: Int? = nil
    @State private var url = ""
    @State private var password = ""
    @State private var checking = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 22) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 84, height: 84)
            Text("Welcome to OhmyTesla").font(.largeTitle.weight(.semibold))
            Text("Self-hosted TeslaMate logging, Sentry alerts, dashcam tools and more. Where should it run?")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            HStack(alignment: .top, spacing: 18) {
                card(index: 0, symbol: "desktopcomputer", title: "Run on this Mac",
                     text: "Installs the complete stack inside the app — database, MQTT, TeslaMate and the "
                         + "OhmyTesla hub. No Docker, no terminal. Monitoring continues in the background "
                         + "while this Mac is awake.")
                card(index: 1, symbol: "network", title: "Connect to an existing server",
                     text: "You already run OhmyTesla with Docker on a NAS or server. This app becomes a "
                         + "native window with notifications for it.")
            }
            if choice == 1 {
                VStack(alignment: .leading, spacing: 8) {
                    TextField("Server address, e.g. http://nas.local:8800", text: $url)
                        .textFieldStyle(.roundedBorder)
                    SecureField("Portal password (OHMYTESLA_PASSWORD)", text: $password)
                        .textFieldStyle(.roundedBorder)
                    if let error { Text(error).foregroundStyle(.red).font(.callout) }
                }
                .frame(width: 460)
            }
            HStack {
                if checking { ProgressView().controlSize(.small) }
                Button(choice == 1 ? "Connect" : "Set Up on This Mac") { go() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(choice == nil || checking || (choice == 1 && (url.isEmpty || password.isEmpty)))
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func card(index: Int, symbol: String, title: String, text: String) -> some View {
        Button { choice = index; error = nil } label: {
            VStack(alignment: .leading, spacing: 10) {
                Image(systemName: symbol).font(.system(size: 28))
                Text(title).font(.headline)
                Text(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .padding(18)
            .frame(width: 300, height: 190, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 12)
                .stroke(choice == index ? Color.accentColor : Color.secondary.opacity(0.25),
                        lineWidth: choice == index ? 2 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func go() {
        if choice == 0 { model.chooseEmbedded(); return }
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
        guard let base = URL(string: s), base.host != nil else { return .failure(Failure(text: "That is not a valid address.")) }
        let session = URLSession(configuration: .ephemeral)
        do {
            var r = URLRequest(url: base.appendingPathComponent("healthz"), timeoutInterval: 8)
            let (data, resp) = try await session.data(for: r)
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                && ((try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["ok"] as? Bool ?? false)
            guard ok else { return .failure(Failure(text: "No OhmyTesla hub answered at \(base.absoluteString).")) }
            r = URLRequest(url: base.appendingPathComponent("api/live/state"), timeoutInterval: 8)
            let token = Data("\(AppPaths.portalUser):\(password)".utf8).base64EncodedString()
            r.setValue("Basic \(token)", forHTTPHeaderField: "Authorization")
            let (_, resp2) = try await session.data(for: r)
            switch (resp2 as? HTTPURLResponse)?.statusCode ?? 0 {
            case 200: return .success(base)
            case 401: return .failure(Failure(text: "The server rejected that password."))
            case 503: return .failure(Failure(text: "The server has no OHMYTESLA_PASSWORD configured."))
            case let code: return .failure(Failure(text: "Unexpected answer from the server (HTTP \(code))."))
            }
        } catch {
            return .failure(Failure(text: "Could not reach the server: \(error.localizedDescription)"))
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
                Text("First launch: creating the database and running TeslaMate's migrations. This takes about a minute.")
                    .foregroundStyle(.secondary)
            }
            if let notice = model.supervisor.notice {
                Label(notice, systemImage: "info.circle").foregroundStyle(.orange)
                if model.supervisor.agentStatus == .requiresApproval {
                    Button("Open Login Items Settings…") { model.supervisor.openLoginItemsSettings() }
                }
            }
            ServiceList()
            HStack {
                if model.state?.desired == "stopped" {
                    Button("Start Services") { model.startServices() }
                } else {
                    Button("Restart Services") { model.restartServices() }
                }
                Button("Open Logs") { model.openLogs() }
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
                        Text(svc.title)
                        Spacer()
                        Text(label(svc)).foregroundStyle(.secondary)
                    }
                    if !svc.lastError.isEmpty && svc.state != "running" {
                        Text(svc.lastError).font(.caption).foregroundStyle(.secondary).padding(.leading, 17)
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

    private func label(_ s: SupervisorState.Service) -> String {
        switch s.state {
        case "preparing": return "migrating database…"
        case "starting": return "starting…"
        case "waiting": return "waiting…"
        case "backoff": return "restarting…"
        default: return s.state
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
                banner("The hub rejected the password.", action: "Retry") { web.reload() }
            } else if let err = web.loadError {
                banner(err, action: "Reload") { web.reload() }
            } else if let notice = model.supervisor.notice, model.mode == .embedded {
                banner(notice, action: model.supervisor.agentStatus == .requiresApproval ? "Open Settings" : nil) {
                    model.supervisor.openLoginItemsSettings()
                }
            }
            HubWebView(controller: web)
        }
        .toolbar {
            ToolbarItemGroup(placement: .navigation) {
                Button { web.webView.goBack() } label: { Image(systemName: "chevron.left") }
                    .disabled(!web.canGoBack).help("Back")
                Button { web.webView.goForward() } label: { Image(systemName: "chevron.right") }
                    .disabled(!web.canGoForward).help("Forward")
                Button { web.reload() } label: { Image(systemName: "arrow.clockwise") }.help("Reload")
                Button { web.goHome() } label: { Image(systemName: "house") }.help("Home")
            }
            ToolbarItem(placement: .primaryAction) {
                Button { model.openInBrowser() } label: { Image(systemName: "safari") }
                    .help("Open in your browser")
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
