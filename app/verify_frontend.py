#!/usr/bin/env python3
"""Exercise native model behavior using authored fixtures, without a personal library."""
from pathlib import Path
import hashlib
import json
import platform
import shutil
import plistlib
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/native-verification'
HARNESS = r'''
private var verificationIndexBuilds = 0
struct CatalogEnvelope: Decodable { let snapshot: LibrarySnapshot }
@main struct FrontendCheck {
    @MainActor static func main() throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let checkout = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: checkout, withIntermediateDirectories: true)
        _ = try LibraryConnection(checkout)
        try FileManager.default.createDirectory(at: checkout.appendingPathComponent(".git"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: checkout) }
        do {
            _ = try LibraryConnection(checkout)
            preconditionFailure("A Git repository must not become a library")
        } catch {}
        let snapshot = try Backend.decode(CatalogEnvelope.self, data: Data(contentsOf: root.appendingPathComponent("library/catalog.json"))).snapshot
        let state = BrowserPreferences(profile: "chromatic", search: "", focusedGroup: nil,
            focusedRelease: nil, selected: [], collectionId: "", packName: "Fixture set",
            packFormat: "image", tab: "LIBRARY", filter: "ALL", packScope: .selection)
        let model = AppModel(preferences: state)
        model.snapshot = snapshot
        var selections = 0
        for profile in ["chromatic", "m64"] {
            model.changeProfile(profile)
            let initialBuilds = verificationIndexBuilds
            for group in model.groups {
                model.focus(group)
                precondition(model.group?.id == group.id)
                for release in group.releases {
                    model.focusBuild(release)
                    precondition(model.release?.id == release.id)
                    model.toggle(release)
                    precondition(model.selected.contains(release.id))
                    model.toggle(release)
                    precondition(!model.selected.contains(release.id))
                    selections += 1
                }
            }
            precondition(verificationIndexBuilds == initialBuilds)
            let release = model.releases[0]
            model.selected = [release.id]; model.packScope = .selection; model.filter = .inSet
            precondition(model.groups.count == 1)
            model.search = "missing fixture"
            precondition(model.groups.isEmpty)
            model.search = ""
            model.clearSelection()
            precondition(model.packScope == .selection && model.packReleases.isEmpty && !model.canPrepare)
        }
        let history = try Backend.decode(SaveHistoryInventory.self, data: Data(contentsOf: root.appendingPathComponent("history.json")))
        let entry = history.entries[0]
        precondition(entry.files.count == 2)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        let encoded = try encoder.encode([entry.choice])
        let rawChoice = try JSONSerialization.jsonObject(with: encoded) as! [[String: Any]]
        precondition(Set(rawChoice[0].keys) == Set(["profile", "identity_sha256", "snapshot_id", "bundle_sha256"]))
        model.changeProfile("chromatic")
        model.saveInspection = SaveInspection(choice: entry.choice, files: entry.files, createdAt: entry.createdAt, verified: true)
        model.chooseEarlierSave(entry)
        precondition(model.saveChoices == [entry.choice] && !model.showSaveHistory)
        model.clearSaveChoices(); precondition(model.saveChoices.isEmpty)
        model.saveChoices = [entry.choice]; model.changeProfile("m64"); precondition(model.saveChoices.isEmpty)
        let recipe = try Backend.decode(RecipeInspection.self, data: Data(contentsOf: root.appendingPathComponent("recipe.json")))
        precondition(recipe.recipe.games.count == 2 && recipe.missing.count == 1 && recipe.matchedReleaseIds.count == 1)
        let recipeBytes = try JSONEncoder().encode(recipe.recipe)
        let publicRecipe = try JSONSerialization.jsonObject(with: recipeBytes) as! [String: Any]
        precondition(Set(publicRecipe.keys) == Set(["schema", "kind", "name", "profile", "description", "games"]))
        let plan = try Backend.decode(InstallPlan.self, data: Data(contentsOf: root.appendingPathComponent("plan.json")))
        precondition(plan.device.volumeUuid == "11111111-2222-3333-4444-555555555555")
        precondition(plan.preview?.games?.added.count == 1 && plan.preview?.games?.removed.count == 1)
        precondition(plan.preview?.saves?.selectedEarlier.count == 1)
        precondition(plan.preview?.firmware?.installedCartridgeVersion == nil)
        precondition(plan.backupLabel == "CARD FILES" && !plan.isBackupRestore)
        model.reviewRecoveryBackup("fixture-backup.files")
        precondition(model.cardSourceIsBackupFolder && model.imageProfile == nil && model.tab == .card && model.plan == nil)
        let event = ProgressEvent(event: "progress", phase: "verify-source-files", completed: 1, total: 2, message: "Checking")
        precondition(event.stageLabel == "VERIFYING THE BACKUP" && event.fraction == 0.5)
        let failure = Backend.readableFailure(response: Data(#"{"error":"The recipe changed after inspection. Review it again."}"#.utf8), diagnostic: #"{"event":"progress","message":"Reading"}"#)
        precondition(failure == "The recipe changed after inspection. Review it again.")
        let missingSystem = try Backend.decode(SystemFilesStatus.self, data: Data(#"{"profile":"m64","status":"missing","version":"fixture","sha256":"fixture","official_url":"https://example.invalid/official","message":"Fixture only"}"#.utf8))
        precondition(missingSystem.status == "missing" && missingSystem.path == nil)
        let connection = try LibraryConnection(root)
        let validated = try connection.validated()
        precondition(validated == root)
        Backend.savePreferences(state, connection: connection)
        precondition(Backend.preferences(connection: connection)?.packName == state.packName)
        let target = root.appendingPathComponent("library/ui-state.json")
        try FileManager.default.removeItem(at: target)
        let outside = root.deletingLastPathComponent().appendingPathComponent("outside-state.json")
        try Data("unchanged".utf8).write(to: outside)
        try FileManager.default.createSymbolicLink(at: target, withDestinationURL: outside)
        Backend.savePreferences(state, connection: connection)
        let outsideText = try String(contentsOf: outside)
        precondition(outsideText == "unchanged")
        precondition(Backend.preferences(connection: connection) == nil)
        try FileManager.default.removeItem(at: target)
        let displaced = root.deletingLastPathComponent().appendingPathComponent("displaced-library")
        try FileManager.default.moveItem(at: root, to: displaced)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        var refusedReplacement = false
        do { _ = try connection.validated() } catch { refusedReplacement = true }
        precondition(refusedReplacement)
        try FileManager.default.removeItem(at: root)
        try FileManager.default.moveItem(at: displaced, to: root)
        print("{\"fixture_only\":true,\"native_selections\":\(selections),\"empty_selection_stays_empty\":true,\"save_choice_roundtrip\":true,\"profile_choice_invalidated\":true,\"recipe_schema\":true,\"preview_provenance\":true,\"library_replacement_refused\":true,\"preference_symlink_escape_refused\":true,\"native_paint_latency_measured\":false}")
    }
}
'''


