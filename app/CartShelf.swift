import SwiftUI
import AppKit
import CoreText
import ImageIO
import Darwin

private let ink = Color.black
private let scarlet = Color(red: 1, green: 0, blue: 0)
private let yellow = Color(red: 1, green: 0.86, blue: 0.05)
private let quiet = Color(white: 0.58)
private let panel = Color(white: 0.045)
private func pixel(_ size: CGFloat, bold: Bool = false) -> Font {
    .custom(bold ? "Silkscreen-Bold" : "Silkscreen-Regular", size: size)
}
private func readableBytes(_ count: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: count, countStyle: .file)
}
private func buildCountLabel(_ count: Int) -> String {
    "\(count) \(count == 1 ? "BUILD" : "BUILDS")"
}
private func localTimestamp(_ value: String) -> String {
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let parsed = parser.date(from: value)
    parser.formatOptions = [.withInternetDateTime]
    guard let date = parsed ?? parser.date(from: value) else { return value }
    return date.formatted(date: .abbreviated, time: .shortened)
}

struct LibraryProfile: Decodable, Identifiable {
    let id: String
    let console: String
    let cartridge: String
    let fileCount: Int
    let releaseCount: Int
    let titleCount: Int
}
struct SourceFile: Decodable {
    let path: String
    let filename: String
    let rawSha256: String
    let sizeBytes: Int64
    let exportPath: String
}
struct UpdateRecord: Decodable {
    let status: String
    let localVersion: String
    let latestVersion: String
    let date: String
    let changes: String
    let source: String
    let confidence: String
    let message: String
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        func string(_ key: Keys) -> String { (try? c.decode(String.self, forKey: key)) ?? "" }
        status = string(.status); localVersion = string(.localVersion)
        latestVersion = string(.latestVersion); date = string(.date)
        source = string(.source); confidence = string(.confidence); message = string(.message)
        changes = (try? c.decode(String.self, forKey: .changes))
            ?? (try? c.decode([String].self, forKey: .changes).joined(separator: "\n")) ?? ""
    }
    enum Keys: String, CodingKey {
        case status, localVersion, latestVersion, date, changes, source, confidence, message
    }
}
struct ReleaseMetadata: Decodable {
    let description: String
    let genre: String
    let author: String
    let developer: String
    let publisher: String
    let releaseYear: String
    let canonicalTitle: String
    let sourceUrl: String
    let confidence: String
    let identityVerified: Bool
    let ed64MetadataSupported: Bool?
    let ed64MetadataLimit: String
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        func string(_ key: Keys) -> String {
            if let value = try? c.decode(String.self, forKey: key) { return value }
            if let value = try? c.decode(Int.self, forKey: key) { return String(value) }
            return ""
        }
        description = string(.description); genre = string(.genre)
        author = string(.author); developer = string(.developer); publisher = string(.publisher)
        releaseYear = string(.releaseYear); canonicalTitle = string(.canonicalTitle)
        sourceUrl = string(.sourceUrl); confidence = string(.confidence)
        identityVerified = (try? c.decode(Bool.self, forKey: .identityVerified)) ?? false
        ed64MetadataSupported = try? c.decode(Bool.self, forKey: .ed64MetadataSupported)
        ed64MetadataLimit = string(.ed64MetadataLimit)
    }
    enum Keys: String, CodingKey {
        case description, genre, author, developer, publisher, releaseYear, canonicalTitle, sourceUrl, confidence
        case identityVerified, ed64MetadataSupported, ed64MetadataLimit
    }
}
struct ROMVerification: Decodable {
    let status: String
    let summary: String
    let checkedAt: String
    let referenceName: String
    let referenceSource: String
    let warnings: [String]
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        func string(_ key: Keys) -> String { (try? c.decode(String.self, forKey: key)) ?? "" }
        status = string(.status); summary = string(.summary); checkedAt = string(.checkedAt)
        referenceName = string(.referenceName); referenceSource = string(.referenceSource)
        warnings = (try? c.decode([String].self, forKey: .warnings)) ?? []
    }
    var isRoutineSuccess: Bool {
        ["reference-match", "reference_match", "match", "matched"].contains(status) && warnings.isEmpty
    }
    enum Keys: String, CodingKey { case status, summary, checkedAt, referenceName, referenceSource, warnings }
}

struct VerificationSummary: Decodable {
    let checkedAt: String
    let filesChecked: Int
    let referenceMatches: Int
    let unlisted: Int
    let needsReview: Int
    let invalid: Int
    let changed: Int
    let reportPath: String
}
struct GameRelease: Decodable, Identifiable {
    let id: String
    let profile: String
    let title: String
    let version: String
    let identitySha256: String
    let gameCode: String
    let artworkPath: String
    let region: String
    let verified: Bool
    let sources: [SourceFile]
    let warnings: [String]
    let update: UpdateRecord?
    let metadata: ReleaseMetadata?
    let verification: ROMVerification?
    var needsReview: Bool {
        !warnings.isEmpty || ["needs-review", "invalid", "changed"].contains(verification?.status ?? "")
    }
}
struct NamedCollection: Decodable, Identifiable {
    let id: String
    let name: String
    let profile: String
    let releaseIds: [String]
    let description: String?
}
struct LibraryIssue: Decodable, Identifiable {
    let path: String
    let filename: String
    let profile: String
    let reason: String
    var id: String { path }
}
struct QuarantineBatch: Decodable, Identifiable {
    let id: String
    let count: Int
    let createdAt: String
    let restored: Bool
}
struct LibraryManagement: Decodable {
    let duplicateFiles: Int
    let duplicateBytes: Int64
    let warningReleases: Int
    let quarantineBatches: [QuarantineBatch]
}
struct CleanupMove: Decodable, Identifiable {
    let source: String
    let filename: String
    let rawSha256: String
    let sizeBytes: Int64
    let reason: String
    let keptPath: String
    var id: String { source }
}
struct CleanupWarning: Decodable, Identifiable {
    let releaseId: String
    let title: String
    let warnings: [String]
    let sources: [SourceFile]
    var id: String { releaseId }
}
struct CleanupPlan: Decodable {
    let planPath: String
    let token: String
    let duplicateFiles: Int
    let duplicateBytes: Int64
    let moves: [CleanupMove]
    let warnings: [CleanupWarning]
    let issues: [LibraryIssue]?
    let message: String
}
struct PreparedImage: Decodable, Identifiable {
    let path: String
    let name: String
    let profile: String
    let releaseIds: [String]
    let gameCount: Int
    let sizeBytes: Int64
    let sha256: String
    let createdAt: String
    let verification: String
    var id: String { path }
}
struct PreparedImageInventory: Decodable {
    let images: [PreparedImage]
}
struct ImageInspection: Decodable {
    let verified: Bool
    let verifiedAt: String
    let path: String
    let profile: String
    let name: String
    let releaseIds: [String]
    let gameCount: Int
    let sizeBytes: Int64
    let sha256: String
}
struct LibrarySnapshot: Decodable {
    let projectRoot: String
    let profiles: [LibraryProfile]
    let releases: [GameRelease]
    let collections: [NamedCollection]
    let metadataVersion: String
    let message: String?
    let management: LibraryManagement?
    let issues: [LibraryIssue]?
    let verificationSummary: VerificationSummary?
    let preparedImages: [PreparedImage]?
}
struct PackReceipt: Decodable {
    let path: String
    let folder: String
    let zipPath: String
    let imagePath: String
    let gameCount: Int
    let fileCount: Int
    let totalBytes: Int64
    let manifestSha256: String
    let message: String?
}
struct CardRecord: Decodable, Identifiable {
    let id: String
    let name: String
    let sizeBytes: Int64
    let mountPoints: [String]
    let identity: String?
    var volumeUuid: String? = nil
}
struct CardInventory: Decodable {
    let cards: [CardRecord]
    let protectedDisks: [String]
    let message: String?
}
struct InstallPlan: Decodable {
    let planPath: String
    let token: String
    let device: CardRecord
    let backupPath: String
    let imagePath: String
    let imageSha256: String
    let imageKind: String?
    let backupKind: String?
    let backupScope: String?
    let message: String?
    var preview: ReplacementPreview? = nil
    var backupMode: CardBackupKind { CardBackupKind(rawValue: backupKind ?? "") ?? .raw }
    var backupLabel: String { backupMode.label(for: backupScope) }
    var backupScopeDescription: String? {
        guard backupMode == .files, backupScope == "card-files-v1" else { return nil }
        return "The new backup excludes only these card-root macOS folders: .Spotlight-V100, .fseventsd and .Trashes. Other files, including custom hidden files, are kept."
    }
    var isBackupRestore: Bool { imageKind == "backup" || imageKind == "file-backup" }
    var replacementSummary: String {
        if imageKind == "backup" {
            return "A verified whole-card image backup comes first. The selected backup is then restored sector for sector, preserving its original partition size. macOS may request authorization for raw card access."
        }
        let backup = backupMode == .raw ? "a whole-card image backup" : backupScope == "card-files-v1" ? "a card-files backup" : "an all-files backup"
        if imageKind == "file-backup" {
            return "CartShelf verifies \(backup) of this card, prepares FAT32 using its full capacity, then restores every recorded file and folder from the selected backup."
        }
        return "CartShelf verifies \(backup), prepares FAT32 using the card's full capacity, then copies this pack and restores saves for matching ROMs. Earlier pack saves stay in the save history."
    }
}
struct SaveChoice: Codable, Identifiable, Equatable {
    let profile: String
    let identitySha256: String
    let snapshotId: String
    let bundleSha256: String
    var id: String { profile + ":" + identitySha256 }
}
struct SaveHistoryFile: Decodable {
    let key: String?
    let path: String
    let bytes: Int64
    let sha256: String
}
struct SaveHistoryEntry: Decodable, Identifiable {
    let snapshotId: String
    let profile: String
    let identitySha256: String
    let createdAt: String
    let bundleSha256: String
    let files: [SaveHistoryFile]
    let sourceRoms: [String]?
    let verified: Bool?
    var id: String { profile + ":" + identitySha256 + ":" + snapshotId }
    var choice: SaveChoice { SaveChoice(profile: profile, identitySha256: identitySha256, snapshotId: snapshotId, bundleSha256: bundleSha256) }
}
struct SaveHistoryInventory: Decodable { let entries: [SaveHistoryEntry] }
struct SaveInspection: Decodable {
    let choice: SaveChoice
    let files: [SaveHistoryFile]
    let createdAt: String
    let verified: Bool
}
struct SaveExport: Decodable { let path: String; let fileCount: Int; let message: String }
struct PackRecipeGame: Codable, Identifiable {
    let sha256: String
    let title: String
    let version: String
    var id: String { sha256 }
}
struct PackRecipe: Codable {
    let schema: Int
    let kind: String
    let name: String
    let profile: String
    let description: String
    let games: [PackRecipeGame]
}
struct RecipeInspection: Decodable {
    let sha256: String
    let recipe: PackRecipe
    let matchedReleaseIds: [String]
    let missing: [PackRecipeGame]
}
struct RecipeImport: Decodable {
    let collectionId: String
    let profile: String
    let matchedReleaseIds: [String]
    let missing: [PackRecipeGame]
}
struct SystemFilesStatus: Decodable {
    let profile: String
    let status: String
    let version: String
    let sha256: String
    let officialUrl: String
    let message: String
    let path: String?
}
struct CardHistoryRecord: Decodable, Identifiable {
    let volumeUuid: String
    let friendlyName: String
    let lastPack: String
    let lastVerifiedAt: String
    let lastBackupPath: String
    let lastBackupAt: String
    var id: String { volumeUuid }
}
struct CardHistoryInventory: Decodable { let cards: [CardHistoryRecord] }
struct CardLabelResult: Decodable { let volumeUuid: String; let friendlyName: String }
struct PreviewGame: Decodable, Identifiable {
    let identitySha256: String?
    let title: String?
    let version: String?
    let path: String?
    var id: String { (identitySha256 ?? "") + ":" + (path ?? title ?? "") }
    var name: String { title ?? path.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "Unknown build" }
}
struct PreviewGames: Decodable {
    let added: [PreviewGame]
    let retained: [PreviewGame]
    let removed: [PreviewGame]
}
struct PreviewSave: Decodable, Identifiable {
    let path: String?
    let identitySha256: String?
    let sourceKind: String?
    let bytes: Int64?
    let reason: String?
    var id: String { (identitySha256 ?? "") + ":" + (path ?? reason ?? "") }
}
struct PreviewSaves: Decodable {
    let current: [PreviewSave]
    let history: [PreviewSave]
    let selectedEarlier: [PreviewSave]
    let keptInBackup: [PreviewSave]
    let warnings: [String]
}
struct PreviewFirmwareFile: Decodable, Identifiable {
    let path: String
    let version: String
    let sha256: String
    var id: String { path }
    init(from decoder: Decoder) throws {
        if let value = try? decoder.singleValueContainer().decode(String.self) {
            path = value; version = ""; sha256 = ""
        } else {
            let c = try decoder.container(keyedBy: Keys.self)
            path = (try? c.decode(String.self, forKey: .path)) ?? (try? c.decode(String.self, forKey: .name)) ?? "System file"
            version = (try? c.decode(String.self, forKey: .version)) ?? ""
            sha256 = (try? c.decode(String.self, forKey: .sha256)) ?? ""
        }
    }
    enum Keys: String, CodingKey { case path, name, version, sha256 }
}
struct PreviewFirmware: Decodable {
    let filesInPack: [PreviewFirmwareFile]
    let installedCartridgeVersion: String?
    let installedStatus: String?
}
struct ReplacementPreview: Decodable {
    let profile: String?
    let games: PreviewGames?
    let saves: PreviewSaves?
    let firmware: PreviewFirmware?
    let sourceFingerprint: String?
    let summary: String?
}

enum CardBackupKind: String, CaseIterable {
    case files, raw
    var label: String { self == .files ? "CARD FILES" : "WHOLE CARD IMAGE" }
    func label(for scope: String?) -> String { self == .files && scope != "card-files-v1" ? "ALL FILES" : label }
    var explanation: String {
        self == .files
            ? "Backs up games, saves, firmware, settings and other files, including custom hidden files. Excludes card-root macOS index, event and Trash folders."
            : "Copies every sector, including unused space, for an exact card image."
    }
}
struct MediaReceipt: Decodable {
    let message: String?
    let backupPath: String?
    let imagePath: String?
    let verified: Bool?
    let ejected: Bool?
    let manualEjectRequired: Bool?
    let ejectWarning: String?
    let saveVaultPath: String?
    let saveFiles: Int?
    let saveVaultWarning: String?
    let restoredSaveFiles: Int?
    let saveRestoreSkipped: [SaveRestoreSkip]?
    let saveRestoreWarnings: [String]?
    let filesystemBytes: Int64?
    let backupKind: String?
    let backupScope: String?
    let excludedSystemPaths: [String]?
    let restoredFiles: Int?
}
struct SaveRestoreSkip: Decodable {
    let path: String
    let reason: String
}
struct CardResult {
    let message: String
    let backupPath: String
    let verified: Bool
    let manualEjectRequired: Bool
    let ejectWarning: String
    let saveVaultPath: String
    let saveFiles: Int
    let saveVaultWarning: String
    let restoredSaveFiles: Int?
    let saveRestoreSkipped: [SaveRestoreSkip]
    let saveRestoreWarnings: [String]
    let filesystemBytes: Int64?
    let backupKind: CardBackupKind?
    let backupScope: String?
    let excludedSystemPaths: [String]
    let restoredFiles: Int?
    var backupLabel: String? { backupKind.map { $0.label(for: backupScope) } }
    let failed: Bool
    init(_ receipt: MediaReceipt) {
        message = receipt.message ?? "Card operation complete."
        backupPath = receipt.backupPath ?? ""; verified = receipt.verified == true
        manualEjectRequired = receipt.manualEjectRequired == true
        ejectWarning = receipt.ejectWarning ?? ""; saveVaultPath = receipt.saveVaultPath ?? ""
        saveFiles = receipt.saveFiles ?? 0; saveVaultWarning = receipt.saveVaultWarning ?? ""
        restoredSaveFiles = receipt.restoredSaveFiles
        saveRestoreSkipped = receipt.saveRestoreSkipped ?? []
        saveRestoreWarnings = receipt.saveRestoreWarnings ?? []
        filesystemBytes = receipt.filesystemBytes
        backupKind = receipt.backupKind.flatMap(CardBackupKind.init(rawValue:))
        backupScope = receipt.backupScope
        excludedSystemPaths = receipt.excludedSystemPaths ?? []
        restoredFiles = receipt.restoredFiles
        failed = false
    }
    init(failure: String, backupPath: String) {
        message = failure; self.backupPath = backupPath; verified = false
        manualEjectRequired = true
        ejectWarning = "Keep the card connected. Review the error and backup receipt before retrying or removing it."
        saveVaultPath = ""; saveFiles = 0; saveVaultWarning = ""; failed = true
        restoredSaveFiles = nil; saveRestoreSkipped = []; saveRestoreWarnings = []; filesystemBytes = nil
        backupKind = nil; backupScope = nil; excludedSystemPaths = []; restoredFiles = nil
    }
}
struct GameGroup: Identifiable {
    let id: String
    let title: String
    let releases: [GameRelease]
}
struct BrowserIndex {
    struct Shelf {
        let releases: [GameRelease]
        let groups: [GameGroup]
        let releasesById: [String: GameRelease]

        func matching(_ query: String) -> [GameGroup] {
            guard !query.isEmpty else { return groups }
            return groups.filter { group in
                group.title.localizedCaseInsensitiveContains(query)
                    || group.releases.contains { release in
                        release.version.localizedCaseInsensitiveContains(query)
                            || release.gameCode.localizedCaseInsensitiveContains(query)
                            || release.sources.contains { $0.filename.localizedCaseInsensitiveContains(query) }
                    }
            }
        }
    }
    let shelves: [String: Shelf]

    init(releases: [GameRelease]) {
        shelves = Dictionary(grouping: releases, by: \.profile).mapValues { releases in
            let groups = Dictionary(grouping: releases, by: \.title).map { title, builds in
                GameGroup(id: title, title: title, releases: builds.sorted { $0.version.localizedStandardCompare($1.version) == .orderedDescending })
            }.sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
            return Shelf(releases: releases, groups: groups,
                         releasesById: Dictionary(uniqueKeysWithValues: releases.map { ($0.id, $0) }))
        }
    }
}
enum AppTab: String, CaseIterable {
    case library = "LIBRARY", packs = "PACKS", card = "CARD"
    var accent: Color {
        switch self {
        case .library: return scarlet
        case .packs: return Color(red: 1, green: 0.6, blue: 0)
        case .card: return Color(red: 0.4, green: 0.796, blue: 0.004)
        }
    }
}
enum PackFormat: String, CaseIterable { case folder, zip, image
    var label: String { self == .image ? "SD IMAGE" : rawValue.uppercased() }
}
struct ProgressEvent: Decodable {
    let event: String
    let phase: String
    let completed: Int64?
    let total: Int64?
    let message: String
    var stageLabel: String {
        switch phase {
        case "backup-files", "backup-card": return "BACKING UP YOUR CARD"
        case "verify-source-files", "verify-backup": return "VERIFYING THE BACKUP"
        case "format-card": return "PREPARING FULL CARD CAPACITY"
        case "write-card-files", "write-card": return "INSTALLING PACK AND SAVES"
        case "verify-card-files", "verify-card": return "CHECKING WRITTEN FILES"
        case "archive-mac-metadata": return "PRESERVING MACOS COMPANIONS"
        case "review-game-files": return "REVIEWING CARD CONTENTS"
        case "complete": return "VERIFIED"
        default: return phase.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ").uppercased()
        }
    }
    var fraction: Double? {
        guard let completed, let total, total > 0 else { return nil }
        return min(1, max(0, Double(completed) / Double(total)))
    }
}
enum OperationState {
    case idle(String)
    case running(String, ProgressEvent? = nil, allowsBrowsing: Bool = false, allowsQuit: Bool = false)
    case failed(String)
    var message: String {
        switch self { case .idle(let s), .failed(let s): return s; case .running(let s, _, _, _): return s }
    }
    var progress: ProgressEvent? { if case .running(_, let value, _, _) = self { return value }; return nil }
    var allowsBrowsing: Bool { if case .running(_, _, let value, _) = self { return value }; return true }
    var allowsQuit: Bool { if case .running(_, _, _, let value) = self { return value }; return true }
    var isRunning: Bool { if case .running = self { return true }; return false }
    var isFailure: Bool { if case .failed = self { return true }; return false }
}
struct BackendFailure: LocalizedError {
    let text: String
    var errorDescription: String? { text }
}

