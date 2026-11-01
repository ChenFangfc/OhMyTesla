import Foundation

/// The language the app speaks. It follows the hub's own setting (GET /api/prefs →
/// "locale": "zh" | "en"), remembered across launches; until a hub has answered,
/// the Mac's preferred language decides.
enum Lang: String {
    case zh, en

    static var system: Lang {
        (Locale.preferredLanguages.first ?? "en").hasPrefix("zh") ? .zh : .en
    }

    /// Dates and numbers: the Mac's own locale when it speaks this language (keeps
    /// the user's 12/24-hour choice), else a plain one for the language.
    var locale: Locale {
        let current = Locale.autoupdatingCurrent
        if current.language.languageCode?.identifier == rawValue { return current }
        return Locale(identifier: self == .zh ? "zh_Hans_CN" : "en_US")
    }
}

enum L10n {
    static let defaultsKey = "language"

    /// Read by `tr`. `AppModel.adoptLanguage` changes it and republishes, so every
    /// view redraws in the new language.
    static var lang: Lang = UserDefaults.standard.string(forKey: defaultsKey).flatMap(Lang.init) ?? .system
}

/// Every user-facing string, both languages side by side: `tr("已锁", "Locked")`.
/// Vocabulary follows the hub (services/hub/app/i18n/locales).
func tr(_ zh: String, _ en: String) -> String { L10n.lang == .zh ? zh : en }

/// Short, readable times and durations in the hub's wording (`time.*`, `home.fresh.*`).
enum Fmt {
    /// "5 分钟前" / "5 min ago"
    static func ago(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds))
        if s < 60 { return tr("刚刚", "just now") }
        let m = s / 60
        if m < 60 { return tr("\(m) 分钟前", "\(m) min ago") }
        let h = m / 60
        if h < 48 { return tr("\(h) 小时前", "\(h) h ago") }
        let d = h / 24
        return tr("\(d) 天前", "\(d) days ago")
    }

    /// "1 小时 20 分" / "1 h 20 min"
    static func duration(minutes: Int) -> String {
        let m = max(0, minutes)
        if m < 60 { return tr("\(m) 分钟", "\(m) min") }
        let h = m / 60, r = m % 60
        if h >= 48 { return tr("\(h / 24) 天", "\(h / 24) days") }
        return r == 0 ? tr("\(h) 小时", "\(h) h") : tr("\(h) 小时 \(r) 分", "\(h) h \(r) min")
    }

    /// "14:05", "昨天 14:05" / "Yesterday 2:05 PM", "10月30日 14:05" / "Oct 30 2:05 PM"
    static func clock(_ date: Date, now: Date = Date()) -> String {
        let f = DateFormatter()
        f.locale = L10n.lang.locale
        f.setLocalizedDateFormatFromTemplate("jm")
        let time = f.string(from: date)
        let cal = Calendar.current
        if cal.isDate(date, inSameDayAs: now) { return time }
        if let y = cal.date(byAdding: .day, value: -1, to: now), cal.isDate(date, inSameDayAs: y) {
            return tr("昨天 \(time)", "Yesterday \(time)")
        }
        f.setLocalizedDateFormatFromTemplate("MMMd")
        return "\(f.string(from: date)) \(time)"
    }

    /// Hub titles carry an emoji for phone pushes ("👀 Cybertruck：有人靠近"); the
    /// app's own UI shows none.
    static func withoutEmoji(_ s: String) -> String {
        let kept = s.unicodeScalars.filter { u in
            if [0xFE0F, 0x200D, 0x20E3].contains(u.value) { return false }
            let p = u.properties
            return !(p.isEmoji && (p.isEmojiPresentation || u.value >= 0x2190))
        }
        return String(String.UnicodeScalarView(kept))
            .split(separator: " ", omittingEmptySubsequences: true).joined(separator: " ")
    }
}