def write_fixtures(work: Path) -> Path:
    library = work / 'fixture-library'
    (library / 'library').mkdir(parents=True, exist_ok=True)
    releases = []
    for index, (profile, title, version) in enumerate([
        ('chromatic', 'Clockwork Orchard', '1.0'),
        ('chromatic', 'Orbit Courier', '1.0'),
        ('chromatic', 'Orbit Courier', '1.1'),
        ('m64', 'Polygon Picnic', '1.0'),
        ('m64', 'Vertex Voyage', '1.0'),
    ], 1):
        identity = f'{index:064x}'
        releases.append({'id': profile + ':' + identity, 'profile': profile, 'title': title,
                         'version': version, 'identity_sha256': identity, 'game_code': '',
                         'artwork_path': '', 'region': 'World', 'verified': True, 'warnings': [],
                         'sources': [{'path': 'fixture-only', 'filename': title + '.fixture',
                                      'raw_sha256': identity, 'size_bytes': 32768,
                                      'export_path': title + '.fixture'}]})
    profiles = [{'id': profile, 'console': console, 'cartridge': cart,
                 'file_count': sum(r['profile'] == profile for r in releases),
                 'release_count': sum(r['profile'] == profile for r in releases),
                 'title_count': len({r['title'] for r in releases if r['profile'] == profile})}
                for profile, console, cart in [('chromatic', 'Chromatic', 'EverDrive-GB X7'), ('m64', 'M64', 'EverDrive-64 PRO')]]
    snapshot = {'project_root': str(library), 'profiles': profiles, 'releases': releases,
                'collections': [], 'metadata_version': 'authored-fixture', 'prepared_images': []}
    (library / 'library/catalog.json').write_text(json.dumps({'snapshot': snapshot}))
    files = [{'key': key, 'path': 'files/0000/' + key, 'bytes': size, 'sha256': f'{i:064x}'}
             for i, (key, size) in enumerate([('srm', 32768), ('rtc', 48)], 11)]
    entry = {'snapshot_id': 'a' * 32, 'profile': 'chromatic', 'identity_sha256': releases[0]['identity_sha256'],
             'created_at': '2026-01-01T12:00:00+00:00', 'bundle_sha256': 'b' * 64,
             'files': files, 'source_roms': ['Clockwork Orchard.fixture'], 'verified': False}
    (library / 'history.json').write_text(json.dumps({'entries': [entry]}))
    games = [{'sha256': r['identity_sha256'], 'title': r['title'], 'version': r['version']} for r in releases[:2]]
    recipe = {'schema': 1, 'kind': 'cartshelf-recipe', 'name': 'A fixture afternoon', 'profile': 'chromatic',
              'description': 'Authored examples, with no ROM or save bytes.', 'games': games}
    (library / 'recipe.json').write_text(json.dumps({'sha256': 'c' * 64, 'recipe': recipe,
                                                  'matched_release_ids': [releases[0]['id']], 'missing': games[1:]}))
    preview = {'profile': 'chromatic', 'games': {'added': [{'identity_sha256': games[0]['sha256'], 'title': games[0]['title'], 'version': '1.0', 'path': 'GB/Example.fixture'}], 'retained': [], 'removed': [{'title': 'Old fixture', 'path': 'GB/Old.fixture'}]},
               'saves': {'current': [], 'history': [], 'selected_earlier': [{'path': 'GBCSYS/save/Example.srm', 'bytes': 32768}], 'kept_in_backup': [], 'warnings': []},
               'source_fingerprint': 'd' * 64, 'firmware': {'files_in_pack': [{'path': 'GBCSYS/GBCOS.BIN', 'version': '1.06', 'sha256': 'e' * 64}], 'installed_cartridge_version': None, 'installed_status': 'unknown'}}
    plan = {'plan_path': 'fixture-plan', 'token': 'fixture-token', 'device': {'id': 'fixture-device', 'name': 'Authored virtual card', 'size_bytes': 128000000000, 'mount_points': [], 'identity': 'f' * 64, 'volume_uuid': '11111111-2222-3333-4444-555555555555'},
            'backup_path': 'fixture-backup', 'image_path': 'fixture-image', 'image_sha256': '0' * 64,
            'image_kind': 'compilation', 'backup_kind': 'files', 'backup_scope': 'card-files-v1', 'preview': preview}
    (library / 'plan.json').write_text(json.dumps(plan))
    return library