private final class ProgressLog {
    private let timer: DispatchSourceTimer
    private let reader: FileHandle
    private var pending = Data()
    init(url: URL, callback: @escaping @Sendable (ProgressEvent) -> Void) throws {
        reader = try FileHandle(forReadingFrom: url)
        timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "cartshelf.progress"))
        timer.schedule(deadline: .now(), repeating: .milliseconds(180))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let data = self.reader.availableData
            guard !data.isEmpty else { return }
            self.pending.append(data)
            while let newline = self.pending.firstIndex(of: 10) {
                let line = self.pending.prefix(upTo: newline)
                self.pending.removeSubrange(...newline)
                if let event = try? JSONDecoder().decode(ProgressEvent.self, from: line), event.event == "progress" {
                    callback(event)
                }
            }
            if self.pending.count > 65536 { self.pending.removeAll() }
        }
        timer.resume()
    }
    deinit { timer.cancel(); try? reader.close() }
}
private enum ArtworkCache {
    static let images: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>()
        cache.countLimit = 80
        return cache
    }()
    static func cached(_ path: String, revision: Int = 0) -> NSImage? {
        images.object(forKey: "\(path)|\(revision)" as NSString)
    }
    static func load(_ path: String, revision: Int = 0) -> NSImage? {
        guard !path.isEmpty else { return nil }
        let key = "\(path)|\(revision)" as NSString
        if let cached = images.object(forKey: key) { return cached }
        guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL,
                [kCGImageSourceShouldCache: false] as CFDictionary),
              let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: 400,
                kCGImageSourceShouldCacheImmediately: true,
              ] as CFDictionary) else { return nil }
        let image = NSImage(cgImage: thumbnail, size: NSSize(width: thumbnail.width, height: thumbnail.height))
        images.setObject(image, forKey: key)
        return image
    }
}
struct CoverArtwork: View {
    let path: String
    let revision: Int
    let m64: Bool
    var width: CGFloat = 105
    var height: CGFloat = 116
    @State private var image: NSImage?
    @State private var loadedKey = ""
    private var artworkKey: String { path + ":" + String(revision) }
    var body: some View {
        let displayed = ArtworkCache.cached(path, revision: revision) ?? (loadedKey == artworkKey ? image : nil)
        return Group {
            if let image = displayed {
                Image(nsImage: image).resizable().interpolation(.medium).scaledToFit().padding(4)
            } else {
                PixelIcon(kind: m64 ? .console : .handheld, tint: m64 ? .purple : yellow).padding(15)
            }
        }.frame(width: width, height: height).background(.black).modifier(Bevel())
            .task(id: artworkKey) {
                let file = path, version = revision, key = artworkKey
                if let cached = ArtworkCache.cached(file, revision: version) {
                    image = cached; loadedKey = key
                    return
                }
                let loaded = await Task.detached(priority: .userInitiated) { ArtworkCache.load(file, revision: version) }.value
                guard !Task.isCancelled else { return }
                image = loaded; loadedKey = key
            }
    }
}

enum PackScope: String, Codable { case fullLibrary, selection }

struct BrowserPreferences: Codable {
    let profile: String
    let search: String
    let focusedGroup: String?
    let focusedRelease: String?
    let selected: [String]
    let collectionId: String
    let packName: String
    let packFormat: String
    let tab: String
    let filter: String
    var packScope: PackScope? = nil
    var restoredScope: PackScope {
        packScope ?? .selection
    }
}
enum ShelfFilter: String, CaseIterable {
    case all = "ALL", inSet = "IN SET", needsReview = "NEEDS REVIEW", hasUpdate = "HAS UPDATE", missingArt = "MISSING ART"
}
struct LibraryConnection {
    let url: URL
    let volumeUUID: String
    let directoryNumber: UInt64
    let deviceNumber: UInt64

    init(_ folder: URL) throws {
        let actual = folder.resolvingSymlinksInPath().standardizedFileURL
        let bundle = Bundle.main.bundleURL.resolvingSymlinksInPath().standardizedFileURL
        guard actual != bundle, !actual.path.hasPrefix(bundle.path + "/") else {
            throw BackendFailure(text: "Choose a separate library folder outside the application bundle.")
        }
        var ancestorPath = actual.path
        while true {
            let ancestor = URL(fileURLWithPath: ancestorPath, isDirectory: true)
            let files = FileManager.default
            let sourceArchive = ["release.json", "library_manager.py", "app/CartShelf.swift"].allSatisfy {
                files.fileExists(atPath: ancestor.appendingPathComponent($0).path)
            }
            if files.fileExists(atPath: ancestor.appendingPathComponent(".git").path) || sourceArchive {
                throw BackendFailure(text: "Choose a library folder outside Git repositories and CartShelf source folders.")
            }
            if ancestorPath == "/" { break }
            let parent = (ancestorPath as NSString).deletingLastPathComponent
            if parent.isEmpty || parent == ancestorPath { break }
            ancestorPath = parent
        }
        let values = try actual.resourceValues(forKeys: [.isDirectoryKey, .volumeUUIDStringKey, .isWritableKey])
        let attributes = try FileManager.default.attributesOfItem(atPath: actual.path)
        let device = try FileManager.default.attributesOfFileSystem(forPath: actual.path)
        guard values.isDirectory == true, values.isWritable == true,
              let volume = values.volumeUUIDString, !volume.isEmpty,
              let inode = attributes[.systemFileNumber] as? NSNumber,
              let number = device[.systemNumber] as? NSNumber else {
            throw BackendFailure(text: "Choose an available writable folder for your library.")
        }
        url = actual; volumeUUID = volume; directoryNumber = inode.uint64Value; deviceNumber = number.uint64Value
    }
    func validated() throws -> URL {
        let current = try LibraryConnection(url)
        guard current.volumeUUID == volumeUUID, current.directoryNumber == directoryNumber,
              current.deviceNumber == deviceNumber else {
            throw BackendFailure(text: "The connected library changed or its drive was replaced. Connect the original library again.")
        }
        return current.url
    }
}

