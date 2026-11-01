import AppKit
import UserNotifications

/// Native notifications (UNUserNotificationCenter).
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifier()

    /// Called when the user clicks a notification.
    var onOpen: ((_ path: String?) -> Void)?

    func setUp() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    /// Short by design (the hub's push policy): the title says what happened —
    /// hub titles already carry their icon and the car — the body where and when.
    func post(id: String, title: String, subtitle: String? = nil, body: String, path: String? = nil) {
        let content = UNMutableNotificationContent()
        content.title = title
        if let subtitle, !subtitle.isEmpty { content.subtitle = subtitle }
        content.body = body
        // .defaultCritical needs Apple's critical-alerts entitlement, which an
        // ad-hoc signed app cannot have.
        content.sound = .default
        content.threadIdentifier = "ohmytesla"
        if let path { content.userInfo = ["path": path] }
        let request = UNNotificationRequest(identifier: id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler:
                                @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let path = response.notification.request.content.userInfo["path"] as? String
        DispatchQueue.main.async { self.onOpen?(path) }
        completionHandler()
    }
}