def build_harness(work: Path, fixture_parent: Path, harness: str = HARNESS) -> tuple[Path, Path]:
    work.mkdir(parents=True, exist_ok=True)
    library = write_fixtures(fixture_parent)
    source = (ROOT / 'app/CartShelf.swift').read_text().split('@main struct CartShelfApp: App {')[0]
    source = source.replace('    init(releases: [GameRelease]) {', '    init(releases: [GameRelease]) {\n        verificationIndexBuilds += 1', 1)
    source_path = work / 'NativeCheck.swift'
    source_path.write_text(source + '\n' + harness)
    bundle = work / 'NativeCheck.app'
    executable = bundle / 'Contents/MacOS/NativeCheck'
    executable.parent.mkdir(parents=True, exist_ok=True)
    info = {'CFBundleIdentifier': 'dev.cartshelf.fixture-verification', 'CFBundleExecutable': 'NativeCheck',
            'CFBundlePackageType': 'APPL', 'CartShelfUIPreferences': True,
            'CartShelfQABuild': True, 'CartShelfInitialLibraryRoot': str(library)}
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    resources = bundle / 'Contents/Resources'
    resources.mkdir(exist_ok=True)
    for name in ('Silkscreen-Regular.ttf', 'Silkscreen-Bold.ttf', 'OFL.txt'):
        shutil.copyfile(ROOT / 'app/Resources' / name, resources / name)
    subprocess.run(['xcrun', 'swiftc', '-O', '-parse-as-library', '-swift-version', '5',
                    '-target', f'{platform.machine()}-apple-macos14.0', str(source_path), '-o', str(executable),
                    '-framework', 'SwiftUI', '-framework', 'AppKit', '-framework', 'CoreText', '-framework', 'ImageIO'], check=True)
    return executable, library