private enum Backend {
    private static let connectionLock = NSLock()
    private static var connection: LibraryConnection?
    private static var connectionLoaded = false
    private static let bookmarkKey = "CartShelfLibraryBookmark"
    private static let identityKey = "CartShelfLibraryIdentity"
    static var usesPreferences: Bool { Bundle.main.object(forInfoDictionaryKey: "CartShelfUIPreferences") as? Bool == true }
    static var selectedRoot: URL? { try? connectedRoot() }
    static func cachedConnection() -> LibraryConnection? {
        connectionLock.lock(); defer { connectionLock.unlock() }
        return connection
    }
    static var rootLabel: String { cachedConnection()?.url.lastPathComponent ?? "NO LIBRARY CONNECTED" }
    static var rootPrefix: String { (cachedConnection()?.url.path ?? "") + "/" }
    static func capturedConnection() throws -> LibraryConnection {
        connectionLock.lock()
        let existing = connection, loaded = connectionLoaded
        connectionLoaded = true
        connectionLock.unlock()
        if loaded {
            guard let existing else { throw BackendFailure(text: "Connect an existing library or create a new library folder to begin.") }
            _ = try existing.validated()
            return existing
        }
        let restored: LibraryConnection?
        if UserDefaults.standard.data(forKey: bookmarkKey) == nil,
           Bundle.main.object(forInfoDictionaryKey: "CartShelfQABuild") as? Bool == true,
           let selected = Bundle.main.object(forInfoDictionaryKey: "CartShelfInitialLibraryRoot") as? String, !selected.isEmpty {
            restored = try LibraryConnection(URL(fileURLWithPath: selected, isDirectory: true))
        } else if let bookmark = UserDefaults.standard.data(forKey: bookmarkKey),
                  let identity = UserDefaults.standard.dictionary(forKey: identityKey),
                  let volume = identity["volume"] as? String,
                  let inode = identity["directory"] as? String {
            var stale = false
            let folder = try URL(resolvingBookmarkData: bookmark, options: [.withSecurityScope, .withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale)
            _ = folder.startAccessingSecurityScopedResource()
            let selected = try LibraryConnection(folder)
            guard selected.volumeUUID == volume, String(selected.directoryNumber) == inode else {
                throw BackendFailure(text: "The saved library is unavailable or was replaced. Connect the original folder again.")
            }
            restored = selected
            if stale, let refreshed = try? folder.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: nil) { UserDefaults.standard.set(refreshed, forKey: bookmarkKey) }
        } else { restored = nil }
        connectionLock.lock()
        if connection == nil { connection = restored }
        let selected = connection
        connectionLock.unlock()
        guard let selected else { throw BackendFailure(text: "Connect an existing library or create a new library folder to begin.") }
        _ = try selected.validated()
        return selected
    }
    static func connectedRoot() throws -> URL { try capturedConnection().validated() }
    static func connect(_ folder: URL) throws {
        let selected = try LibraryConnection(folder)
        let bookmark = try selected.url.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: nil)
        _ = selected.url.startAccessingSecurityScopedResource()
        connectionLock.lock()
        connection = selected; connectionLoaded = true
        connectionLock.unlock()
        if usesPreferences {
            UserDefaults.standard.set(bookmark, forKey: bookmarkKey)
            UserDefaults.standard.set(["volume": selected.volumeUUID, "directory": String(selected.directoryNumber)], forKey: identityKey)
        }
    }
    static func preferences(connection supplied: LibraryConnection? = nil) -> BrowserPreferences? {
        guard usesPreferences, let selected = supplied ?? (try? capturedConnection()),
              let root = try? selected.validated() else { return nil }
        let file = root.appendingPathComponent("library/ui-state.json")
        guard file.resolvingSymlinksInPath().path.hasPrefix(root.path + "/"), let data = try? Data(contentsOf: file) else { return nil }
        return try? JSONDecoder().decode(BrowserPreferences.self, from: data)
    }
    static func savePreferences(_ state: BrowserPreferences, connection supplied: LibraryConnection? = nil) {
        guard usesPreferences, let selected = supplied ?? (try? capturedConnection()),
              let root = try? selected.validated() else { return }
        let target = root.appendingPathComponent("library/ui-state.json")
        guard target.resolvingSymlinksInPath().path.hasPrefix(root.path + "/"),
              let data = try? JSONEncoder().encode(state) else { return }
        try? FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: target, options: .atomic)
    }
    static func bundledResource(_ key: String, fallback: String) throws -> URL {
        guard let resources = Bundle.main.resourceURL else { throw BackendFailure(text: "The application resources are missing. Reinstall CartShelf.") }
        let relative = Bundle.main.object(forInfoDictionaryKey: key) as? String ?? fallback
        let result = resources.appendingPathComponent(relative).resolvingSymlinksInPath()
        guard !relative.hasPrefix("/"), result.path.hasPrefix(resources.resolvingSymlinksInPath().path + "/"),
              FileManager.default.fileExists(atPath: result.path) else {
            throw BackendFailure(text: "The bundled runtime is incomplete. Reinstall CartShelf.")
        }
        return result
    }
    static func embeddedMediaArguments(_ command: [String], root: URL? = nil) throws -> [String] {
        #if CARTSHELF_EMBEDDED_MEDIA
        let actual = try root ?? connectedRoot()
        let source = "import base64;exec(compile(base64.b64decode('" + EmbeddedMedia.sourceBase64 + "'),'CartShelfMedia','exec'),{'__name__':'__main__'})"
        let python = try bundledResource("CartShelfPythonRelativePath", fallback: "python/bin/python3")
        return [python.path, "-I", "-B", "-c", source, "--root", actual.path] + command
        #else
        throw BackendFailure(text: "This verification build does not contain an authorized card worker.")
        #endif
    }
    static func createOperationLog(_ url: URL) throws -> FileHandle {
        let descriptor = url.path.withCString { Darwin.open($0, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode_t(0o600)) }
        guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        return FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
    }
    static func readableFailure(response: Data, diagnostic: String) -> String? {
        let candidates = [String(data: response, encoding: .utf8) ?? ""] + diagnostic.split(separator: "\n").reversed().map(String.init)
        for candidate in candidates {
            guard let bytes = candidate.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                  let error = object["error"] as? String else { continue }
            let message = error.trimmingCharacters(in: .whitespacesAndNewlines)
            if !message.isEmpty { return String(message.suffix(2600)) }
        }
        let lines = diagnostic.split(separator: "\n").map(String.init).filter { line in
            guard let bytes = line.data(using: .utf8), let object = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { return true }
            return object["event"] as? String != "progress"
        }
        let message = (lines.isEmpty ? String(data: response, encoding: .utf8) ?? "" : lines.joined(separator: "\n")).trimmingCharacters(in: .whitespacesAndNewlines)
        return message.isEmpty ? nil : String(message.suffix(2600))
    }
    static func run(_ command: [String], media: Bool = false, experience: Bool = false,
                    progress: (@Sendable (ProgressEvent) -> Void)? = nil) async throws -> Data {
        let captured = cachedConnection()
        return try await Task.detached(priority: .userInitiated) {
            let selected = try captured ?? capturedConnection()
            let root = try selected.validated()
            let fm = FileManager.default
            let logDirectory = root.appendingPathComponent("tmp/app-operations", isDirectory: true)
            guard logDirectory.resolvingSymlinksInPath().path.hasPrefix(root.path + "/") else {
                throw BackendFailure(text: "The operation log folder leaves the connected library.")
            }
            try fm.createDirectory(at: logDirectory, withIntermediateDirectories: true)
            let log = logDirectory.appendingPathComponent(UUID().uuidString + ".log")
            let errors = try createOperationLog(log)
            defer { try? errors.close() }
            let progressLog = try progress.map { try ProgressLog(url: log, callback: $0) }
            defer { withExtendedLifetime(progressLog) {} }
            let process = Process()
            let output = Pipe()
            process.standardOutput = output; process.standardError = errors
            process.currentDirectoryURL = root
            let temporary = root.appendingPathComponent("tmp", isDirectory: true)
            var environment = ProcessInfo.processInfo.environment
            for key in Array(environment.keys) where key.hasPrefix("PYTHON") || key.hasPrefix("DYLD_") || key.hasPrefix("LD_") || ["VIRTUAL_ENV", "CONDA_PREFIX", "__PYVENV_LAUNCHER__"].contains(key) {
                environment.removeValue(forKey: key)
            }
            environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            environment["TMPDIR"] = temporary.path; environment["COPYFILE_DISABLE"] = "1"
            process.environment = environment
            if media {
                let invocation = try embeddedMediaArguments(command, root: root)
                process.executableURL = URL(fileURLWithPath: invocation[0]); process.arguments = Array(invocation.dropFirst())
            } else {
                let core = try bundledResource("CartShelfCoreRelativePath", fallback: "core")
                let python = try bundledResource("CartShelfPythonRelativePath", fallback: "python/bin/python3")
                let service = experience ? "library_experience" : "library_manager"
                let bootstrap = "import sys,runpy; core=sys.argv.pop(1); module=sys.argv.pop(1); sys.path.insert(0,core); runpy.run_module(module,run_name='__main__')"
                process.executableURL = python
                process.arguments = ["-I", "-B", "-c", bootstrap, core.path, service, "--root", root.path] + command
            }
            _ = try selected.validated()
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                let diagnostic = String(data: (try? Data(contentsOf: log)) ?? Data(), encoding: .utf8) ?? ""
                if !data.isEmpty {
                    var evidence = Data("\nBackend response\n".utf8)
                    evidence.append(data)
                    try? errors.write(contentsOf: evidence)
                }
                throw BackendFailure(text: readableFailure(response: data, diagnostic: diagnostic) ?? "The operation stopped. See \(log.path).")
            }
            _ = try selected.validated()
            return data
        }.value
    }
    static func decode<T: Decodable>(_ type: T.Type, data: Data) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        do { return try decoder.decode(type, from: data) }
        catch { throw BackendFailure(text: "The library tool returned an unreadable result. \(error.localizedDescription)") }
    }
}
@MainActor final class AppModel: ObservableObject {
    static weak var active: AppModel?
    private var operationId = UUID()
    private(set) var imageChoiceRevision = 0
    private var initialLoad = true
    private var restoringPreferences = true
    private var preferenceSave: DispatchWorkItem?
    private let preferenceQueue = DispatchQueue(label: "cartshelf.preferences")
    private var startupStarted = false
    private var libraryRevision = UUID()
    init(preferences: BrowserPreferences? = nil) {
        Self.active = self
        if let value = preferences {
            restorePreferences(value)
            restoringPreferences = false
        } else {
            restoringPreferences = Backend.usesPreferences
        }
    }
    private func restorePreferences(_ value: BrowserPreferences) {
        guard ["m64", "chromatic"].contains(value.profile) else { return }
        profile = value.profile; search = value.search
        focusedGroup = value.focusedGroup; focusedRelease = value.focusedRelease
        selected = Set(value.selected); collectionId = value.collectionId; packName = value.packName
        packFormat = PackFormat(rawValue: value.packFormat) ?? .image
        packScope = value.restoredScope
        tab = AppTab(rawValue: value.tab) ?? .library
        filter = ShelfFilter(rawValue: value.filter) ?? .all
    }
    func start() {
        guard !startupStarted else { return }
        startupStarted = true
        let revision = imageChoiceRevision, originalSearch = search, connectionRevision = libraryRevision
        let shouldRestore = restoringPreferences
        operation = .running("CONNECTING YOUR LIBRARY", nil, allowsBrowsing: true, allowsQuit: true)
        Task {
            let selected = await Task.detached(priority: .userInitiated) { try? Backend.capturedConnection() }.value
            guard libraryRevision == connectionRevision else { return }
            guard let selected else {
                restoringPreferences = false; libraryFolderSelected = false
                operation = .idle("CONNECT OR CREATE YOUR LIBRARY")
                return
            }
            libraryFolderSelected = true
            let value = shouldRestore ? await Task.detached(priority: .userInitiated) { Backend.preferences(connection: selected) }.value : nil
            guard libraryRevision == connectionRevision else { return }
            let unchanged = imageChoiceRevision == revision && search == originalSearch
            if unchanged, let value { restorePreferences(value) }
            restoringPreferences = false
            if !unchanged { schedulePreferenceSave() }
            operation = .idle("READY")
            load()
        }
    }
    private func schedulePreferenceSave() {
        guard !restoringPreferences, snapshot != nil, Backend.usesPreferences else { return }
        preferenceSave?.cancel()
        let revision = libraryRevision, connection = Backend.cachedConnection()
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor in
                guard let self, self.libraryRevision == revision, let connection else { return }
                let value = BrowserPreferences(profile: self.profile, search: self.search,
                    focusedGroup: self.focusedGroup, focusedRelease: self.focusedRelease,
                    selected: self.selected.sorted(), collectionId: self.collectionId, packName: self.packName,
                    packFormat: self.packFormat.rawValue, tab: self.tab.rawValue, filter: self.filter.rawValue,
                    packScope: self.packScope)
                self.preferenceQueue.async { Backend.savePreferences(value, connection: connection) }
            }
        }
        preferenceSave = work
        preferenceQueue.asyncAfter(deadline: .now() + .milliseconds(500), execute: work)
    }
    func savePreferencesNow() {
        guard !restoringPreferences, snapshot != nil else { return }
        preferenceSave?.cancel()
        Backend.savePreferences(BrowserPreferences(profile: profile, search: search, focusedGroup: focusedGroup,
            focusedRelease: focusedRelease, selected: selected.sorted(), collectionId: collectionId,
            packName: packName, packFormat: packFormat.rawValue, tab: tab.rawValue, filter: filter.rawValue,
            packScope: packScope))
    }
    @Published var snapshot: LibrarySnapshot? {
        didSet {
            artworkRevision += 1
            browserIndex = BrowserIndex(releases: snapshot?.releases ?? [])
            preparedImageInventory = snapshot?.preparedImages ?? []
            refreshGroups()
            schedulePreferenceSave()
        }
    }
    @Published private(set) var artworkRevision = 0
    @Published var operation = OperationState.idle("READY")
    @Published var tab = AppTab.library {
        didSet { if tab != oldValue { imageChoiceRevision += 1 }; schedulePreferenceSave() }
    }
    @Published var profile = "chromatic" {
        didSet { if profile != oldValue { saveChoices = []; plan = nil; showPlan = false; invalidateReceipt(); refreshPackPreview(); refreshGroups(); schedulePreferenceSave() } }
    }
    @Published var search = "" {
        didSet { if search != oldValue { refreshGroups(); schedulePreferenceSave() } }
    }
    @Published private(set) var groups: [GameGroup] = []
    private var browserIndex = BrowserIndex(releases: [])
    @Published private(set) var preparedImageInventory: [PreparedImage] = [] {
        didSet {
            preparedImageIds = Dictionary(uniqueKeysWithValues: preparedImageInventory.map { ($0.path, Set($0.releaseIds)) })
            refreshPackPreview()
            retainCompatibleSaveChoices()
        }
    }
    private var preparedImageIds: [String: Set<String>] = [:]
    private(set) var packReleases: [GameRelease] = []
    private(set) var packSourceBytes: Int64 = 0
    private(set) var matchingPreparedImage: PreparedImage?
    private var visibleGroupsById: [String: GameGroup] = [:]
    private var visibleGroupPositions: [String: Int] = [:]
    @Published var focusedGroup: String? { didSet { schedulePreferenceSave() } }
    @Published var focusedRelease: String? { didSet { schedulePreferenceSave() } }
    @Published var selected: Set<String> = [] {
        didSet {
            guard selected != oldValue else { return }
            packScope = .selection
            invalidateReceipt()
            refreshPackPreview()
            if filter == .inSet { refreshGroups() }
            schedulePreferenceSave()
        }
    }
    @Published var packScope = PackScope.fullLibrary {
        didSet {
            guard packScope != oldValue else { return }
            if packScope == .selection, packName == (profile == "m64" ? "M64 Full Library" : "Chromatic Full Library") {
                packName = profile == "m64" ? "My M64 Collection" : "My Chromatic Collection"
            }
            invalidateReceipt(); refreshPackPreview(); schedulePreferenceSave()
        }
    }
    @Published var collectionId = "" {
        didSet { if collectionId != oldValue { invalidateReceipt(); schedulePreferenceSave() } }
    }
    @Published var packName = "Chromatic Full Library" {
        didSet { if packName != oldValue { invalidateReceipt(); schedulePreferenceSave() } }
    }
    @Published var packFormat = PackFormat.image {
        didSet { if packFormat != oldValue { invalidateReceipt(); schedulePreferenceSave() } }
    }
    @Published var filter = ShelfFilter.all { didSet { refreshGroups(); schedulePreferenceSave() } }
    @Published var receipt: PackReceipt?
    @Published private(set) var preparedImageWarning: String?
    @Published var cards: [CardRecord] = []
    @Published var protectedDisks: [String] = []
    @Published var selectedCard = "" { didSet { if selectedCard != oldValue { plan = nil; showPlan = false } } }
    @Published var imagePath = "" {
        didSet { if imagePath != oldValue { imageChoiceRevision += 1; imageProfile = nil; cardSourceIsBackupFolder = false; plan = nil; showPlan = false; retainCompatibleSaveChoices() } }
    }
    @Published var imageProfile: String? { didSet { if let imageProfile, imageProfile != profile { saveChoices = [] } } }
    @Published var cardSourceIsBackupFolder = false
    @Published var backupKind = CardBackupKind.files {
        didSet { if backupKind != oldValue { plan = nil; showPlan = false } }
    }
    @Published var lastBackupPath = ""
    @Published var cardResult: CardResult?
    @Published var plan: InstallPlan?
    @Published var showPlan = false
    @Published var saveChoices: [SaveChoice] = [] { didSet { if saveChoices != oldValue { plan = nil; showPlan = false } } }
    @Published var showSaveHistory = false
    @Published var saveEntries: [SaveHistoryEntry] = []
    @Published var saveInspection: SaveInspection?
    @Published var saveExport: SaveExport?
    @Published var showSystemFiles = false
    @Published var systemFiles: SystemFilesStatus?
    @Published var historyTitle = ""
    @Published var historyIdentity = ""
    @Published var showRecipe = false
    @Published var recipeInspection: RecipeInspection?
    private var recipePath = ""
    @Published var cardHistory: [CardHistoryRecord] = []
    @Published var showCardLabel = false
    @Published var cardLabel = ""
    @Published private(set) var libraryFolderSelected = false
    @Published private(set) var reopeningLibrary = false
    @Published var showCollection = false
    @Published var showThemePicker = false
    @Published var showPreparedImages = false
    @Published var collectionName = ""
    @Published var showError = false
    @Published var cleanupPlan: CleanupPlan?
    @Published var showManagement = false
    @Published var showDeleteCollection = false
    @Published var showVerification = false
    @Published var searchFocusRequest = UUID()
    @Published var scrollRequest: String?
    @Published var browserHint: String?
    var busy: Bool { operation.isRunning }
    var hasOpenSheet: Bool {
        showPlan || showSaveHistory || showRecipe || showCardLabel || showSystemFiles || showCollection
            || showThemePicker || showPreparedImages || showManagement || showVerification
    }
    var draftLocked: Bool { busy && !operation.allowsBrowsing }
    var canReopenLibrary: Bool {
        libraryFolderSelected && initialLoad && snapshot == nil && busy
            && operation.allowsQuit && operation.progress == nil && !reopeningLibrary
    }
    var currentProfile: LibraryProfile? { snapshot?.profiles.first { $0.id == profile } }
    var releases: [GameRelease] { browserIndex.shelves[profile]?.releases ?? [] }
    var collections: [NamedCollection] { snapshot?.collections.filter { $0.profile == profile } ?? [] }
    var group: GameGroup? {
        if let id = focusedGroup, let result = visibleGroupsById[id] { return result }
        return groups.first
    }
    var release: GameRelease? {
        guard let group else { return nil }
        if let id = focusedRelease, let result = browserIndex.shelves[profile]?.releasesById[id], result.title == group.id {
            return result
        }
        return group.releases.first
    }
    private func refreshGroups() {
        let candidates = browserIndex.shelves[profile]?.matching(search) ?? []
        groups = candidates.filter { group in
            switch filter {
            case .all: return true
            case .inSet: return group.releases.contains { selected.contains($0.id) }
            case .needsReview: return group.releases.contains { $0.needsReview }
            case .hasUpdate: return group.releases.contains { $0.update?.status == "update-listed" }
            case .missingArt: return group.releases.contains { $0.artworkPath.isEmpty }
            }
        }
        visibleGroupsById = Dictionary(uniqueKeysWithValues: groups.map { ($0.id, $0) })
        visibleGroupPositions = Dictionary(uniqueKeysWithValues: groups.enumerated().map { ($0.element.id, $0.offset) })
    }
    var pickedCard: CardRecord? { cards.first { $0.id == selectedCard } }
    var emptyShelfMessage: String {
        if snapshot == nil { return busy ? "READING CARTRIDGES..." : "CONNECT YOUR LIBRARY" }
        if releases.isEmpty { return "NO GAMES INDEXED\nIMPORT ROMS TO BEGIN" }
        if search.isEmpty, filter == .inSet { return "NO BUILDS IN THIS SET" }
        return "NO MATCHING TITLES"
    }
    func showAllTitles() { search = ""; filter = .all }
    func updatePreparedImages(_ inventory: PreparedImageInventory) {
        preparedImageInventory = inventory.images
        preparedImageWarning = nil
    }
    var selectionLabel: String { packScope == .fullLibrary ? "FULL LIBRARY" : "\(selected.count) SELECTED \(selected.count == 1 ? "BUILD" : "BUILDS")" }
    var chosenCollection: NamedCollection? { collections.first { $0.id == collectionId } }
    var preparedImages: [PreparedImage] {
        preparedImageInventory.filter { $0.profile == profile }
    }
    var knownImageProfile: String? {
        guard !cardSourceIsBackupFolder else { return nil }
        return preparedImageInventory.first { $0.path == imagePath }?.profile ?? imageProfile
    }
    var imageProfileMismatch: Bool { knownImageProfile.map { $0 != profile } ?? false }
    var collectionIsEdited: Bool {
        guard let collection = chosenCollection else { return false }
        return selected.count != collection.releaseIds.count || !collection.releaseIds.allSatisfy { selected.contains($0) }
    }
    private func refreshPackPreview() {
        let shelf = browserIndex.shelves[profile]
        if packScope == .fullLibrary {
            packReleases = shelf?.releases ?? []
        } else {
            packReleases = selected.compactMap { shelf?.releasesById[$0] }.sorted {
                let order = $0.title.localizedStandardCompare($1.title)
                return order == .orderedSame ? $0.id < $1.id : order == .orderedAscending
            }
        }
        packSourceBytes = packReleases.reduce(0) { $0 + ($1.sources.first?.sizeBytes ?? 0) }
        let ids = packScope == .fullLibrary ? Set(packReleases.map(\.id)) : selected
        matchingPreparedImage = ids.isEmpty ? nil : preparedImages.first { preparedImageIds[$0.path] == ids }
    }
    var canPrepare: Bool {
        !busy && !releases.isEmpty && (packScope == .fullLibrary || !selected.isEmpty)
            && !packName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    var prepareCommand: [String]? {
        guard canPrepare else { return nil }
        var command = ["prepare", "--profile", profile, "--name", packName.trimmingCharacters(in: .whitespacesAndNewlines), "--format", packFormat.rawValue]
        if packScope == .selection { command += selected.sorted().flatMap { ["--release", $0] } }
        return command
    }
    private func invalidateReceipt() {
        imageChoiceRevision += 1
        guard receipt != nil else { return }
        receipt = nil
        if !busy { operation = .idle("PACK DRAFT CHANGED") }
    }
    func editPack() {
        guard !busy else { return }
        invalidateReceipt()
    }
    func changeProfile(_ newValue: String) {
        guard !draftLocked, profile != newValue else { return }
        profile = newValue; focusedGroup = nil; focusedRelease = nil; search = ""
        chooseCollection("")
    }
    func focus(_ group: GameGroup) {
        focusedGroup = group.id
        focusedRelease = group.releases.count == 1 ? group.releases.first?.id : nil
        browserHint = nil
    }
    func focusRelative(_ offset: Int) {
        guard !groups.isEmpty else { return }
        let position = focusedGroup.flatMap { visibleGroupPositions[$0] } ?? 0
        let target = groups[min(groups.count - 1, max(0, position + offset))]
        focus(target); scrollRequest = target.id
    }
    func focusBuild(_ release: GameRelease) { focusedRelease = release.id; browserHint = nil }
    func toggleFocusedBuild() {
        guard let group, let release else { return }
        guard group.releases.count == 1 || focusedRelease != nil else {
            browserHint = "CHOOSE A VERSION IN DETAILS"
            return
        }
        toggle(release)
    }
    var statusMessage: String {
        if busy { return operation.message }
        if tab == .card && imageProfileMismatch { return "IMAGE PROFILE MISMATCH" }
        return browserHint ?? operation.message
    }
    func toggle(_ release: GameRelease) {
        guard !draftLocked else { return }
        browserHint = nil
        if selected.contains(release.id) { selected.remove(release.id) } else { selected.insert(release.id) }
    }
    func clearSelection() {
        guard !draftLocked else { return }
        selected = []; packScope = .selection; collectionId = ""; filter = .all
        packName = profile == "m64" ? "My M64 Collection" : "My Chromatic Collection"
    }
    func chooseCollection(_ id: String) {
        guard !draftLocked else { return }
        collectionId = id
        if let c = chosenCollection {
            selected = Set(c.releaseIds); packScope = .selection; packName = c.name; filter = .inSet
        } else {
            selected = []; packScope = .fullLibrary; filter = .all
            packName = profile == "m64" ? "M64 Full Library" : "Chromatic Full Library"
        }
    }
    func run(_ command: [String], media: Bool = false, experience: Bool = false) async throws -> Data {
        let id = operationId
        return try await Backend.run(command, media: media, experience: experience) { [weak self] event in
            Task { @MainActor in
                guard let self, self.busy, self.operationId == id else { return }
                self.operation = .running(event.message.isEmpty ? self.operation.message : event.message, event, allowsBrowsing: self.operation.allowsBrowsing, allowsQuit: self.operation.allowsQuit)
            }
        }
    }
    func checkLibrary() {
        let target = profile
        action("CHECKING LIBRARY", readOnly: true) {
            self.cleanupPlan = try Backend.decode(CleanupPlan.self, data: await self.run(["cleanup-plan", "--profile", target]))
            self.showManagement = true
            self.operation = .idle("LIBRARY CHECK READY")
        }
    }
    func verifyLibrary() {
        showVerification = false
        action("VERIFYING ROM FILES", readOnly: true) {
            self.snapshot = try Backend.decode(LibrarySnapshot.self, data: await self.run(["verify"]))
            self.operation = .idle(self.snapshot?.message ?? "FILE CHECK COMPLETE")
            self.showVerification = self.snapshot?.verificationSummary != nil
        }
    }
    func quarantineDuplicates() {
        guard let plan = cleanupPlan, plan.duplicateFiles > 0 else { return }
        showManagement = false
        load(["cleanup", "--plan", plan.planPath, "--confirm", plan.token], label: "MOVING DUPLICATES TO QUARANTINE")
    }
    func restoreCleanup(_ id: String) {
        showManagement = false
        load(["restore-cleanup", "--id", id], label: "RESTORING QUARANTINED FILES")
    }
    func deleteCollection() {
        guard !collectionId.isEmpty else { return }
        let id = collectionId
        clearSelection()
        load(["collection-delete", "--id", id], label: "ARCHIVING SET DEFINITION")
    }
    func action(_ label: String, readOnly: Bool = false, allowsQuit: Bool = false, work: @escaping () async throws -> Void) {
        guard !busy else { return }
        let id = UUID()
        operationId = id
        operation = .running(label, nil, allowsBrowsing: readOnly, allowsQuit: allowsQuit)
        Task {
            do {
                try await work()
                guard operationId == id else { return }
                if operation.isRunning { operation = .idle("COMPLETE") }
            } catch {
                guard operationId == id else { return }
                operation = .failed(error.localizedDescription)
                showError = true
            }
        }
    }
    func load(_ command: [String] = ["scan"], label: String = "READING LIBRARIES") {
        action(label, readOnly: ["scan", "refresh-metadata", "check-updates"].contains(command.first ?? ""), allowsQuit: command.first == "scan") {
            self.snapshot = try Backend.decode(LibrarySnapshot.self, data: await self.run(command))
            let available = Set(self.releases.map(\.id))
            self.selected.formIntersection(available)
            self.operation = .idle(self.snapshot?.message ?? "LIBRARIES READY")
            let scanCards = self.initialLoad && self.tab == .card
            self.initialLoad = false
            if scanCards { self.refreshCards() }
        }
    }
    func connectLibrary(create: Bool = false) {
        guard !busy, !hasOpenSheet else { return }
        let picker = NSOpenPanel()
        picker.title = create ? "Create a CartShelf Library" : "Connect CartShelf Library"
        picker.message = create ? "Choose a folder for your games, packs and backups. Existing files are never replaced." : "Choose your library folder. Games, saves and backups stay in that folder."
        picker.prompt = create ? "CREATE LIBRARY" : "CONNECT"
        picker.canChooseFiles = false; picker.canChooseDirectories = true
        picker.canCreateDirectories = create; picker.allowsMultipleSelection = false
        picker.directoryURL = Backend.selectedRoot
        guard picker.runModal() == .OK, let folder = picker.url else { return }
        do {
            try Backend.connect(folder)
            preferenceSave?.cancel(); libraryRevision = UUID(); operationId = UUID()
            snapshot = nil; cards = []; cardHistory = []; selectedCard = ""; imagePath = ""
            selected = []; focusedGroup = nil; focusedRelease = nil; saveChoices = []
            plan = nil; cardResult = nil; receipt = nil; lastBackupPath = ""
            collectionId = ""; packScope = .selection; search = ""; filter = .all
            packName = profile == "m64" ? "My M64 Collection" : "My Chromatic Collection"
            restoringPreferences = true; initialLoad = true; startupStarted = false
            libraryFolderSelected = true
            if create {
                startupStarted = true
                action("CREATING LIBRARY") {
                    _ = try await self.run(["init"])
                    self.snapshot = try Backend.decode(LibrarySnapshot.self, data: await self.run(["scan"]))
                    self.restoringPreferences = false; self.initialLoad = false
                    self.operation = .idle("LIBRARY CREATED. IMPORT GAMES TO BEGIN.")
                }
            } else { start() }
        } catch {
            operation = .failed(error.localizedDescription); showError = true
        }
    }
    func loadSaveHistory() {
        guard let game = release, !busy else { return }
        let targetProfile = game.profile, identity = game.identitySha256
        historyTitle = game.title; historyIdentity = identity
        saveInspection = nil; saveExport = nil; saveEntries = []
        action("READING SAVE HISTORY", readOnly: true) {
            self.saveEntries = try Backend.decode(SaveHistoryInventory.self, data: await self.run(["save-history", "--profile", targetProfile, "--identity", identity], experience: true)).entries
            self.showSaveHistory = true
            self.operation = .idle(self.saveEntries.isEmpty ? "NO ARCHIVED SAVES FOR THIS EXACT BUILD" : "SAVE HISTORY READY")
        }
    }
    func inspectSave(_ entry: SaveHistoryEntry) {
        action("VERIFYING ARCHIVED SAVE", readOnly: true) {
            let result = try Backend.decode(SaveInspection.self, data: await self.run(["save-inspect", "--profile", entry.profile, "--identity", entry.identitySha256, "--snapshot", entry.snapshotId], experience: true))
            guard result.verified, result.choice == entry.choice else { throw BackendFailure(text: "The archived save no longer matches this history entry.") }
            self.saveInspection = result
            self.operation = .idle("ARCHIVED SAVE VERIFIED")
        }
    }
    func chooseEarlierSave(_ entry: SaveHistoryEntry) {
        guard let inspected = saveInspection, inspected.verified, inspected.choice == entry.choice, entry.profile == profile else { return }
        saveChoices.removeAll { $0.id == entry.choice.id }
        saveChoices.append(inspected.choice)
        plan = nil; showPlan = false; showSaveHistory = false
        operation = .idle("EARLIER SAVE SELECTED FOR NEXT CARD WRITE")
    }
    func exportSave(_ entry: SaveHistoryEntry) {
        action("EXPORTING VERIFIED SAVE BUNDLE") {
            let result = try Backend.decode(SaveExport.self, data: await self.run(["save-export", "--profile", entry.profile, "--identity", entry.identitySha256, "--snapshot", entry.snapshotId, "--confirm", entry.bundleSha256], experience: true))
            guard result.fileCount == entry.files.count else { throw BackendFailure(text: "The exported save bundle has an unexpected file count.") }
            self.saveExport = result
            self.saveInspection = SaveInspection(choice: entry.choice, files: entry.files, createdAt: entry.createdAt, verified: true)
            self.operation = .idle(result.message)
        }
    }
    private func retainCompatibleSaveChoices() {
        guard let image = preparedImageInventory.first(where: { $0.path == imagePath }) else { return }
        let identities = Set(image.releaseIds)
        saveChoices.removeAll { !identities.contains($0.profile + ":" + $0.identitySha256) }
    }
    func name(for choice: SaveChoice) -> String {
        browserIndex.shelves[choice.profile]?.releasesById[choice.id]?.title ?? "Exact build " + choice.identitySha256.prefix(12)
    }
    func clearSaveChoices() {
        guard !busy else { return }
        saveChoices = []; plan = nil; showPlan = false
        operation = .idle("AUTOMATIC SAVE CONTINUITY RESTORED")
    }
    func openSystemFiles() {
        let target = profile
        action("CHECKING CARTRIDGE SYSTEM FILES", readOnly: true) {
            self.systemFiles = try Backend.decode(SystemFilesStatus.self, data: await self.run(["system-files-status", "--profile", target], experience: true))
            self.showSystemFiles = true
            self.operation = .idle("CARTRIDGE SYSTEM FILES CHECKED")
        }
    }
    func importSystemFiles() {
        guard let status = systemFiles, !busy else { return }
        let picker = NSOpenPanel()
        picker.title = "Choose Official Cartridge System Files"
        picker.message = status.profile == "m64" ? "Choose the official ED64 PRO EFU file or ZIP download." : "Choose the official GB X7 OS ZIP or GBCOS.BIN file."
        picker.prompt = "VERIFY & IMPORT"; picker.canChooseDirectories = false; picker.allowsMultipleSelection = false
        guard picker.runModal() == .OK, let input = picker.url else { return }
        action("VERIFYING OFFICIAL SYSTEM FILES") {
            self.systemFiles = try Backend.decode(SystemFilesStatus.self, data: await self.run(["system-files-import", "--profile", status.profile, "--path", input.path], experience: true))
            self.operation = .idle(self.systemFiles?.message ?? "SYSTEM FILES VERIFIED")
        }
    }
    func exportRecipe() {
        guard !busy, !packReleases.isEmpty else { return }
        let picker = NSSavePanel()
        picker.title = "Export a Pack Recipe"
        picker.message = "Shares exact game choices and notes. ROMs and saves are excluded."
        picker.allowedContentTypes = [.json]; picker.canCreateDirectories = true
        picker.nameFieldStringValue = (packName.isEmpty ? "My pack" : packName) + ".cartshelf.json"
        guard picker.runModal() == .OK, let destination = picker.url else { return }
        let chosen = packReleases.map(\.id), target = profile, name = packName
        let description = chosenCollection?.description ?? ""
        action("EXPORTING PACK RECIPE") {
            var command = ["recipe-export", "--profile", target, "--name", name, "--description", description]
            command += chosen.flatMap { ["--release", $0] }
            let recipe = try Backend.decode(PackRecipe.self, data: await self.run(command, experience: true))
            let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(recipe).write(to: destination, options: .atomic)
            self.operation = .idle("PACK RECIPE EXPORTED. NO ROMS OR SAVES INCLUDED.")
            self.reveal(destination.path)
        }
    }
    func openRecipe() {
        guard !busy else { return }
        let picker = NSOpenPanel()
        picker.title = "Open a Pack Recipe"; picker.prompt = "INSPECT RECIPE"
        picker.allowedContentTypes = [.json]; picker.canChooseDirectories = false; picker.allowsMultipleSelection = false
        guard picker.runModal() == .OK, let input = picker.url else { return }
        action("RESOLVING EXACT RECIPE BUILDS", readOnly: true) {
            self.recipeInspection = try Backend.decode(RecipeInspection.self, data: await self.run(["recipe-inspect", "--path", input.path], experience: true))
            self.recipePath = input.path; self.showRecipe = true
            self.operation = .idle("REVIEW PACK RECIPE")
        }
    }
    func importRecipe() {
        guard let inspection = recipeInspection, !inspection.matchedReleaseIds.isEmpty else { return }
        let input = recipePath
        showRecipe = false
        action("IMPORTING MATCHED RECIPE BUILDS") {
            let result = try Backend.decode(RecipeImport.self, data: await self.run(["recipe-import", "--path", input, "--confirm", inspection.sha256, "--name", inspection.recipe.name], experience: true))
            self.snapshot = try Backend.decode(LibrarySnapshot.self, data: await self.run(["scan"]))
            self.profile = result.profile; self.collectionId = result.collectionId
            self.selected = Set(result.matchedReleaseIds); self.packScope = .selection
            self.packName = inspection.recipe.name; self.filter = .inSet; self.search = ""
            self.tab = .packs
            self.operation = .idle(result.missing.isEmpty ? "EXACT RECIPE BUILDS IMPORTED" : "MATCHED BUILDS IMPORTED. MISSING VERSIONS KEPT EXPLICIT.")
        }
    }
    func history(for card: CardRecord) -> CardHistoryRecord? {
        guard let uuid = card.volumeUuid else { return nil }
        return cardHistory.first { $0.volumeUuid.caseInsensitiveCompare(uuid) == .orderedSame }
    }
    func renameCard() {
        guard let card = pickedCard, card.volumeUuid != nil, !busy else { return }
        cardLabel = history(for: card)?.friendlyName ?? ""; showCardLabel = true
    }
    func saveCardLabel() {
        guard let card = pickedCard, let uuid = card.volumeUuid else { return }
        let name = cardLabel.trimmingCharacters(in: .whitespacesAndNewlines)
        showCardLabel = false
        action("SAVING CARD NAME") {
            _ = try Backend.decode(CardLabelResult.self, data: await self.run(["card-label", "--volume", uuid, "--name", name], experience: true))
            self.cardHistory = try Backend.decode(CardHistoryInventory.self, data: await self.run(["card-history"], experience: true)).cards
            self.operation = .idle("CARD NAME SAVED")
        }
    }
    func openLibrary() { if let root = Backend.selectedRoot { reveal(root.path) } }
    func openBackups() { if let root = Backend.selectedRoot { reveal(root.appendingPathComponent("backups").path) } }
    func reopenLibrary() {
        guard canReopenLibrary else { return }
        reopeningLibrary = true
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.createsNewApplicationInstance = true
        configuration.activates = true
        NSWorkspace.shared.openApplication(at: Bundle.main.bundleURL, configuration: configuration) { application, error in
            Task { @MainActor in
                guard error == nil, let application, application.processIdentifier != ProcessInfo.processInfo.processIdentifier else {
                    self.reopeningLibrary = false
                    let alert = NSAlert()
                    alert.messageText = "CartShelf could not reopen"
                    alert.informativeText = error?.localizedDescription ?? "Quit CartShelf, then open it again in Finder. Your existing library state stays in its folder."
                    alert.runModal()
                    return
                }
                NSApp.terminate(nil)
            }
        }
    }
    func importFiles() {
        let panel = NSOpenPanel()
        panel.title = "Import ROMs or an archive"
        panel.prompt = "IMPORT"
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        guard panel.runModal() == .OK else { return }
        load(["import"] + panel.urls.flatMap { ["--path", $0.path] }, label: "IMPORTING AND VERIFYING")
    }
    func saveCollection() {
        let name = collectionName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, !selected.isEmpty else { return }
        showCollection = false
        let id: String
        if let collection = chosenCollection, collection.name.caseInsensitiveCompare(name) == .orderedSame {
            id = collection.id
        } else {
            id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        }
        var command = ["collection-save", "--name", name, "--profile", profile, "--id", id]
        command += selected.sorted().flatMap { ["--release", $0] }
        action("SAVING COLLECTION") {
            self.snapshot = try Backend.decode(LibrarySnapshot.self, data: await self.run(command))
            self.collectionId = id
            self.packName = name
            self.operation = .idle("COLLECTION SAVED")
        }
    }
    func prepare() {
        guard let command = prepareCommand else { return }
        receipt = nil
        preparedImageWarning = nil
        action(packFormat == .image ? "BUILDING AND VERIFYING SD IMAGE" : "PREPARING VERIFIED PACK") {
            self.receipt = try Backend.decode(PackReceipt.self, data: await self.run(command))
            if let image = self.receipt?.imagePath, !image.isEmpty {
                self.imagePath = image; self.imageProfile = self.profile
                do {
                    self.updatePreparedImages(try Backend.decode(PreparedImageInventory.self, data: await self.run(["prepared-images"])))
                } catch {
                    self.preparedImageWarning = "Your pack is ready. The saved image list could not refresh. " + error.localizedDescription
                }
            }
            self.operation = .idle(self.receipt?.message ?? "VERIFIED PACK READY")
        }
    }
    func useReadyImage() {
        guard !busy, let receipt, !receipt.imagePath.isEmpty else { return }
        imagePath = receipt.imagePath
        imageProfile = profile
        tab = .card
    }
    func inspectPreparedImage(_ image: PreparedImage) {
        guard !busy, image.profile == profile else { return }
        let target = profile, revision = imageChoiceRevision
        action("CHECKING PREPARED IMAGE", readOnly: true) {
            let result = try Backend.decode(ImageInspection.self, data: await self.run(["inspect-image", "--path", image.path]))
            try self.finishImageInspection(result, image: image, profile: target, choiceRevision: revision)
        }
    }
    func finishImageInspection(_ result: ImageInspection, image: PreparedImage, profile target: String, choiceRevision: Int) throws {
        guard imageChoiceRevision == choiceRevision, profile == target else {
            operation = .idle("IMAGE CHECK COMPLETE. CURRENT CHOICE KEPT.")
            return
        }
        guard result.verified, result.path == image.path, result.profile == target,
              Set(result.releaseIds) == Set(image.releaseIds), result.sha256 == image.sha256,
              result.gameCount == image.gameCount, result.sizeBytes == image.sizeBytes else {
            throw BackendFailure(text: "The image no longer matches its prepared record. Build it again before using it.")
        }
        imagePath = result.path; imageProfile = result.profile; plan = nil
        tab = .card
        operation = .idle("IMAGE VERIFIED. INSERT AN SD CARD.")
    }
    func refreshCards() {
        action("LOOKING FOR SD CARDS") {
            let inventory = try Backend.decode(CardInventory.self, data: await self.run(["cards"], media: true))
            self.cards = inventory.cards; self.protectedDisks = inventory.protectedDisks
            self.cardHistory = try Backend.decode(CardHistoryInventory.self, data: await self.run(["card-history"], experience: true)).cards
            if !self.cards.contains(where: { $0.id == self.selectedCard }) { self.selectedCard = self.cards.first?.id ?? "" }
            self.operation = .idle(inventory.message ?? (self.cards.isEmpty ? "INSERT AN SD CARD" : "CARD FOUND"))
        }
    }
    func chooseImage(backup: Bool = false) {
        let panel = NSOpenPanel()
        panel.title = backup ? "Choose a verified CartShelf card backup" : "Choose an SD image"
        panel.message = backup ? "Choose a backup from your connected library." : "Choose an image from your connected library."
        panel.prompt = backup ? "CHOOSE BACKUP" : "CHOOSE IMAGE"
        panel.allowsMultipleSelection = false
        panel.canChooseFiles = true
        panel.canChooseDirectories = backup
        panel.directoryURL = Backend.selectedRoot?.appendingPathComponent(backup ? "backups" : "exports")
        panel.allowedContentTypes = backup ? [.diskImage, .data, .folder] : [.diskImage, .data]
        if panel.runModal() == .OK, let source = panel.url {
            if backup { saveChoices = [] }
            imagePath = source.path
            imageProfile = nil
            cardSourceIsBackupFolder = backup && ((try? source.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true)
            plan = nil
        }
    }
    func reviewInstall() {
        guard let card = pickedCard, !imagePath.isEmpty, !imageProfileMismatch else { return }
        action("CHECKING IMAGE AND CARD") {
            let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
            let choices = String(decoding: try encoder.encode(self.saveChoices), as: UTF8.self)
            self.plan = try Backend.decode(InstallPlan.self, data: await self.run(["plan-install", "--image", self.imagePath, "--device", card.id, "--backup-kind", self.backupKind.rawValue, "--save-choices", choices], media: true))
            self.showPlan = true
            self.operation = .idle("REVIEW CARD REPLACEMENT")
        }
    }
    func install() {
        guard let plan, !imageProfileMismatch else { return }
        showPlan = false; cardResult = nil
        action("BACKING UP, WRITING, AND VERIFYING CARD") {
            let activity = ProcessInfo.processInfo.beginActivity(options: .userInitiated, reason: "Backing up and verifying a game card")
            defer { ProcessInfo.processInfo.endActivity(activity) }
            do {
                let data = try await self.run(["install", "--plan", plan.planPath, "--confirm", plan.token], media: true)
                let receipt = try Backend.decode(MediaReceipt.self, data: data)
                self.cardResult = CardResult(receipt)
                self.lastBackupPath = receipt.backupPath ?? ""
                self.plan = nil
                if receipt.verified == true { self.saveChoices = [] }
                self.cardHistory = (try? Backend.decode(CardHistoryInventory.self, data: await self.run(["card-history"], experience: true)).cards) ?? self.cardHistory
                self.operation = .idle(receipt.message ?? "CARD REPLACEMENT VERIFIED")
            } catch {
                self.lastBackupPath = plan.backupPath
                self.cardResult = CardResult(failure: error.localizedDescription, backupPath: plan.backupPath)
                throw error
            }
        }
    }
    func reviewRecoveryBackup(_ path: String) {
        guard !busy, !path.isEmpty else { return }
        saveChoices = []
        imagePath = path; imageProfile = nil
        cardSourceIsBackupFolder = URL(fileURLWithPath: path).pathExtension == "files"
        plan = nil; showPlan = false; tab = .card
        operation = .idle("BACKUP SELECTED. REVIEW THE CARD BEFORE RESTORING.")
    }
    func backupCard() {
        guard let card = pickedCard else { return }
        cardResult = nil
        action("BACKING UP AND VERIFYING CARD") {
            let activity = ProcessInfo.processInfo.beginActivity(options: .userInitiated, reason: "Backing up and verifying a game card")
            defer { ProcessInfo.processInfo.endActivity(activity) }
            do {
                let data = try await self.run(["backup", "--device", card.id, "--backup-kind", self.backupKind.rawValue], media: true)
                let receipt = try Backend.decode(MediaReceipt.self, data: data)
                self.cardResult = CardResult(receipt)
                self.lastBackupPath = receipt.backupPath ?? ""
                self.operation = .idle(receipt.message ?? "CARD BACKUP VERIFIED")
            } catch {
                self.cardResult = CardResult(failure: error.localizedDescription, backupPath: "")
                throw error
            }
        }
    }
    func reveal(_ path: String) { if !path.isEmpty { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)]) } }
}

