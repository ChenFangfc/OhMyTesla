import AppKit
import SwiftUI
import WebKit

/// Owns the WKWebView that shows the hub, so the toolbar can drive it.
@MainActor
final class WebController: NSObject, ObservableObject {
    @Published var canGoBack = false
    @Published var canGoForward = false
    @Published var isLoading = false
    @Published var authFailed = false
    @Published var loadError: String?

    let webView: WKWebView
    private(set) var base: URL?
    private var password = ""
    private var observers: [NSKeyValueObservation] = []

    override init() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.applicationNameForUserAgent = "OhmyTeslaMac/\(Bundle.main.shortVersion)"
        config.preferences.isElementFullscreenEnabled = true
        webView = WKWebView(frame: .zero, configuration: config)
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsMagnification = true
        if #available(macOS 13.3, *) { webView.isInspectable = true }
        observers = [
            webView.observe(\.canGoBack) { [weak self] wv, _ in Task { @MainActor in self?.canGoBack = wv.canGoBack } },
            webView.observe(\.canGoForward) { [weak self] wv, _ in Task { @MainActor in self?.canGoForward = wv.canGoForward } },
            webView.observe(\.isLoading) { [weak self] wv, _ in Task { @MainActor in self?.isLoading = wv.isLoading } },
        ]
    }

    func load(base: URL, password: String, path: String? = nil) {
        let changed = base != self.base || password != self.password
        self.base = base
        self.password = password
        authFailed = false
        if changed || webView.url == nil {
            webView.load(URLRequest(url: path.flatMap { URL(string: $0, relativeTo: base) } ?? base))
        } else if let path, let url = URL(string: path, relativeTo: base) {
            webView.load(URLRequest(url: url))
        }
    }

    func reload() {
        authFailed = false
        loadError = nil
        if webView.url == nil, let base { webView.load(URLRequest(url: base)) } else { webView.reload() }
    }

    func goHome() { if let base { webView.load(URLRequest(url: base)) } }

    private func sameOrigin(_ url: URL?) -> Bool {
        guard let url, let base else { return false }
        return url.host == base.host && (url.port ?? defaultPort(url)) == (base.port ?? defaultPort(base))
    }

    private func defaultPort(_ url: URL) -> Int { url.scheme == "https" ? 443 : 80 }
}

extension WebController: WKNavigationDelegate {
    // The hub protects every page with HTTP Basic auth; answer it with the
    // generated (or entered) password. WebKit then reuses the credential for
    // every sub-resource, fetch() and EventSource in this session.
    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let space = challenge.protectionSpace
        let basic = space.authenticationMethod == NSURLAuthenticationMethodHTTPBasic
        if basic, let base, space.host == base.host {
            if challenge.previousFailureCount > 0 {
                authFailed = true
                completionHandler(.cancelAuthenticationChallenge, nil)
            } else {
                completionHandler(.useCredential,
                                  URLCredential(user: AppPaths.portalUser, password: password, persistence: .forSession))
            }
            return
        }
        completionHandler(.performDefaultHandling, nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        // Anything that is not the hub (TeslaMate, Tesla's sign-in, map links,
        // docs) opens in the default browser.
        if let url = action.request.url, action.targetFrame?.isMainFrame ?? true,
           ["http", "https"].contains(url.scheme ?? ""), !sameOrigin(url) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if !response.canShowMIMEType || disposition.lowercased().hasPrefix("attachment") {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadError = nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let e = error as NSError
        if e.code != NSURLErrorCancelled { loadError = e.localizedDescription }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }
}

extension WebController: WKUIDelegate {
    // target=_blank / window.open: the hub in place, everything else in the browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if sameOrigin(url) { webView.load(action.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    // The hub asks for confirmation (unlock, open trunk, …) with confirm().
    // Without these handlers WebKit silently answers "no".
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        present(alert, in: webView) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        present(alert, in: webView) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        present(alert, in: webView) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    // <input type=file> (TeslaCam import on the USB page)
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        if let window = webView.window {
            panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
        } else {
            completionHandler(panel.runModal() == .OK ? panel.urls : nil)
        }
    }

    private func present(_ alert: NSAlert, in webView: WKWebView, _ done: @escaping (NSApplication.ModalResponse) -> Void) {
        if let window = webView.window {
            alert.beginSheetModal(for: window, completionHandler: done)
        } else {
            done(alert.runModal())
        }
    }
}

extension WebController: WKDownloadDelegate {
    // Exports and evidence packages go to ~/Downloads.
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        var target = dir.appendingPathComponent(suggestedFilename)
        let stem = target.deletingPathExtension().lastPathComponent
        let ext = target.pathExtension
        var n = 1
        while FileManager.default.fileExists(atPath: target.path) {
            n += 1
            target = dir.appendingPathComponent(ext.isEmpty ? "\(stem) \(n)" : "\(stem) \(n).\(ext)")
        }
        completionHandler(target)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let url = download.progress.fileURL {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        loadError = "Download failed: \(error.localizedDescription)"
    }
}

struct HubWebView: NSViewRepresentable {
    let controller: WebController
    func makeNSView(context: Context) -> WKWebView { controller.webView }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}

extension Bundle {
    var shortVersion: String { object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0" }
}