def build_review_preview(work: Path, fixture_parent: Path) -> Path:
    """Build a display-only review fixture without launching it or accessing cards."""
    harness = r'''
private var verificationIndexBuilds = 0
struct CatalogEnvelope: Decodable { let snapshot: LibrarySnapshot }
@main @MainActor struct ReviewFixtureApp: App {
    @StateObject private var model: AppModel
    init() {
        for name in ["Silkscreen-Regular", "Silkscreen-Bold"] {
            if let url = Bundle.main.url(forResource: name, withExtension: "ttf") { CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil) }
        }
        NSApplication.shared.setActivationPolicy(.regular)
        do {
            let root = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "CartShelfInitialLibraryRoot") as! String)
            let snapshot = try Backend.decode(CatalogEnvelope.self, data: Data(contentsOf: root.appendingPathComponent("library/catalog.json"))).snapshot
            let plan = try Backend.decode(InstallPlan.self, data: Data(contentsOf: root.appendingPathComponent("plan.json")))
            let state = BrowserPreferences(profile: "chromatic", search: "", focusedGroup: nil, focusedRelease: nil, selected: [], collectionId: "", packName: "Authored review fixture", packFormat: "image", tab: "CARD", filter: "ALL", packScope: .selection)
            let value = AppModel(preferences: state)
            value.snapshot = snapshot; value.cards = [plan.device]; value.selectedCard = plan.device.id
            value.imagePath = plan.imagePath; value.plan = plan; value.showPlan = true
            _model = StateObject(wrappedValue: value)
        } catch { fatalError("The authored review fixture failed to load: \(error)") }
    }
    var body: some Scene { WindowGroup("CartShelf review fixture") { CartShelfView(model: model).preferredColorScheme(.dark) }.windowStyle(.hiddenTitleBar).defaultSize(width: 1100, height: 820) }
}
'''
    work.mkdir(parents=True, exist_ok=True)
    library = write_fixtures(fixture_parent)
    source = (ROOT / 'app/CartShelf.swift').read_text().split('@main struct CartShelfApp: App {')[0]
    source = source.replace('.task { model.start(); browserFocused = true }', '.task { browserFocused = true }')
    start = source.index('    func install() {')
    end = source.index('    func backupCard() {', start)
    source = source[:start] + '    func install() { operation = .idle("Display-only fixture. No card is accessed.") }\n' + source[end:]
    source_path = work / 'ReviewFixture.swift'
    source_path.write_text(source + '\n' + harness)
    bundle = work / 'ReviewFixture.app'
    executable = bundle / 'Contents/MacOS/ReviewFixture'
    executable.parent.mkdir(parents=True, exist_ok=True)
    info = {'CFBundleIdentifier': 'dev.cartshelf.review-fixture', 'CFBundleExecutable': 'ReviewFixture', 'CFBundlePackageType': 'APPL', 'CartShelfUIPreferences': False, 'CartShelfQABuild': True, 'CartShelfInitialLibraryRoot': str(library), 'CartShelfVersion': 'REVIEW FIXTURE'}
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    resources = bundle / 'Contents/Resources'
    resources.mkdir(exist_ok=True)
    for name in ('Silkscreen-Regular.ttf', 'Silkscreen-Bold.ttf', 'OFL.txt'):
        shutil.copyfile(ROOT / 'app/Resources' / name, resources / name)
    subprocess.run(['xcrun', 'swiftc', '-O', '-parse-as-library', '-swift-version', '5', '-target', f'{platform.machine()}-apple-macos14.0', str(source_path), '-o', str(executable), '-framework', 'SwiftUI', '-framework', 'AppKit', '-framework', 'CoreText', '-framework', 'ImageIO'], check=True)
    return bundle


def main():
    with tempfile.TemporaryDirectory(prefix='cartshelf-native-fixtures-') as temporary:
        executable, library = build_harness(WORK, Path(temporary))
        result = subprocess.run([str(executable), str(library)], capture_output=True, text=True, check=True, timeout=30)
        checked = json.loads(result.stdout)
    checked['swift_source_sha256'] = hashlib.sha256((ROOT / 'app/CartShelf.swift').read_bytes()).hexdigest()
    (WORK / 'result.json').write_text(json.dumps(checked, indent=2) + '\n')
    print(json.dumps(checked, indent=2))


if __name__ == '__main__':
    main()