struct Bevel: ViewModifier {
    var highlighted = false
    var accent: Color = scarlet
    func body(content: Content) -> some View {
        content.background(panel)
            .overlay(Rectangle().stroke(Color(white: 0.18), lineWidth: 1))
            .overlay(alignment: .top) { Rectangle().fill(highlighted ? accent : Color(white: 0.48)).frame(height: 3) }
            .overlay(alignment: .leading) { Rectangle().fill(highlighted ? accent : Color(white: 0.4)).frame(width: 3) }
            .overlay(alignment: .bottom) { Rectangle().fill(Color(white: 0.12)).frame(height: 3) }
            .overlay(alignment: .trailing) { Rectangle().fill(Color(white: 0.12)).frame(width: 3) }
            .padding(2).overlay(Rectangle().stroke(highlighted ? accent : Color(white: 0.24), lineWidth: 2))
    }
}
struct PixelButton: View {
    let title: String
    var primary = false
    var danger = false
    var disabled = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(title).font(pixel(12, bold: primary)).foregroundStyle(disabled ? Color(white: 0.3) : primary ? .black : .white)
                .padding(.horizontal, 14).frame(minHeight: 34)
                .frame(maxWidth: .infinity)
                .background(disabled ? Color(white: 0.07) : primary ? (danger ? scarlet : yellow) : Color(white: 0.12))
                .overlay(alignment: .top) { Rectangle().fill(Color.white.opacity(disabled ? 0.04 : 0.35)).frame(height: 2) }
                .overlay(alignment: .leading) { Rectangle().fill(Color.white.opacity(disabled ? 0.04 : 0.25)).frame(width: 2) }
                .overlay(alignment: .bottom) { Rectangle().fill(Color.black.opacity(0.7)).frame(height: 3) }
                .overlay(alignment: .trailing) { Rectangle().fill(Color.black.opacity(0.7)).frame(width: 3) }
                .overlay(Rectangle().stroke(Color(white: 0.27), lineWidth: 1))
        }.buttonStyle(.plain).disabled(disabled)
    }
}
struct PixelProgress: View {
    let fraction: Double
    var body: some View {
        GeometryReader { geometry in
            HStack(spacing: 2) {
                ForEach(0..<13, id: \.self) { index in
                    Rectangle().fill(Double(index) / 13 < fraction ? Color(red: 0.5, green: 1, blue: 0) : Color(white: 0.15))
                }
            }.padding(2).background(.black).overlay(Rectangle().stroke(quiet, lineWidth: 1))
        }.accessibilityLabel("Progress").accessibilityValue("\(Int(fraction * 100)) percent")
    }
}
struct PixelIcon: View {
    enum Kind { case library, pack, card, handheld, console }
    let kind: Kind
    enum Mood { case idle, busy, happy, error }
    var tint: Color = .white
    var mood = Mood.idle
    var body: some View {
        Canvas { context, size in
            let unit = min(size.width, size.height) / 24
            func box(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ color: Color) {
                context.fill(Path(CGRect(x: x * unit, y: y * unit, width: w * unit, height: h * unit)), with: .color(color))
            }
            switch kind {
            case .library:
                box(3, 4, 18, 17, tint); box(5, 6, 14, 13, .black); box(7, 8, 10, 2, tint); box(7, 12, 10, 2, tint); box(7, 16, 6, 1, tint)
            case .pack:
                box(2, 7, 20, 14, tint); box(4, 9, 16, 10, .black); box(4, 3, 7, 4, tint); box(4, 5, 16, 2, tint); box(11, 11, 2, 5, tint); box(8, 14, 8, 2, tint)
            case .card:
                box(6, 2, 13, 20, tint); box(4, 5, 2, 17, tint); box(6, 7, 11, 13, .black); box(8, 3, 2, 4, .black); box(11, 3, 2, 4, .black); box(14, 3, 2, 4, .black); box(8, 12, 7, 2, tint)
            case .handheld:
                box(6, 1, 12, 21, tint); box(7, 2, 10, 10, Color(white: 0.22)); box(8, 3, 8, 7, .black); box(9, 4, 6, 5, Color(red: 0, green: 0.7, blue: 0.9)); box(8, 14, 5, 2, .black); box(10, 12, 2, 6, .black); box(15, 13, 2, 2, scarlet); box(13, 16, 2, 2, scarlet); box(12, 20, 4, 1, Color(white: 0.3))
                if mood == .busy {
                    box(9, 4, 2, 2, .white.opacity(0.7)); box(13, 7, 2, 2, .white.opacity(0.7))
                    box(11, 6, 2, 1, .black)
                } else {
                    box(10, 5, 1, 1, .black); box(13, 5, 1, 1, .black)
                    box(11, mood == .error ? 6 : 7, 2, 1, .black)
                    if mood == .happy { box(10, 6, 1, 1, .black); box(13, 6, 1, 1, .black) }
                }
            case .console:
                box(3, 8, 18, 10, tint); box(5, 5, 14, 3, tint); box(8, 2, 8, 4, Color(white: 0.7)); box(10, 1, 4, 2, Color(white: 0.7)); box(8, 7, 8, 2, .black); box(5, 14, 3, 2, .black); box(10, 14, 3, 2, .black); box(15, 14, 3, 2, .black); box(4, 18, 3, 2, Color(white: 0.3)); box(17, 18, 3, 2, Color(white: 0.3))
            }
        }.accessibilityHidden(true)
    }
}
struct PixelBackdrop: View {
    var body: some View {
        Canvas { context, size in
            let u: CGFloat = 5
            for i in 0..<24 {
                let x = CGFloat((i * 89 + 23) % 997)
                let y = CGFloat((i * 67 + 37) % 701)
                let px = x / 1040 * size.width, py = y / 760 * size.height
                let color = Color(white: 0.11)
                context.fill(Path(CGRect(x: px, y: py, width: u * 3, height: u)), with: .color(color))
                context.fill(Path(CGRect(x: px + u, y: py - u, width: u, height: u * 3)), with: .color(color))
            }
        }.accessibilityHidden(true)
    }
}
struct WindowSetup: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            window.isMovableByWindowBackground = true
            window.titleVisibility = .hidden
            window.titlebarAppearsTransparent = true
            window.backgroundColor = .black
            for button in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] { window.standardWindowButton(button)?.isHidden = true }
        }
        return view
    }
    func updateNSView(_ nsView: NSView, context: Context) {}
}

private struct PixelScrollView<Content: View>: View {
    @State private var contentHeight: CGFloat = 0
    private let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }
    var body: some View {
        GeometryReader { viewport in
            let overflows = contentHeight > viewport.size.height + 1
            ScrollView(.vertical, showsIndicators: false) {
                content.background {
                    GeometryReader { geometry in
                        Color.clear.onChange(of: geometry.size.height, initial: true) { _, height in
                            contentHeight = height
                        }
                    }
                }
            }
            .scrollIndicators(.never)
            .padding(.bottom, overflows ? 18 : 0)
            .overlay(alignment: .bottomTrailing) {
                if overflows {
                    Text("SCROLL").font(pixel(7)).foregroundStyle(quiet)
                        .padding(.horizontal, 5).padding(.vertical, 3).background(.black)
                        .overlay(Rectangle().stroke(quiet.opacity(0.45), lineWidth: 1))
                        .padding(3)
                        .allowsHitTesting(false).accessibilityHidden(true)
                }
            }
        }
    }
}

struct CartShelfView: View {
    @ObservedObject var model: AppModel
    @FocusState private var searchFocused: Bool
    @FocusState private var browserFocused: Bool
    private var browserKeysEnabled: Bool {
        model.tab == .library && (!model.busy || model.operation.allowsBrowsing)
            && !model.showCollection && !model.showThemePicker && !model.showPreparedImages
            && !model.showPlan && !model.showManagement && !model.showVerification
            && !model.showSaveHistory && !model.showRecipe && !model.showCardLabel && !model.showSystemFiles
    }
    var body: some View {
        VStack(spacing: 0) {
            topBar
            VStack(spacing: 0) {
                heading
                deviceSelector
                Group {
                    switch model.tab {
                    case .library: libraryPanel
                    case .packs: packsPanel
                    case .card: cardPanel
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
                footer
            }.background { PixelBackdrop() }.background(.black).modifier(Bevel(highlighted: true, accent: model.tab.accent))
        }
        .padding(14).background(.black).foregroundStyle(.white)
        .frame(minWidth: 1000, minHeight: 720).background(WindowSetup())
        .focusable().focused($browserFocused).focusEffectDisabled()
        .onKeyPress(.downArrow) {
            guard browserKeysEnabled, !searchFocused else { return .ignored }
            model.focusRelative(1); return .handled
        }
        .onKeyPress(.upArrow) {
            guard browserKeysEnabled, !searchFocused else { return .ignored }
            model.focusRelative(-1); return .handled
        }
        .onKeyPress(.space) {
            guard browserKeysEnabled, !searchFocused else { return .ignored }
            model.toggleFocusedBuild(); return .handled
        }
        .task { model.start(); browserFocused = true }
        .onChange(of: model.searchFocusRequest) { _, _ in searchFocused = true }
        .onChange(of: model.tab) { _, tab in if tab == .card { model.refreshCards() } }
        .sheet(isPresented: $model.showCollection) { collectionSheet }
        .sheet(isPresented: $model.showThemePicker) { themePickerSheet }
        .sheet(isPresented: $model.showPreparedImages) { preparedImagesSheet }
        .sheet(isPresented: $model.showPlan) { installSheet }
        .sheet(isPresented: $model.showSaveHistory) { saveHistorySheet }
        .sheet(isPresented: $model.showRecipe) { recipeSheet }
        .sheet(isPresented: $model.showCardLabel) { cardLabelSheet }
        .sheet(isPresented: $model.showSystemFiles) { systemFilesSheet }
        .sheet(isPresented: $model.showManagement) { managementSheet }
        .sheet(isPresented: $model.showVerification) { verificationSheet }
        .alert("ARCHIVE THIS SET?", isPresented: $model.showDeleteCollection) {
            Button("CANCEL", role: .cancel) {}
            Button("ARCHIVE SET", role: .destructive) { model.deleteCollection() }
        } message: { Text("This set leaves the chooser. A copy is kept in Backups & Saves. Your games stay in the library.") }
        .alert("OPERATION STOPPED", isPresented: $model.showError) {
            Button("OK", role: .cancel) {}
        } message: { Text(model.operation.message) }
    }
    private var topBar: some View {
        HStack(alignment: .bottom, spacing: 8) {
            ForEach(AppTab.allCases, id: \.self) { tab in
                Button {
                    if !model.busy || model.operation.allowsBrowsing { model.tab = tab }
                } label: {
                    HStack(spacing: 9) {
                        PixelIcon(kind: tab == .library ? .library : tab == .packs ? .pack : .card, tint: model.tab == tab ? .white : quiet).frame(width: 25, height: 25)
                        Text(tab.rawValue).font(pixel(12)).foregroundStyle(model.tab == tab ? .white : quiet)
                    }.padding(.horizontal, 15).frame(height: 46)
                        .background(model.tab == tab ? .black : Color(white: 0.07))
                        .modifier(Bevel(highlighted: model.tab == tab, accent: tab.accent))
                }.buttonStyle(.plain).disabled(model.busy && !model.operation.allowsBrowsing)
            }
            Spacer()
            HStack(spacing: 0) {
                ForEach(Array("CARTSHELF".enumerated()), id: \.offset) { i, letter in
                    Text(String(letter)).font(pixel(18, bold: true)).foregroundStyle([scarlet, .orange, yellow, .green, .mint, .cyan, .blue, .purple, .pink][i])
                }
            }.padding(.bottom, 13)
            Spacer().frame(width: 12)
            Button { NSApp.keyWindow?.miniaturize(nil) } label: {
                Text("_").font(pixel(18, bold: true)).foregroundStyle(.white).frame(width: 30, height: 30).background(Color(white: 0.15)).modifier(Bevel())
            }.buttonStyle(.plain).accessibilityLabel("Minimize CartShelf").padding(.bottom, 9)
            Button { NSApp.terminate(nil) } label: {
                Text("X").font(pixel(18, bold: true)).foregroundStyle(.white).frame(width: 30, height: 30).background(scarlet).modifier(Bevel())
            }.buttonStyle(.plain).accessibilityLabel("Quit CartShelf").disabled(!model.operation.allowsQuit).padding(.bottom, 9)
        }
    }
    private var heading: some View {
        HStack {
            Text(model.tab == .library ? "CARTRIDGE LIBRARY" : model.tab == .packs ? "BUILD A GAME PACK" : "SD CARD STATION")
                .font(pixel(25, bold: true)).tracking(-1).foregroundStyle(.black)
            Spacer()
            Text(Bundle.main.object(forInfoDictionaryKey: "CartShelfVersion") as? String ?? "DEV").font(pixel(12)).foregroundStyle(.black.opacity(0.6))
        }.padding(.horizontal, 18).frame(height: 53).background(model.tab.accent).padding(10)
    }
    private var deviceSelector: some View {
        HStack(spacing: 12) {
            deviceButton(id: "chromatic", console: "CHROMATIC", cartridge: "EVERDRIVE-GB X7", icon: .handheld, accent: yellow)
            deviceButton(id: "m64", console: "M64", cartridge: "EVERDRIVE-64 PRO", icon: .console, accent: Color(red: 0.58, green: 0.44, blue: 0.88))
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 6) {
                Text("\(model.currentProfile?.titleCount ?? 0) TITLES").font(pixel(15)).foregroundStyle(.white)
                Text("\(model.currentProfile?.fileCount ?? 0) FILES IN LIBRARY").font(pixel(10)).foregroundStyle(quiet)
            }.padding(.trailing, 4)
        }.padding(.horizontal, 18).padding(.top, 4).padding(.bottom, 14)
    }
    private func deviceButton(id: String, console: String, cartridge: String, icon: PixelIcon.Kind, accent: Color) -> some View {
        Button { model.changeProfile(id) } label: {
            HStack(spacing: 11) {
                PixelIcon(kind: icon, tint: accent).frame(width: 37, height: 42)
                VStack(alignment: .leading, spacing: 6) {
                    Text(console).font(pixel(15, bold: true)).foregroundStyle(model.profile == id ? accent : quiet)
                    Text(cartridge).font(pixel(9)).foregroundStyle(model.profile == id ? .white : quiet)
                }
                if model.profile == id { Text("●").font(.system(size: 9)).foregroundStyle(accent) }
            }.padding(.horizontal, 12).frame(height: 62).frame(minWidth: 242)
                .background(model.profile == id ? Color(white: 0.10) : .black).modifier(Bevel(highlighted: model.profile == id, accent: accent))
        }.buttonStyle(.plain).disabled(model.busy && !model.operation.allowsBrowsing)
    }
    private var libraryPanel: some View {
        let groups = model.groups
        let focusedGroupId = model.group?.id
        return VStack(spacing: 12) {
            HStack(spacing: 10) {
                HStack(spacing: 10) {
                    Text(">").font(pixel(15)).foregroundStyle(yellow)
                    TextField("SEARCH TITLES OR VERSIONS", text: $model.search).textFieldStyle(.plain).font(pixel(11)).accessibilityLabel("Search library")
                        .focused($searchFocused)
                        .onKeyPress(.downArrow) { model.focusRelative(1); searchFocused = false; browserFocused = true; return .handled }
                        .onKeyPress(.upArrow) { model.focusRelative(-1); searchFocused = false; browserFocused = true; return .handled }
                    if !model.search.isEmpty { Button { model.search = "" } label: { Text("X").font(pixel(10)).foregroundStyle(quiet) }.buttonStyle(.plain) }
                }.padding(.horizontal, 12).frame(height: 36).modifier(Bevel())
                PixelButton(title: "IMPORT", primary: true, disabled: model.busy || model.snapshot == nil) { model.importFiles() }.frame(width: 114)
                PixelButton(title: "RESCAN", disabled: model.busy) { model.load() }.frame(width: 108)
            }
            HStack(spacing: 8) {
                ForEach(ShelfFilter.allCases, id: \.self) { filter in
                    Button { model.filter = filter } label: {
                        Text(filter.rawValue).font(pixel(8)).foregroundStyle(model.filter == filter ? .black : quiet)
                            .padding(.horizontal, 10).frame(height: 26).background(model.filter == filter ? yellow : Color(white: 0.09))
                            .overlay(Rectangle().stroke(Color(white: 0.24), lineWidth: 1))
                    }.buttonStyle(.plain)
                }
                Spacer()
                Button { model.verifyLibrary() } label: { Text("VERIFY ROMS").font(pixel(9)).foregroundStyle(yellow) }.buttonStyle(.plain).disabled(model.busy)
                Button { model.checkLibrary() } label: {
                    Text("LIBRARY CHECK").font(pixel(9)).foregroundStyle(yellow)
                }.buttonStyle(.plain).disabled(model.busy)
            }
            HStack(alignment: .top, spacing: 14) {
                VStack(spacing: 0) {
                    HStack {
                        Text("GAME SHELF").font(pixel(11)).foregroundStyle(quiet)
                        Spacer()
                        Text("\(groups.count)").font(pixel(11)).foregroundStyle(yellow)
                    }.padding(12).background(Color(white: 0.09))
                    ScrollViewReader { proxy in
                        PixelScrollView {
                            LazyVStack(spacing: 1) {
                                ForEach(groups) { group in
                                    groupRow(group, focused: focusedGroupId == group.id).id(group.id)
                                }
                                if groups.isEmpty {
                                    Text(model.emptyShelfMessage)
                                        .font(pixel(12)).foregroundStyle(quiet).multilineTextAlignment(.center).padding(32)
                                }
                            }
                        }.onChange(of: model.scrollRequest) { _, id in
                            if let id { proxy.scrollTo(id, anchor: .center) }
                        }
                    }
                }.frame(minWidth: 310, maxWidth: .infinity).modifier(Bevel())
                detailsPanel.frame(minWidth: 430, maxWidth: .infinity)
            }.frame(maxHeight: .infinity)
            HStack(spacing: 10) {
                collectionPicker
                PixelButton(title: "SAVE SET", disabled: model.busy || model.selected.isEmpty) {
                    model.collectionName = model.collections.first(where: { $0.id == model.collectionId })?.name ?? ""
                    model.showCollection = true
                }.frame(width: 120)
                PixelButton(title: "BUILD PACK >", primary: true, disabled: model.busy || model.releases.isEmpty) { model.tab = .packs }.frame(width: 165)
            }
        }.padding(.horizontal, 18).padding(.bottom, 14)
    }
    private func groupRow(_ group: GameGroup, focused: Bool) -> some View {
        HStack(spacing: 0) {
            Button { model.focus(group); browserFocused = true } label: {
                HStack(spacing: 8) {
                    Text(focused ? ">" : " ").font(pixel(12)).foregroundStyle(yellow).frame(width: 13)
                    Text(group.title).font(pixel(12)).multilineTextAlignment(.leading).lineLimit(2).foregroundStyle(focused ? .white : Color(white: 0.72))
                    Spacer(minLength: 2)
                    if group.releases.count > 1 {
                        let count = group.releases.filter { model.selected.contains($0.id) }.count
                        Text(count > 0 ? "\(count)/\(group.releases.count) IN SET" : "\(group.releases.count) BUILDS")
                            .font(pixel(8)).foregroundStyle(count > 0 ? yellow : quiet).fixedSize().padding(.trailing, 8)
                    }
                }.padding(.leading, 8).padding(.vertical, 10).frame(maxWidth: .infinity, alignment: .leading)
            }.buttonStyle(.plain).accessibilityLabel(group.title)
            if group.releases.count == 1, let release = group.releases.first {
                Button { model.toggle(release) } label: {
                    Text(model.selected.contains(release.id) ? "X" : " ").font(pixel(10, bold: true)).foregroundStyle(yellow)
                        .frame(width: 19, height: 19).background(.black).overlay(Rectangle().stroke(quiet, lineWidth: 1))
                }.buttonStyle(.plain).padding(.horizontal, 10)
                    .accessibilityLabel((model.selected.contains(release.id) ? "Remove " : "Add ") + group.title + (model.selected.contains(release.id) ? " from set" : " to set"))
            }
        }.background(focused ? Color(white: 0.16) : .clear)
    }
    @ViewBuilder private var detailsPanel: some View {
        if let release = model.release, let group = model.group {
            VStack(spacing: 0) {
                PixelScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 15) {
                    artwork(release)
                    VStack(alignment: .leading, spacing: 9) {
                        Text(group.title).font(pixel(18, bold: true)).fixedSize(horizontal: false, vertical: true)
                        if let metadata = release.metadata {
                            let facts = [metadata.genre, metadata.releaseYear].filter { !$0.isEmpty }
                            if !facts.isEmpty { Text(facts.joined(separator: " / ")).font(pixel(9)).foregroundStyle(yellow) }
                            if !metadata.description.isEmpty {
                                Text(metadata.description).font(.system(size: 12, design: .monospaced))
                                    .foregroundStyle(Color(white: 0.82)).lineLimit(3)
                            } else {
                                Text("No sourced description is available for this build.").font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
                            }
                        } else {
                            Text("No sourced game details are available for this build.").font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
                        }
                        Text([release.region.isEmpty ? "REGION UNSPECIFIED" : release.region.uppercased(),
                              release.sources.first.map { readableBytes($0.sizeBytes) } ?? ""].filter { !$0.isEmpty }.joined(separator: " / "))
                            .font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                    }
                    Spacer(minLength: 0)
                }
                HStack {
                    PixelButton(title: "SAVE HISTORY", disabled: model.busy) { model.loadSaveHistory() }.frame(width: 190)
                    Text("Archives for this exact build").font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                }
                if let verification = release.verification, !verification.isRoutineSuccess { verificationPanel(verification) }
                if !release.warnings.isEmpty {
                    Text(release.warnings.joined(separator: "\n")).font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                }
                HStack {
                    Text("CHOOSE EXACT BUILDS").font(pixel(10)).foregroundStyle(quiet)
                    Spacer()
                    Text("\(group.releases.count) AVAILABLE").font(pixel(9)).foregroundStyle(quiet)
                }
                VStack(spacing: 5) {
                    ForEach(group.releases) { build in
                        HStack(spacing: 10) {
                            Button { model.toggle(build) } label: {
                                Text(model.selected.contains(build.id) ? "X" : " ").font(pixel(12, bold: true)).foregroundStyle(yellow)
                                    .frame(width: 23, height: 23).background(.black).overlay(Rectangle().stroke(quiet, lineWidth: 2))
                            }.buttonStyle(.plain).accessibilityLabel((model.selected.contains(build.id) ? "Remove " : "Add ") + (build.sources.first?.filename ?? build.title) + (model.selected.contains(build.id) ? " from set" : " to set"))
                            Button { model.focusBuild(build) } label: {
                                VStack(alignment: .leading, spacing: 6) {
                                    HStack {
                                        Text(build.version.lowercased() == "unknown" ? "VERSION UNKNOWN" : "VERSION \(build.version)").font(pixel(10)).foregroundStyle(release.id == build.id ? .white : quiet)
                                        Spacer(minLength: 0)
                                        if build.sources.count > 1 { Text("\(build.sources.count) IDENTICAL FILES").font(pixel(8)).foregroundStyle(quiet) }
                                    }
                                    Text(build.sources.first?.filename ?? build.id)
                                        .font(.system(size: 11, design: .monospaced)).foregroundStyle(Color(white: 0.76))
                                        .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
                                }.frame(minWidth: 0, maxWidth: .infinity, minHeight: 66, alignment: .leading)
                            }.buttonStyle(.plain)
                        }.padding(8).background(release.id == build.id ? Color(white: 0.12) : Color(white: 0.055))
                    }
                }
                if let verification = release.verification, verification.isRoutineSuccess { verificationPanel(verification) }
                if let metadata = release.metadata {
                    DisclosureGroup {
                        metadataPanel(metadata)
                    } label: {
                        Text("GAME DETAILS & SOURCES").font(pixel(9)).foregroundStyle(quiet)
                    }.id(release.id + "metadata")
                }
                if let update = release.update {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(update.latestVersion.isEmpty ? "OFFICIAL RELEASE NOTES" : "MODRETRO LISTS \(update.latestVersion)").font(pixel(10)).foregroundStyle(yellow)
                        Text(update.message.isEmpty ? update.status.replacingOccurrences(of: "-", with: " ") : update.message).font(.system(size: 11, design: .monospaced)).foregroundStyle(Color(white: 0.74)).lineLimit(2)
                        if !update.changes.isEmpty { Text(update.changes).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet).lineLimit(3) }
                        Text("LOCAL VERSION FROM FILENAME").font(pixel(8)).foregroundStyle(quiet)
                    }.padding(10).frame(maxWidth: .infinity, alignment: .leading).background(Color(white: 0.075))
                }
                Spacer(minLength: 0)
                VStack(alignment: .leading, spacing: 5) {
                    Text(release.sources.first?.filename ?? "").font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet).lineLimit(2).textSelection(.enabled)
                    HStack {
                        Text("FILE SHA256 \(String((release.sources.first?.rawSha256 ?? release.identitySha256).prefix(24)))...").font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled)
                        Spacer()
                        Button("REVEAL") { model.reveal(release.sources.first?.path ?? "") }.font(pixel(8)).buttonStyle(.plain).foregroundStyle(yellow)
                    }
                }
                    }.padding(15).frame(maxWidth: .infinity, alignment: .topLeading)
                }.id(model.profile + ":" + group.id)
                HStack(spacing: 8) {
                    if model.profile == "chromatic" {
                        PixelButton(title: "CHECK GAME UPDATES", disabled: model.busy) { model.load(["check-updates"], label: "READING MODRETRO RELEASE NOTES") }
                    }
                    PixelButton(title: "REFRESH METADATA", disabled: model.busy) {
                        model.load(["refresh-metadata", "--profile", model.profile], label: "REFRESHING METADATA")
                    }
                }.padding(.horizontal, 15).padding(.bottom, 15).padding(.top, 8)
            }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading).modifier(Bevel())
        } else if model.snapshot != nil {
            VStack(spacing: 16) {
                PixelIcon(kind: model.profile == "m64" ? .console : .handheld, tint: model.profile == "m64" ? .purple : yellow).frame(width: 100, height: 110)
                Text(model.emptyShelfMessage).font(pixel(15)).foregroundStyle(quiet).multilineTextAlignment(.center)
                if model.releases.isEmpty {
                    Text("Add Game Boy, Game Boy Color, or N64 files to fill your shelf.")
                        .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center)
                    PixelButton(title: "IMPORT ROMS", primary: true, disabled: model.busy) { model.importFiles() }.frame(width: 220)
                } else {
                    Text(model.filter == .inSet && model.search.isEmpty ? "Choose a theme or add exact builds to this set." : "Try another title, version, or shelf filter.")
                        .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center)
                    PixelButton(title: "SHOW ALL TITLES", disabled: model.draftLocked) { model.showAllTitles() }.frame(width: 220)
                }
            }.padding(24).frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity).modifier(Bevel())
        } else {
            PixelScrollView {
                VStack(spacing: 14) {
                    PixelIcon(kind: model.profile == "m64" ? .console : .handheld, tint: model.profile == "m64" ? .purple : yellow).frame(width: 100, height: 110)
                    Text(model.busy ? "READING YOUR LIBRARY" : "SELECT A CARTRIDGE").font(pixel(15)).foregroundStyle(quiet)
                    PixelButton(title: "CONNECT LIBRARY", disabled: model.busy || model.reopeningLibrary) { model.connectLibrary() }.frame(width: 220)
                    PixelButton(title: "CREATE LIBRARY", primary: true, disabled: model.busy) { model.connectLibrary(create: true) }.frame(width: 220)
                    if model.canReopenLibrary || model.reopeningLibrary {
                        PixelButton(title: model.reopeningLibrary ? "REOPENING..." : "REOPEN CARTSHELF", primary: true, disabled: !model.canReopenLibrary) { model.reopenLibrary() }.frame(width: 240)
                        Text("Folder selected. Reopen CartShelf if reading has not started.")
                            .font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center)
                    } else {
                        Text("Connect an existing library or create a new library folder.")
                            .font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center)
                    }
                    Text("Your games, saves, packs and backups stay in your chosen library folder.").font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center)
                }.padding(24).frame(minWidth: 0, maxWidth: .infinity)
            }.frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity).modifier(Bevel())
        }
    }
    private func verificationPanel(_ record: ROMVerification) -> some View {
        let status = record.status.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ").uppercased()
        let color: Color = record.isRoutineSuccess ? .green : ["invalid", "changed"].contains(record.status) ? scarlet : .orange
        return Group {
            if record.isRoutineSuccess {
                DisclosureGroup {
                    verificationEvidence(record)
                } label: {
                    Text(status).font(pixel(9)).foregroundStyle(color)
                }.id((model.release?.id ?? "") + "verification")
            } else {
                VStack(alignment: .leading, spacing: 5) {
                    Text(status).font(pixel(10)).foregroundStyle(color)
                    verificationEvidence(record)
                }
            }
        }.padding(10).frame(minWidth: 0, maxWidth: .infinity, alignment: .leading).background(Color(white: 0.075))
    }
    private func verificationEvidence(_ record: ROMVerification) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(record.summary).font(.system(size: 11, design: .monospaced)).foregroundStyle(Color(white: 0.75)).fixedSize(horizontal: false, vertical: true)
            if !record.referenceName.isEmpty { Text(record.referenceName).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled) }
            if let url = URL(string: record.referenceSource), ["http", "https"].contains(url.scheme ?? "") {
                Link("REFERENCE SOURCE", destination: url).font(pixel(8)).foregroundStyle(yellow)
            } else if !record.referenceSource.isEmpty {
                Text(record.referenceSource).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled)
            }
            if !record.checkedAt.isEmpty { Text("Last checked " + localTimestamp(record.checkedAt)).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet) }
            if !record.warnings.isEmpty { Text(record.warnings.joined(separator: "\n")).font(.system(size: 10, design: .monospaced)).foregroundStyle(.orange) }
        }
    }
    private func metadataPanel(_ metadata: ReleaseMetadata) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            let facts = [metadata.genre, metadata.releaseYear].filter { !$0.isEmpty }
            if !facts.isEmpty { Text(facts.joined(separator: " / ")).font(pixel(9)).foregroundStyle(yellow) }
            let authors = [metadata.author, metadata.developer, metadata.publisher].filter { !$0.isEmpty }
            if !authors.isEmpty { Text(Array(NSOrderedSet(array: authors)) as? [String] ?? authors, format: .list(type: .and)).font(.system(size: 11, design: .monospaced)).foregroundStyle(Color(white: 0.75)) }
            if !metadata.description.isEmpty {
                Text(metadata.description).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color(white: 0.78)).fixedSize(horizontal: false, vertical: true)
            }
            if metadata.ed64MetadataSupported == false {
                Text("Cartridge metadata is unavailable for this ROM header. The library artwork and details remain available here.")
                    .font(.system(size: 10, design: .monospaced)).foregroundStyle(.orange)
            }
            HStack(alignment: .top, spacing: 8) {
                if !metadata.confidence.isEmpty { Text(metadata.confidence).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true) }
                Spacer(minLength: 0)
                if let url = URL(string: metadata.sourceUrl), ["http", "https"].contains(url.scheme ?? "") {
                    Link("SOURCE", destination: url).font(pixel(8)).foregroundStyle(yellow)
                }
            }
        }.padding(10).frame(maxWidth: .infinity, alignment: .leading).background(Color(white: 0.075))
    }
    private func artwork(_ release: GameRelease) -> some View {
        CoverArtwork(path: release.artworkPath, revision: model.artworkRevision, m64: model.profile == "m64",
                     width: model.profile == "m64" ? 136 : 105, height: model.profile == "m64" ? 98 : 116)
    }
    private var collectionPicker: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("CHOOSE A THEME").font(pixel(8)).foregroundStyle(quiet)
            HStack(spacing: 9) {
                Button { model.showThemePicker = true } label: {
                    HStack(spacing: 10) {
                        Text(model.chosenCollection?.name ?? (model.packScope == .fullLibrary ? "FULL LIBRARY" : "CUSTOM SET"))
                            .font(pixel(9)).lineLimit(1).truncationMode(.tail)
                        Spacer(minLength: 0)
                        Text("V").font(pixel(8)).foregroundStyle(quiet)
                    }.padding(.horizontal, 10).frame(height: 27).background(.black).modifier(Bevel())
                }.buttonStyle(.plain).frame(minWidth: 140, maxWidth: .infinity)
                    .accessibilityLabel("Choose a theme").help("Choose a saved theme or the full library.").disabled(model.draftLocked)
                Text(buildCountLabel(model.packScope == .fullLibrary ? model.releases.count : model.selected.count))
                    .font(pixel(8)).foregroundStyle(yellow).fixedSize()
                if !model.collectionId.isEmpty {
                    Button { model.showDeleteCollection = true } label: { Text("ARCHIVE SET").font(pixel(8)).foregroundStyle(quiet) }.buttonStyle(.plain).disabled(model.busy)
                }
                if model.packScope == .fullLibrary || !model.selected.isEmpty || !model.collectionId.isEmpty {
                    Button { model.clearSelection() } label: { Text("CLEAR").font(pixel(8)).foregroundStyle(quiet) }.buttonStyle(.plain).disabled(model.draftLocked)
                }
            }
        }.padding(.horizontal, 10).padding(.vertical, 8).frame(minWidth: 0, maxWidth: .infinity).modifier(Bevel())
    }

    private var packsPanel: some View {
        VStack(spacing: 12) {
            HStack(spacing: 12) {
                Text("RECIPES SHARE CHOICES, WITHOUT ROMS OR SAVES").font(pixel(8)).foregroundStyle(quiet)
                Spacer()
                PixelButton(title: "SYSTEM FILES", disabled: model.busy || model.snapshot == nil) { model.openSystemFiles() }.frame(width: 160)
                PixelButton(title: "OPEN RECIPE", disabled: model.busy || model.snapshot == nil) { model.openRecipe() }.frame(width: 160)
                PixelButton(title: "EXPORT RECIPE", disabled: model.busy || model.packReleases.isEmpty) { model.exportRecipe() }.frame(width: 178)
            }
            packPreview.padding(18).frame(minWidth: 0, maxWidth: .infinity, maxHeight: .infinity).modifier(Bevel())
            if let receipt = model.receipt, !model.busy {
                packReadyPanel(receipt)
            } else {
                HStack(alignment: .top, spacing: 18) {
                    packNameField.frame(minWidth: 0, maxWidth: .infinity)
                    VStack(alignment: .leading, spacing: 7) {
                        packOutputChoices
                        packOutputDescription
                    }.frame(width: 290)
                    VStack(alignment: .leading, spacing: 8) {
                        if model.busy {
                            progressPanel
                        } else if let prepared = model.matchingPreparedImage {
                            Text("PREPARED IMAGE / \(readableBytes(prepared.sizeBytes))").font(pixel(8)).foregroundStyle(quiet)
                            PixelButton(title: "CHECK & USE IMAGE >", disabled: model.busy) { model.inspectPreparedImage(prepared) }
                                .help("Checks the current saved image before opening Card.")
                        }
                        packBuildButton
                    }.frame(width: 252)
                }.padding(14).frame(minWidth: 0, maxWidth: .infinity, alignment: .leading).modifier(Bevel())
            }
        }.frame(minWidth: 0, maxWidth: .infinity).padding(.horizontal, 18).padding(.bottom, 16)
    }
    private func packReadyPanel(_ receipt: PackReceipt) -> some View {
        HStack(alignment: .top, spacing: 20) {
            VStack(alignment: .leading, spacing: 9) {
                Text("VERIFIED PACK READY").font(pixel(16, bold: true)).foregroundStyle(yellow)
                    .fixedSize(horizontal: false, vertical: true)
                Text("\(buildCountLabel(receipt.gameCount)) / \(receipt.fileCount) FILES / \(readableBytes(receipt.totalBytes))")
                    .font(pixel(9)).foregroundStyle(.white).fixedSize(horizontal: false, vertical: true)
                Text(URL(fileURLWithPath: receipt.path).lastPathComponent).font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(quiet).lineLimit(3).truncationMode(.middle)
                    .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading).textSelection(.enabled).help(receipt.path)
                if let warning = model.preparedImageWarning {
                    Text(warning).font(.system(size: 11, design: .monospaced)).foregroundStyle(.orange)
                        .lineLimit(3).help(warning)
                }
            }.frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
            VStack(spacing: 8) {
                if !receipt.imagePath.isEmpty {
                    PixelButton(title: "WRITE TO CARD >", primary: true) { model.useReadyImage() }
                }
                PixelButton(title: "SHOW PACK", primary: receipt.imagePath.isEmpty) { model.reveal(receipt.path) }
                Button { model.editPack() } label: { Text("EDIT PACK").font(pixel(8)).foregroundStyle(quiet) }.buttonStyle(.plain)
            }.frame(width: 252)
        }.padding(16).frame(minWidth: 0, maxWidth: .infinity, alignment: .leading).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private var packThemeActions: some View {
        HStack(spacing: 12) {
            PixelButton(title: "CHANGE THEME", disabled: model.draftLocked) { model.showThemePicker = true }
                .accessibilityLabel("Choose a theme")
            if model.packScope == .fullLibrary || !model.selected.isEmpty || !model.collectionId.isEmpty {
                Button { model.clearSelection() } label: { Text("CLEAR").font(pixel(8)).foregroundStyle(quiet) }.buttonStyle(.plain).disabled(model.draftLocked)
            }
        }.frame(width: 240)
    }
    private var packPreview: some View {
        PixelScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .top, spacing: 20) {
                    packCollectionHeading
                    packThemeActions
                }
                LazyVGrid(columns: [GridItem(.flexible(), alignment: .top), GridItem(.flexible(), alignment: .top), GridItem(.flexible(), alignment: .top)], alignment: .center, spacing: 16) {
                    ForEach(model.packReleases) { build in
                        HStack(alignment: .top, spacing: 12) {
                            CoverArtwork(path: build.artworkPath, revision: model.artworkRevision, m64: model.profile == "m64", width: model.profile == "m64" ? 128 : 86, height: model.profile == "m64" ? 94 : 102)
                            VStack(alignment: .leading, spacing: 7) {
                                Text(build.title).font(.system(size: 12, weight: .semibold, design: .monospaced)).foregroundStyle(.white)
                                    .fixedSize(horizontal: false, vertical: true)
                                if let genre = build.metadata?.genre, !genre.isEmpty {
                                    Text(genre).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                Text(build.version.lowercased() == "unknown" ? "VERSION UNKNOWN" : "VERSION \(build.version)")
                                    .font(.system(size: 9, design: .monospaced)).foregroundStyle(yellow)
                                    .fixedSize(horizontal: false, vertical: true)
                            }.frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                        }.padding(10).frame(minWidth: 0, maxWidth: .infinity, minHeight: 122, alignment: .topLeading).background(Color(white: 0.075))
                    }
                }
                if !model.packReleases.isEmpty { exactPackBuilds }
            }.frame(minWidth: 0, maxWidth: .infinity, alignment: .topLeading)
        }.frame(minWidth: 0, maxWidth: .infinity, maxHeight: .infinity)
    }

    private var packCollectionHeading: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(model.packScope == .fullLibrary ? "THE WHOLE SHELF" : model.chosenCollection?.name ?? (model.packReleases.isEmpty ? "YOUR EMPTY SET" : "YOUR CUSTOM SET"))
                .font(pixel(21, bold: true)).foregroundStyle(yellow).fixedSize(horizontal: false, vertical: true)
            Text(model.chosenCollection?.description ?? (model.packScope == .fullLibrary ? "Every exact build in your \(model.profile == "m64" ? "M64" : "Chromatic") library, ready for one card." : model.packReleases.isEmpty ? "Choose a theme, or add exact builds in Library to start your own set." : "Your exact build choices from Library. Save the set to keep this selection."))
                .font(.system(size: 12, design: .monospaced)).foregroundStyle(Color(white: 0.82)).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Text("\(buildCountLabel(model.packReleases.count)) / \(readableBytes(model.packSourceBytes)) SOURCE ROMS")
                    .font(pixel(9)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                if model.collectionIsEdited { Text("EDITED SET").font(pixel(8)).foregroundStyle(.orange) }
            }
        }.frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
    }
    private var exactPackBuilds: some View {
        DisclosureGroup {
            LazyVStack(alignment: .leading, spacing: 11) {
                ForEach(model.packReleases) { build in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(build.title).font(.system(size: 11, weight: .semibold, design: .monospaced)).foregroundStyle(.white)
                        Text(build.sources.first?.filename ?? build.id)
                            .font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    }.frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                }
            }.padding(.top, 10)
        } label: {
            Text("EXACT BUILD DETAILS").font(pixel(9)).foregroundStyle(quiet)
        }.tint(quiet).padding(.top, 5)
    }
    private var packNameField: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("PACK NAME").font(pixel(9)).foregroundStyle(quiet)
            TextField("Name your game pack", text: $model.packName).font(pixel(10)).textFieldStyle(.plain)
                .padding(10).background(.black).modifier(Bevel())
                .accessibilityLabel("Pack name").disabled(model.draftLocked)
        }
    }
    private var packOutputChoices: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("OUTPUT").font(pixel(9)).foregroundStyle(quiet)
            HStack(spacing: 6) {
                ForEach(PackFormat.allCases, id: \.self) { format in
                    Button { model.packFormat = format } label: {
                        Text(format.label).font(pixel(9)).foregroundStyle(model.packFormat == format ? .black : .white)
                            .frame(minWidth: 0, maxWidth: .infinity).frame(height: 34)
                            .background(model.packFormat == format ? yellow : Color(white: 0.12)).modifier(Bevel())
                    }.buttonStyle(.plain).disabled(model.busy)
                }
            }
        }
    }
    private var packOutputDescription: some View {
        Text(model.packFormat == .image ? "A verified SD image for Card or another image writer." : model.packFormat == .zip ? "A portable archive of the card folder and manifest." : "A verified folder with the cartridge's game and metadata layout.")
            .font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
    }
    private var packBuildButton: some View {
        PixelButton(title: model.packFormat == .image ? "BUILD SD IMAGE" : "PREPARE PACK", primary: true, disabled: !model.canPrepare) { model.prepare() }
    }
    private var cardPanel: some View {
        HStack(alignment: .top, spacing: 18) {
            VStack(alignment: .leading, spacing: 10) {
                PixelScrollView {
                    VStack(alignment: .leading, spacing: 11) {
                        stepLabel("01", "CHOOSE A PACK OR BACKUP")
                        HStack(spacing: 10) {
                            PixelButton(title: "PREPARED IMAGES", disabled: model.busy || model.preparedImages.isEmpty) { model.showPreparedImages = true }
                            PixelButton(title: "CHOOSE OTHER...", disabled: model.busy) { model.chooseImage() }
                            PixelButton(title: "SYSTEM FILES", disabled: model.busy || !model.libraryFolderSelected) { model.openSystemFiles() }
                        }
                        Text(imageName).font(.system(size: 12, design: .monospaced)).foregroundStyle(model.imagePath.isEmpty ? quiet : .white).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12).frame(minWidth: 0, maxWidth: .infinity, alignment: .leading).modifier(Bevel())
                        if model.cardSourceIsBackupFolder {
                            Text("Backup folder selected. Card review verifies every recorded file before restoring.")
                                .font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                        } else if model.imageProfileMismatch {
                            HStack(spacing: 12) {
                                Text("This image is for \(model.knownImageProfile == "m64" ? "M64" : "Chromatic"). Switch devices before card review.")
                                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                                PixelButton(title: "SWITCH", disabled: model.busy) {
                                    if let profile = model.knownImageProfile { model.changeProfile(profile) }
                                }.frame(width: 95)
                            }
                        } else if !model.imagePath.isEmpty {
                            Text(model.knownImageProfile.map { "\($0 == "m64" ? "M64" : "Chromatic") image selected. Card review checks the image again." } ?? "Device profile is not recorded for this image. Review its contents before writing.")
                                .font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                        }
                        Button { model.chooseImage(backup: true) } label: { Text("RESTORE A CARTSHELF BACKUP...").font(pixel(9)).foregroundStyle(yellow) }.buttonStyle(.plain).disabled(model.busy)
                        stepLabel("02", "INSERT YOUR SD CARD")
                        if model.cards.isEmpty {
                            HStack(spacing: 18) {
                                PixelIcon(kind: .card, tint: quiet).frame(width: 48, height: 58)
                                VStack(alignment: .leading, spacing: 8) {
                                    Text("NO ELIGIBLE CARD").font(pixel(13)).foregroundStyle(quiet)
                                    Text("Insert a removable SD card, then rescan.").font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet)
                                }
                            }.padding(12).frame(maxWidth: .infinity, alignment: .leading).modifier(Bevel())
                        } else {
                            VStack(spacing: 6) {
                                ForEach(model.cards) { card in
                                    Button { model.selectedCard = card.id; model.plan = nil } label: {
                                        HStack(spacing: 12) {
                                            Text(model.selectedCard == card.id ? "X" : " ").font(pixel(12)).foregroundStyle(yellow).frame(width: 22, height: 22).overlay(Rectangle().stroke(quiet, lineWidth: 1))
                                            VStack(alignment: .leading, spacing: 5) {
                                                Text(model.history(for: card).flatMap { $0.friendlyName.isEmpty ? nil : $0.friendlyName } ?? card.name).font(pixel(12)).foregroundStyle(.white)
                                                Text("\(card.id)  ·  \(readableBytes(card.sizeBytes))").font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
                                                if let identity = card.identity { Text("ID " + identity.prefix(16)).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet) }
                                            }
                                            Spacer()
                                        }.padding(12).background(model.selectedCard == card.id ? Color(white: 0.14) : .black).modifier(Bevel())
                                    }.buttonStyle(.plain).disabled(model.busy)
                                }
                            }
                        }
                        HStack(spacing: 10) {
                            PixelButton(title: "RESCAN CARDS", disabled: model.busy) { model.refreshCards() }
                            PixelButton(title: "BACK UP ONLY", disabled: model.busy || model.pickedCard == nil) { model.backupCard() }
                        }
                        if let card = model.pickedCard {
                            HStack {
                                PixelButton(title: "NAME CARD", disabled: model.busy || card.volumeUuid == nil) { model.renameCard() }.frame(width: 165)
                                Text("Names are advisory. Live device checks always apply.").font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                            }
                            if let history = model.history(for: card) {
                                if !history.lastPack.isEmpty { Text("Last verified pack: " + history.lastPack).font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet) }
                                if !history.lastBackupAt.isEmpty { Text("Last backup: " + localTimestamp(history.lastBackupAt)).font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet) }
                                if !history.lastBackupPath.isEmpty { Button("SHOW LAST BACKUP") { model.reveal(history.lastBackupPath) }.font(pixel(8)).buttonStyle(.plain).foregroundStyle(yellow) }
                            }
                        }
                        if !model.saveChoices.isEmpty {
                            Text("\(model.saveChoices.count) EARLIER SAVE \(model.saveChoices.count == 1 ? "BUNDLE" : "BUNDLES") CHOSEN").font(pixel(10)).foregroundStyle(.orange)
                            ForEach(model.saveChoices) { choice in
                                Text(model.name(for: choice) + " / archive " + choice.snapshotId.prefix(12)).font(.system(size: 11, design: .monospaced)).foregroundStyle(.white)
                            }
                            Text("These exact-build choices override matching current saves for this reviewed write only. Current card saves are backed up first.").font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
                            PixelButton(title: "CLEAR EARLIER SAVE CHOICES", disabled: model.busy) { model.clearSaveChoices() }
                        }
                        stepLabel("03", "BACK UP. REPLACE. VERIFY.")
                        HStack(spacing: 8) {
                            ForEach(CardBackupKind.allCases, id: \.self) { kind in
                                Button { model.backupKind = kind } label: {
                                    Text(kind.label).font(pixel(9)).foregroundStyle(model.backupKind == kind ? .black : .white)
                                        .frame(minWidth: 0, maxWidth: .infinity).frame(height: 34)
                                        .background(model.backupKind == kind ? yellow : Color(white: 0.12)).modifier(Bevel())
                                }.buttonStyle(.plain).disabled(model.busy)
                            }
                        }
                        Text(model.backupKind.explanation)
                            .font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                        Text("Fresh packs use full card capacity and restore matching saves. A verified backup comes first.\nFinish saving in-game and power off before removing its SD card.")
                            .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                    }.frame(minWidth: 0, maxWidth: .infinity, alignment: .topLeading)
                }.frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
                PixelButton(title: "REVIEW CARD REPLACEMENT >", primary: true, disabled: model.busy || model.pickedCard == nil || model.imagePath.isEmpty || model.imageProfileMismatch) { model.reviewInstall() }
            }.padding(16).frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity).modifier(Bevel())
            cardStatusPanel
        }.frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity).padding(.horizontal, 18).padding(.bottom, 16)
    }
    private var cardStatusPanel: some View {
        VStack(spacing: 15) {
            PixelIcon(kind: model.profile == "m64" ? .console : .handheld, tint: model.profile == "m64" ? .purple : yellow, mood: model.busy ? .busy : model.cardResult?.failed == true ? .error : .happy).frame(width: 92, height: 105)
            PixelScrollView {
                VStack(spacing: 14) {
                    if model.busy { progressPanel }
                    else if let result = model.cardResult {
                        Text(result.failed ? "OPERATION STOPPED" : result.verified ? "CARD CHECK VERIFIED" : "CARD RESULT")
                            .font(pixel(13, bold: true)).foregroundStyle(result.failed ? scarlet : .green).multilineTextAlignment(.center)
                        Text(result.message).font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center)
                        if result.manualEjectRequired {
                            Text(result.failed ? "KEEP THE CARD CONNECTED" : "MANUAL EJECT REQUIRED").font(pixel(11, bold: true)).foregroundStyle(.orange).multilineTextAlignment(.center)
                            Text(result.ejectWarning.isEmpty ? "Eject the card in Finder before removing it." : result.ejectWarning)
                                .font(.system(size: 11, design: .monospaced)).foregroundStyle(.orange).multilineTextAlignment(.center)
                        }
                        if !result.backupPath.isEmpty {
                            if let label = result.backupLabel {
                                Text(label + " BACKUP").font(pixel(9)).foregroundStyle(yellow)
                            }
                            Text("BACKUP LOCATION").font(pixel(9)).foregroundStyle(quiet)
                            Text(result.backupPath).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled)
                            PixelButton(title: "SHOW BACKUP") { model.reveal(result.backupPath) }
                            if result.failed {
                                PixelButton(title: "REVIEW BACKUP RESTORE", disabled: model.busy) { model.reviewRecoveryBackup(result.backupPath) }
                            }
                        }
                        if !result.excludedSystemPaths.isEmpty {
                            Text("BACKUP EXCLUSIONS").font(pixel(8)).foregroundStyle(.orange)
                            Text(result.excludedSystemPaths.joined(separator: ", "))
                                .font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center).textSelection(.enabled)
                        }
                        if !result.saveVaultPath.isEmpty {
                            Text("\(result.saveFiles) SAVE FILES PRESERVED").font(pixel(9)).foregroundStyle(yellow)
                            PixelButton(title: "SHOW SAVES") { model.reveal(result.saveVaultPath) }
                        }
                        if let restored = result.restoredSaveFiles {
                            Text("\(restored) SAVE FILES RESTORED").font(pixel(9)).foregroundStyle(.green)
                        }
                        if let restored = result.restoredFiles {
                            Text("\(restored) FILES RESTORED").font(pixel(9)).foregroundStyle(.green)
                        }
                        if let capacity = result.filesystemBytes {
                            Text("\(readableBytes(capacity)) CARD CAPACITY").font(pixel(9)).foregroundStyle(yellow).multilineTextAlignment(.center)
                        }
                        if !result.saveRestoreSkipped.isEmpty {
                            DisclosureGroup {
                                ForEach(Array(result.saveRestoreSkipped.enumerated()), id: \.offset) { _, skipped in
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(skipped.path).foregroundStyle(.white)
                                        Text(skipped.reason).foregroundStyle(quiet)
                                    }.font(.system(size: 10, design: .monospaced)).textSelection(.enabled).padding(.vertical, 4)
                                }
                            } label: {
                                Text("\(result.saveRestoreSkipped.count) SAVES KEPT IN BACKUP").font(pixel(8)).foregroundStyle(.orange)
                            }
                        }
                        ForEach(Array(result.saveRestoreWarnings.enumerated()), id: \.offset) { _, warning in
                            Text(warning).font(.system(size: 10, design: .monospaced)).foregroundStyle(.orange).multilineTextAlignment(.center)
                        }
                        if !result.saveVaultWarning.isEmpty { Text(result.saveVaultWarning).font(.system(size: 10, design: .monospaced)).foregroundStyle(.orange).multilineTextAlignment(.center) }
                    } else {
                        Text("YOUR LIBRARY\nSTAYS HERE").font(pixel(14, bold: true)).lineSpacing(8).multilineTextAlignment(.center)
                        Text("Your library, application and startup disks are protected from replacement.").font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center)
                        if !model.protectedDisks.isEmpty { Text(model.protectedDisks.joined(separator: ", ")).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet) }
                        Text("IMAGES + BACKUPS").font(pixel(10)).foregroundStyle(yellow).padding(.top, 12)
                        Text("Packs and backups stay in your connected library folder.").font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet).multilineTextAlignment(.center)
                    }
                }.frame(minWidth: 0, maxWidth: .infinity)
            }.frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
            PixelButton(title: "BACKUPS & SAVES") { model.openBackups() }
            PixelButton(title: "OPEN LIBRARY", disabled: !model.libraryFolderSelected) { model.openLibrary() }
            PixelButton(title: "CHANGE LIBRARY", disabled: model.busy) { model.connectLibrary() }
        }.padding(20).frame(width: 250).frame(minHeight: 0, maxHeight: .infinity).modifier(Bevel())
    }
    private var progressPanel: some View {
        VStack(spacing: 14) {
            Text(model.operation.progress?.stageLabel ?? "WORKING")
                .font(pixel(12)).foregroundStyle(quiet).multilineTextAlignment(.center)
            if let fraction = model.operation.progress?.fraction {
                PixelProgress(fraction: fraction).frame(height: 28)
                Text("\(Int(fraction * 100))%").font(pixel(12)).foregroundStyle(Color(red: 0.5, green: 1, blue: 0))
            } else { ProgressView().tint(yellow) }
            Text(model.operation.message).font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
        }.padding(.vertical, 6)
    }
    private var imageName: String { model.imagePath.isEmpty ? "Build an SD image in Packs, or choose a verified card backup." : URL(fileURLWithPath: model.imagePath).lastPathComponent }
    private func stepLabel(_ number: String, _ text: String) -> some View {
        HStack(spacing: 10) {
            Text(number).font(pixel(13, bold: true)).foregroundStyle(.black).padding(6).background(yellow)
            Text(text).font(pixel(13, bold: true))
        }
    }
    private var footer: some View {
        HStack(spacing: 11) {
            if model.busy {
                if let fraction = model.operation.progress?.fraction {
                    PixelProgress(fraction: fraction).frame(width: 94, height: 9)
                } else { ProgressView().controlSize(.small).tint(yellow).scaleEffect(0.8) }
            }
            else { Rectangle().fill(model.operation.isFailure ? scarlet : .green).frame(width: 7, height: 7) }
            Text(model.operation.isFailure ? "OPERATION STOPPED" : model.statusMessage.uppercased()).font(pixel(9)).foregroundStyle(model.operation.isFailure ? scarlet : quiet).lineLimit(1)
            Spacer()
            Text(Backend.rootLabel.uppercased()).font(pixel(8)).foregroundStyle(quiet)
            Button { model.openLibrary() } label: { Text("OPEN").font(pixel(8)).foregroundStyle(yellow) }.buttonStyle(.plain)
        }.padding(.horizontal, 16).frame(height: 34).background(Color(white: 0.06))
    }
    private var themePickerSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("CHOOSE A THEME").font(pixel(22, bold: true)).foregroundStyle(yellow)
                Spacer()
                Button { model.showThemePicker = false } label: { Text("X").font(pixel(16)) }.buttonStyle(.plain)
            }
            Text("\(model.profile == "m64" ? "M64" : "CHROMATIC") / EXACT BUILDS, SAVED TOGETHER")
                .font(pixel(9)).foregroundStyle(quiet)
            PixelScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    themeChoice(id: "", name: "FULL LIBRARY", description: "Every build on this shelf.", count: model.releases.count)
                    ForEach(model.collections) { collection in
                        themeChoice(id: collection.id, name: collection.name, description: collection.description ?? "Your saved exact build choices.", count: collection.releaseIds.count)
                    }
                }.padding(2)
            }
            Text("Choose a theme, then preview its games in Packs. Your custom choices stay in Library until you change the set.")
                .font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
        }.padding(25).frame(width: 700, height: 580).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private func themeChoice(id: String, name: String, description: String, count: Int) -> some View {
        let chosen = id.isEmpty ? model.packScope == .fullLibrary : model.collectionId == id
        return Button {
            model.chooseCollection(id); model.showThemePicker = false
        } label: {
            HStack(alignment: .top, spacing: 15) {
                Text(chosen ? ">" : " ").font(pixel(12)).foregroundStyle(yellow)
                VStack(alignment: .leading, spacing: 6) {
                    Text(name).font(pixel(13, bold: true)).foregroundStyle(.white).fixedSize(horizontal: false, vertical: true)
                    Text(description).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color(white: 0.78)).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Text(buildCountLabel(count)).font(pixel(9)).foregroundStyle(yellow).fixedSize()
            }.padding(14).frame(maxWidth: .infinity, alignment: .leading).background(chosen ? Color(white: 0.13) : panel)
                .modifier(Bevel(highlighted: chosen, accent: yellow))
        }.buttonStyle(.plain).disabled(model.draftLocked)
    }
    private var preparedImagesSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("PREPARED IMAGES").font(pixel(22, bold: true)).foregroundStyle(yellow)
                Spacer()
                Button { model.showPreparedImages = false } label: { Text("X").font(pixel(16)) }.buttonStyle(.plain)
            }
            Text("\(model.profile == "m64" ? "M64" : "CHROMATIC") / CHECK BEFORE USE")
                .font(pixel(9)).foregroundStyle(quiet)
            Text("These images passed verification when built. Check the current file before sending it to Card.")
                .font(.system(size: 12, design: .monospaced)).foregroundStyle(Color(white: 0.8)).fixedSize(horizontal: false, vertical: true)
            PixelScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(model.preparedImages) { image in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(image.name).font(pixel(13, bold: true)).foregroundStyle(.white).fixedSize(horizontal: false, vertical: true)
                            Text("\(buildCountLabel(image.gameCount)) / \(readableBytes(image.sizeBytes)) / \(localTimestamp(image.createdAt))")
                                .font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                            HStack(spacing: 12) {
                                Text(URL(fileURLWithPath: image.path).lastPathComponent).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                                    .lineLimit(2).truncationMode(.middle).frame(minWidth: 0, maxWidth: .infinity, alignment: .leading).help(image.path)
                                PixelButton(title: "CHECK & USE", disabled: model.busy) {
                                    model.showPreparedImages = false; model.inspectPreparedImage(image)
                                }.frame(width: 160)
                            }
                        }.padding(14).frame(minWidth: 0, maxWidth: .infinity, alignment: .leading).background(panel).modifier(Bevel())
                    }
                }.padding(2)
            }
        }.padding(25).frame(width: 700, height: 580).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private var collectionSheet: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("SAVE YOUR GAME SET").font(pixel(20, bold: true)).foregroundStyle(yellow)
            Text("\(model.selected.count) exact \(model.selected.count == 1 ? "build" : "builds") for \(model.profile == "m64" ? "M64" : "Chromatic").").font(.system(size: 13, design: .monospaced)).foregroundStyle(quiet)
            Text("A new name keeps the original set.").font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
            TextField("Puzzle night", text: $model.collectionName).font(pixel(16)).textFieldStyle(.plain).padding(14).background(.black).modifier(Bevel()).accessibilityLabel("Collection name").onSubmit { model.saveCollection() }
            HStack(spacing: 12) {
                PixelButton(title: "CANCEL") { model.showCollection = false }
                PixelButton(title: "SAVE SET", primary: true, disabled: model.collectionName.trimmingCharacters(in: .whitespaces).isEmpty) { model.saveCollection() }
            }
        }.padding(28).frame(width: 520).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private var verificationSheet: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("ROM FILE CHECK").font(pixel(20, bold: true)).foregroundStyle(yellow)
            Text("Each file is read and compared with available reference hashes.")
                .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet)
            if let result = model.snapshot?.verificationSummary {
                VStack(spacing: 0) {
                    verificationCount("FILES CHECKED", result.filesChecked, color: .white)
                    verificationCount("REFERENCE MATCHES", result.referenceMatches, color: .green)
                    verificationCount("UNLISTED BUILDS", result.unlisted, color: yellow)
                    verificationCount("NEEDS REVIEW", result.needsReview, color: .orange)
                    verificationCount("INVALID FILES", result.invalid, color: result.invalid == 0 ? quiet : scarlet)
                    verificationCount("CHANGED FILES", result.changed, color: result.changed == 0 ? quiet : scarlet)
                }.modifier(Bevel())
                Text("Unlisted builds remain in your library. Review findings do not remove files. Console playability requires a console test.")
                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 12) {
                    PixelButton(title: "SHOW REPORT") { model.reveal(result.reportPath) }
                    PixelButton(title: "CLOSE", primary: true) { model.showVerification = false }
                }
            }
        }.padding(28).frame(width: 610).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private func verificationCount(_ name: String, _ count: Int, color: Color) -> some View {
        HStack {
            Text(name).font(pixel(10)).foregroundStyle(quiet)
            Spacer()
            Text("\(count)").font(pixel(14, bold: true)).foregroundStyle(color)
        }.padding(.horizontal, 14).padding(.vertical, 12).background(Color(white: 0.055))
    }
    private var managementSheet: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("LIBRARY CHECK").font(pixel(20, bold: true)).foregroundStyle(yellow)
                Spacer()
                Button { model.showManagement = false } label: { Text("X").font(pixel(16)).foregroundStyle(.white) }.buttonStyle(.plain)
            }
            if let plan = model.cleanupPlan {
                Text("\(plan.duplicateFiles) CONFIRMED DUPLICATES / \(readableBytes(plan.duplicateBytes))")
                    .font(pixel(12)).foregroundStyle(.white)
                Text("Keep one copy of each exact build. Extra copies move to your library quarantine and can be restored. Checksum findings stay for review.")
                    .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                PixelScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(plan.moves) { move in
                            VStack(alignment: .leading, spacing: 5) {
                                Text("MOVE  " + move.filename).font(.system(size: 11, design: .monospaced)).foregroundStyle(.white)
                                Text("KEEP  " + URL(fileURLWithPath: move.keptPath).lastPathComponent).font(.system(size: 11, design: .monospaced)).foregroundStyle(yellow)
                                Text(move.source.replacingOccurrences(of: Backend.rootPrefix, with: "")).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled)
                            }.padding(10).frame(maxWidth: .infinity, alignment: .leading).background(Color(white: 0.075))
                        }
                        if plan.moves.isEmpty { Text("NO EXTRA COPIES FOUND").font(pixel(12)).foregroundStyle(quiet).padding(.vertical, 12) }
                        if !plan.warnings.isEmpty {
                            DisclosureGroup {
                                ForEach(plan.warnings) { warning in
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(warning.title).font(.system(size: 12, design: .monospaced)).foregroundStyle(.white)
                                        Text(warning.warnings.joined(separator: "\n")).font(.system(size: 10, design: .monospaced)).foregroundStyle(.orange)
                                        if let path = warning.sources.first?.path { Button("REVEAL FILE") { model.reveal(path) }.font(pixel(8)).buttonStyle(.plain).foregroundStyle(yellow) }
                                    }.padding(.vertical, 6)
                                }
                            } label: { Text("\(plan.warnings.count) BUILDS NEED REVIEW").font(pixel(10)).foregroundStyle(.orange) }
                        }
                        if let issues = plan.issues, !issues.isEmpty {
                            DisclosureGroup {
                                ForEach(issues) { issue in
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(issue.filename).font(.system(size: 11, design: .monospaced)).foregroundStyle(.white)
                                        Text(issue.reason).font(.system(size: 10, design: .monospaced)).foregroundStyle(.orange)
                                        Button("REVEAL FILE") { model.reveal(issue.path) }.font(pixel(8)).buttonStyle(.plain).foregroundStyle(yellow)
                                    }.padding(.vertical, 6)
                                }
                            } label: { Text("\(issues.count) FILES COULD NOT BE INDEXED").font(pixel(10)).foregroundStyle(.orange) }
                        }
                        if let batches = model.snapshot?.management?.quarantineBatches, !batches.isEmpty {
                            Text("QUARANTINE HISTORY / BOTH LIBRARIES").font(pixel(10)).foregroundStyle(quiet).padding(.top, 8)
                            ForEach(batches.reversed()) { batch in
                                HStack(spacing: 12) {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(batch.restored ? "RESTORED" : "\(batch.count) FILES IN QUARANTINE").font(pixel(9)).foregroundStyle(batch.restored ? quiet : .white)
                                        Text(localTimestamp(batch.createdAt)).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                                    }
                                    Spacer()
                                    PixelButton(title: "RESTORE", disabled: model.busy || batch.restored) { model.restoreCleanup(batch.id) }.frame(width: 115)
                                }.padding(10).background(Color(white: 0.075))
                            }
                        }
                    }.padding(12)
                }.modifier(Bevel())
                HStack(spacing: 12) {
                    PixelButton(title: "CLOSE") { model.showManagement = false }
                    PixelButton(title: "VERIFY ROMS", disabled: model.busy) { model.showManagement = false; model.verifyLibrary() }
                    PixelButton(title: "QUARANTINE DUPLICATES", primary: true, disabled: model.busy || plan.duplicateFiles == 0) { model.quarantineDuplicates() }
                }
            }
        }.padding(25).frame(width: 760, height: 640).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private var systemFilesSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("CARTRIDGE SYSTEM FILES").font(pixel(19, bold: true)).foregroundStyle(yellow)
            if let files = model.systemFiles {
                Text(files.profile == "m64" ? "EVERDRIVE-64 PRO" : "EVERDRIVE-GB X7").font(pixel(12)).foregroundStyle(.white)
                Text(files.status == "ready" ? "VERIFIED FILES READY" : files.status == "changed" ? "CACHED FILE NEEDS REVIEW" : "OFFICIAL FILE NEEDED")
                    .font(pixel(12)).foregroundStyle(files.status == "ready" ? .green : .orange)
                Text(files.message).font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                Text("Supported version " + files.version).font(.system(size: 12, design: .monospaced)).foregroundStyle(.white)
                if let path = files.path { Text(URL(fileURLWithPath: path).lastPathComponent).font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet) }
                Text("Required SHA256 " + files.sha256).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled)
                Text(files.profile == "m64" ? "The EFU updater is included in your packs. Run it on the cartridge when an update is needed. Copying the file does not flash the cartridge." : "The verified GB X7 menu OS is copied into packs with the CartShelf palette. The menu loads from the card.")
                    .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet).fixedSize(horizontal: false, vertical: true)
                Text("Download from the official source, then choose the downloaded file. CartShelf checks its bytes before adding it to your library.")
                    .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet)
                if let url = URL(string: files.officialUrl), ["https", "http"].contains(url.scheme ?? "") {
                    Link("OPEN OFFICIAL DOWNLOAD", destination: url).font(pixel(10)).foregroundStyle(yellow)
                }
                if model.busy { progressPanel }
                HStack(spacing: 12) {
                    PixelButton(title: "CLOSE", disabled: model.busy) { model.showSystemFiles = false }
                    PixelButton(title: "IMPORT OFFICIAL FILE", primary: true, disabled: model.busy) { model.importSystemFiles() }
                }
            }
        }.padding(26).frame(width: 680).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private var saveHistorySheet: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("SAVE TIMELINE").font(pixel(20, bold: true)).foregroundStyle(yellow)
            Text(model.historyTitle).font(.system(size: 15, weight: .semibold, design: .monospaced))
            Text("Exact build " + model.historyIdentity.prefix(16)).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
            Text("Inspect or export an archive. Choosing a save applies only to the next reviewed card write. Your current card is backed up first.")
                .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet)
            if model.busy { progressPanel }
            if let exported = model.saveExport {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("EXPORTED \(exported.fileCount) SAVE FILES").font(pixel(10)).foregroundStyle(.green)
                        Text(exported.message).font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
                        Text(URL(fileURLWithPath: exported.path).lastPathComponent).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    PixelButton(title: "SHOW EXPORT", disabled: model.busy) { model.reveal(exported.path) }.frame(width: 160)
                }.padding(12).background(Color(white: 0.08))
            }
            PixelScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if model.saveEntries.isEmpty {
                        Text("NO ARCHIVED SAVES FOR THIS BUILD").font(pixel(11)).foregroundStyle(quiet).padding(.vertical, 24)
                        Text("Verified card operations archive recognized saves. A similarly named game with a different ROM hash has separate history.")
                            .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet)
                    }
                    ForEach(model.saveEntries) { entry in saveHistoryRow(entry) }
                }.padding(4)
            }.modifier(Bevel())
            PixelButton(title: "CLOSE", disabled: model.busy) { model.showSaveHistory = false }
        }.padding(26).frame(width: 760, height: 640).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private func saveHistoryRow(_ entry: SaveHistoryEntry) -> some View {
        let inspected = model.saveInspection?.verified == true && model.saveInspection?.choice == entry.choice
        let selected = model.saveChoices.contains(entry.choice)
        return VStack(alignment: .leading, spacing: 9) {
            HStack {
                Text(localTimestamp(entry.createdAt)).font(pixel(10)).foregroundStyle(.white)
                Spacer()
                Text(selected ? "CHOSEN FOR NEXT WRITE" : inspected ? "BUNDLE VERIFIED" : "ARCHIVE RECORDED")
                    .font(pixel(8)).foregroundStyle(selected ? .orange : inspected ? .green : quiet)
            }
            Text("\(entry.files.count) \(entry.files.count == 1 ? "file" : "files") / \(readableBytes(entry.files.reduce(0) { $0 + $1.bytes }))")
                .font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
            ForEach(Array(entry.files.enumerated()), id: \.offset) { _, file in
                Text((file.key ?? URL(fileURLWithPath: file.path).lastPathComponent).uppercased() + " / " + String(file.sha256.prefix(16)))
                    .font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled)
            }
            HStack(spacing: 9) {
                PixelButton(title: inspected ? "VERIFIED" : "VERIFY", disabled: model.busy || inspected) { model.inspectSave(entry) }
                PixelButton(title: "EXPORT", disabled: model.busy) { model.exportSave(entry) }
                PixelButton(title: "USE IN NEXT WRITE", primary: true, disabled: model.busy || !inspected || entry.profile != model.profile) { model.chooseEarlierSave(entry) }
            }
        }.padding(12).background(Color(white: 0.08))
    }
    private var recipeSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("REVIEW PACK RECIPE").font(pixel(20, bold: true)).foregroundStyle(yellow)
            if let inspection = model.recipeInspection {
                Text(inspection.recipe.name).font(pixel(16)).foregroundStyle(.white)
                Text(inspection.recipe.profile == "m64" ? "M64 / EVERDRIVE-64 PRO" : "CHROMATIC / EVERDRIVE-GB X7")
                    .font(pixel(9)).foregroundStyle(quiet)
                Text(inspection.recipe.description).font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet)
                Text("\(inspection.matchedReleaseIds.count) exact \(inspection.matchedReleaseIds.count == 1 ? "build" : "builds") found / \(inspection.missing.count) missing")
                    .font(pixel(11)).foregroundStyle(inspection.missing.isEmpty ? .green : .orange)
                Text("Only exact revisions are selected. Missing games stay missing. Recipes contain choices and notes, with no ROMs or saves.")
                    .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet)
                PixelScrollView {
                    LazyVStack(alignment: .leading, spacing: 9) {
                        ForEach(inspection.recipe.games) { game in
                            let missing = inspection.missing.contains { $0.sha256 == game.sha256 }
                            HStack(alignment: .top, spacing: 10) {
                                Text(missing ? "?" : "X").font(pixel(12)).foregroundStyle(missing ? .orange : .green)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(game.title).font(.system(size: 12, weight: .semibold, design: .monospaced))
                                    Text(game.version + " / " + game.sha256.prefix(16)).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet)
                                }
                                Spacer()
                                Text(missing ? "MISSING" : "MATCHED").font(pixel(8)).foregroundStyle(missing ? .orange : .green)
                            }.padding(9).background(Color(white: 0.07))
                        }
                    }.padding(3)
                }.modifier(Bevel())
                HStack(spacing: 12) {
                    PixelButton(title: "CANCEL") { model.showRecipe = false }
                    PixelButton(title: "IMPORT \(inspection.matchedReleaseIds.count) MATCHED \(inspection.matchedReleaseIds.count == 1 ? "BUILD" : "BUILDS")", primary: true, disabled: model.busy || inspection.matchedReleaseIds.isEmpty) { model.importRecipe() }
                }
            }
        }.padding(26).frame(width: 740, height: 620).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    private var cardLabelSheet: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("NAME THIS CARD").font(pixel(20, bold: true)).foregroundStyle(yellow)
            Text("Names help you recognize a volume. The live device identity and capacity still control every write.")
                .font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet)
            TextField("Weekend card", text: $model.cardLabel).font(pixel(13)).textFieldStyle(.plain).padding(12).background(.black).modifier(Bevel()).accessibilityLabel("Card name")
            HStack(spacing: 12) {
                PixelButton(title: "CANCEL") { model.showCardLabel = false }
                PixelButton(title: "SAVE NAME", primary: true, disabled: model.cardLabel.count > 64) { model.saveCardLabel() }
            }
        }.padding(26).frame(width: 560).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }
    @ViewBuilder private func replacementPreview(_ preview: ReplacementPreview) -> some View {
        if let summary = preview.summary { Text(summary).font(.system(size: 12, design: .monospaced)).foregroundStyle(quiet) }
        if let games = preview.games {
            previewGames("ADDED", games.added, color: .green)
            previewGames("RETAINED", games.retained, color: quiet)
            previewGames("REMOVED FROM CARD", games.removed, color: .orange)
        }
        if let saves = preview.saves {
            previewSaves("FROM THIS CARD", saves.current, color: .green)
            previewSaves("FROM VERIFIED HISTORY", saves.history, color: yellow)
            previewSaves("YOUR EARLIER SAVE CHOICES", saves.selectedEarlier, color: .orange)
            previewSaves("PRESERVED WITHOUT AUTOMATIC MAPPING", saves.keptInBackup, color: quiet)
            ForEach(Array(saves.warnings.enumerated()), id: \.offset) { _, warning in
                Text(warning).font(.system(size: 11, design: .monospaced)).foregroundStyle(.orange)
            }
        }
        if let firmware = preview.firmware {
            Text("SYSTEM FILES INCLUDED").font(pixel(10)).foregroundStyle(yellow)
            ForEach(firmware.filesInPack) { file in
                Text(file.path + (file.version.isEmpty ? "" : " / " + file.version)).font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
            }
            Text("Installed cartridge firmware: " + (firmware.installedCartridgeVersion ?? "Unknown") + ". Copying an updater does not run it.")
                .font(.system(size: 11, design: .monospaced)).foregroundStyle(.orange)
        }
    }
    private func previewGames(_ label: String, _ games: [PreviewGame], color: Color) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(games.enumerated()), id: \.offset) { _, game in
                    Text(game.name + ((game.version ?? "").isEmpty ? "" : " / " + (game.version ?? "")))
                        .font(.system(size: 11, design: .monospaced)).foregroundStyle(quiet)
                }
            }.padding(.vertical, 7)
        } label: { Text(buildCountLabel(games.count) + " " + label).font(pixel(10)).foregroundStyle(color) }
    }
    private func previewSaves(_ label: String, _ saves: [PreviewSave], color: Color) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(saves.enumerated()), id: \.offset) { _, save in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(save.path ?? "Save bundle").foregroundStyle(.white)
                        if let reason = save.reason { Text(reason).foregroundStyle(quiet) }
                    }.font(.system(size: 10, design: .monospaced)).textSelection(.enabled)
                }
            }.padding(.vertical, 7)
        } label: { Text("\(saves.count) \(saves.count == 1 ? "SAVE FILE" : "SAVE FILES") \(label)").font(pixel(9)).foregroundStyle(color) }
    }

    private var installSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(model.plan?.isBackupRestore == true ? "RESTORE CARD BACKUP" : "REVIEW CARD REPLACEMENT").font(pixel(20, bold: true)).foregroundStyle(scarlet)
            if let plan = model.plan {
                Text("\(plan.device.name)  /  \(plan.device.id)  /  \(readableBytes(plan.device.sizeBytes))").font(pixel(13)).foregroundStyle(.white)
                if let identity = plan.device.identity { Text("LIVE DEVICE ID " + identity.prefix(24)).font(.system(size: 10, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled) }
                PixelScrollView {
                    VStack(alignment: .leading, spacing: 13) {
                        if let preview = plan.preview { replacementPreview(preview) }
                        Divider().overlay(quiet)
                        Text(plan.imageKind == "file-backup" ? "FILE BACKUP TO RESTORE" : "IMAGE").font(pixel(10)).foregroundStyle(quiet)
                        Text(plan.imagePath).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                        Text("SHA256 " + plan.imageSha256).font(.system(size: 9, design: .monospaced)).foregroundStyle(quiet).textSelection(.enabled)
                        Text(plan.backupLabel + " BACKUP WILL BE SAVED TO").font(pixel(10)).foregroundStyle(yellow)
                        Text(plan.backupPath).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                        Text(plan.replacementSummary).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color(white: 0.72))
                        if let description = plan.backupScopeDescription { Text(description).font(.system(size: 11, design: .monospaced)).foregroundStyle(.orange) }
                    }.padding(4)
                }.modifier(Bevel())
                HStack(spacing: 12) {
                    PixelButton(title: "CANCEL") { model.showPlan = false; model.plan = nil }
                    PixelButton(title: plan.isBackupRestore ? "BACK UP & RESTORE" : "BACK UP & REPLACE", primary: true, danger: true) { model.install() }
                }
            }
        }.padding(26).frame(width: 790, height: 680).background(.black).foregroundStyle(.white).modifier(Bevel(highlighted: true, accent: yellow))
    }

}

@MainActor final class CartShelfDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if AppModel.active?.operation.allowsQuit == false {
            NSSound.beep()
            return .terminateCancel
        }
        AppModel.active?.savePreferencesNow()
        return .terminateNow
    }
}

@main struct CartShelfApp: App {
    @NSApplicationDelegateAdaptor(CartShelfDelegate.self) private var delegate
    @StateObject private var model = AppModel()
    init() {
        for name in ["Silkscreen-Regular", "Silkscreen-Bold"] {
            if let url = Bundle.main.url(forResource: name, withExtension: "ttf") {
                CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
            }
        }
        NSApplication.shared.setActivationPolicy(.regular)
    }
    var body: some Scene {
        WindowGroup("CartShelf") { CartShelfView(model: model).preferredColorScheme(.dark) }
            .windowStyle(.hiddenTitleBar)
            .defaultSize(width: 1100, height: 820)
            .commands {
                CommandGroup(replacing: .newItem) {
                    Button("Connect Library...") { model.connectLibrary() }.keyboardShortcut("o").disabled(model.busy || model.hasOpenSheet)
                    Button("Create Library...") { model.connectLibrary(create: true) }.disabled(model.busy || model.hasOpenSheet)
                }
                CommandMenu("Navigate") {
                    Button("Library") { model.tab = .library }.keyboardShortcut("1").disabled(model.draftLocked)
                    Button("Packs") { model.tab = .packs }.keyboardShortcut("2").disabled(model.draftLocked)
                    Button("Card") { model.tab = .card }.keyboardShortcut("3").disabled(model.draftLocked)
                }
                CommandGroup(replacing: .appTermination) {
                    Button("Quit CartShelf") { NSApp.terminate(nil) }.keyboardShortcut("q").disabled(!model.operation.allowsQuit)
                }
                CommandGroup(after: .textEditing) {
                    Button("Find in library") { model.tab = .library; model.searchFocusRequest = UUID() }
                        .keyboardShortcut("f").disabled(model.busy && !model.operation.allowsBrowsing)
                }
                CommandGroup(after: .appInfo) {
                    Button("Licenses and notices") {
                        if let notice = Bundle.main.url(forResource: "THIRD_PARTY_NOTICES", withExtension: "md") { NSWorkspace.shared.open(notice) }
                    }
                    Button("Open library folder") { model.openLibrary() }
                    Button("Open Chromatic updater") { NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/ChromaticFirmwareUpdater.app")) }
                }
            }
    }
}
