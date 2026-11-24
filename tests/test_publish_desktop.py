import base64
import copy
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('publisher', Path(__file__).parents[1] / 'scripts/publish_desktop.py')
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


def document():
    return {'filenum': 58, 'linux': {}, 'macos': {}, 'windows': {
        channel: {'version': '4.8.3', 'link': 'old', 'link_silent': 'unchanged',
                  'hash_silent': 'old', 'details': 'keep', 'update': {'warnalways': 'keep'}}
        for channel in ('beta', 'stable', 'beta_arm', 'stable_arm')}}


class Cloud:
    def __init__(self, build=100):
        self.build = build
        self.downloads = []
        self.missing = set()
        self.bad_hash = False

    def listing(self, platform):
        names = list(p.PLATFORMS[platform])
        if platform == 'Windows':
            names += p.ZIPS + [x + '.sha256' for x in p.ZIPS]
        return {p.source_name(name, '4.9.0', number): '' for name in names
                for number in (99, self.build)}.keys() - self.missing

    def download(self, platform, source, target):
        self.downloads.append(source)
        if target.name.endswith('.sha256'):
            payload = target.name.removesuffix('.sha256').encode()
            digest = '0' * 64 if self.bad_hash else hashlib.sha256(payload).hexdigest()
            target.write_text(digest + '  original-versioned-file.zip\n')
        else:
            target.write_bytes(target.name.encode())


class FakeGitHub(p.GitHub):
    def __init__(self):
        super().__init__('Happ-proxy/happ-desktop', 'fake')
        self.releases = {'silent_update': {'id': 1, 'tag_name': 'silent_update', 'draft': False}}
        self.documents = {'main': document()}
        self.prs = []
        self.uploads = []
        self.writes = []
        self.calls = []
        self.asset_names = {}
        self.asset_digests = {}
        self.conflicts = 0
        self.fail_upload = None
        self.fail_completed = False

    def api(self, path, method='GET', data=None):
        self.calls.append((path, method, copy.deepcopy(data)))
        if path.startswith('releases/tags/'):
            tag = path.removeprefix('releases/tags/')
            if tag not in self.releases:
                raise p.ApiError(404, 'Not found')
            return copy.deepcopy(self.releases[tag])
        if path.startswith('pulls?'):
            return copy.deepcopy(self.prs)
        if path.startswith('contents/release?ref='):
            from urllib.parse import unquote
            branch = unquote(path.split('=', 1)[1])
            return {'content': base64.b64encode(json.dumps(self.documents[branch]).encode()).decode(), 'sha': 'current'}
        if path == 'contents/release' and method == 'PUT':
            if self.conflicts:
                self.conflicts -= 1
                self.documents[data['branch']]['filenum'] += 1
                raise p.ApiError(409, 'Concurrent change')
            self.documents[data['branch']] = json.loads(base64.b64decode(data['content']))
            self.writes.append(data['branch'])
            return {}
        if path == 'releases' and method == 'POST':
            result = dict(data, id=2, html_url='https://github.com/release')
            self.releases[data['tag_name']] = result
            return copy.deepcopy(result)
        if path.startswith('releases/') and '/assets' in path:
            release_id = int(path.split('/')[1])
            return [{'name': name, 'state': 'uploaded',
                     'digest': self.asset_digests.get((release_id, name))}
                    for name in self.asset_names.get(release_id, set())]
        if path.startswith('releases/') and method == 'PATCH':
            if self.fail_completed and '"completed": true' in data.get('body', ''):
                self.fail_completed = False
                raise p.ApiError(500, 'Failed final metadata update')
            release = next(r for r in self.releases.values() if r['id'] == int(path.split('/')[1]))
            release.update(data)
            return copy.deepcopy(release)
        if path.startswith('git/ref/heads/'):
            branch = path.removeprefix('git/ref/heads/')
            if branch not in self.documents:
                raise p.ApiError(404, 'Not found')
            return {'object': {'sha': 'main-head'}}
        if path == 'git/refs':
            branch = data['ref'].removeprefix('refs/heads/')
            self.documents[branch] = copy.deepcopy(self.documents['main'])
            return {}
        if path == 'pulls' and method == 'POST':
            pr = {'head': {'ref': data['head']}, 'html_url': 'https://github.com/pr/1'}
            self.prs.append(pr)
            return copy.deepcopy(pr)
        raise AssertionError((path, method, data))

    def upload(self, tag, path, replace=False):
        if tag == 'silent_update':
            assert not replace, 'Silent update files must never be replaced'
        if self.fail_upload == path.name:
            self.fail_upload = None
            raise p.PublishError('Upload interrupted')
        self.uploads.append((tag, path.name))
        self.asset_names.setdefault(self.releases[tag]['id'], set()).add(path.name)
        self.asset_digests[(self.releases[tag]['id'], path.name)] = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.gh = FakeGitHub()
        self.cloud = Cloud()
        log_patch = patch.object(p, 'log')
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def run_publish(self, stable=False, auto=True):
        p.publish(self.gh, self.cloud, '4.9.0', stable, auto, 'comming soon', self.directory)

    def test_numeric_selection_and_exact_version(self):
        listings = {x: self.cloud.listing(x) for x in p.PLATFORMS}
        listings['Windows'].add('setup-Happ.x64.v14.9.0(999).exe')
        listings['Linux'].add('Happ.linux.x64.v4.9.0(348).deb')
        self.assertEqual(p.choose_builds(listings, '4.9.0'), {'Windows': 100, 'Linux': 348, 'macOS': 100})

    def test_final_names(self):
        self.run_publish()
        expected = {'setup-Happ.arm64.exe', 'setup-Happ.x64.exe', 'Happ.macOS.universal.dmg',
                    'Happ.linux.arm64.deb', 'Happ.linux.arm64.rpm', 'Happ.linux.arm64.pkg.tar.zst',
                    'Happ.linux.x64.deb', 'Happ.linux.x64.rpm', 'Happ.linux.x64.pkg.tar.zst'}
        self.assertEqual({name for _, name in self.gh.uploads}, expected)

    def test_incomplete_newest_build_does_not_fall_back(self):
        self.cloud.missing.add('Happ.linux.x64.v4.9.0(100).rpm')
        with self.assertRaisesRegex(p.PublishError, 'Incomplete Linux'):
            self.run_publish(True)
        self.assertFalse(self.gh.uploads)
        self.assertNotIn('4.9.0', self.gh.releases)

    def test_bad_hash_prevents_all_mutations(self):
        self.cloud.bad_hash = True
        with self.assertRaisesRegex(p.PublishError, 'SHA256 mismatch'):
            self.run_publish(True)
        self.assertFalse(any(method != 'GET' for _, method, _ in self.gh.calls))

    def test_missing_matching_checksum(self):
        self.cloud.missing.add('update-Happ.x64.v4.9.0(100).zip.sha256')
        with self.assertRaisesRegex(p.PublishError, 'missing update-Happ'):
            self.run_publish(True)

    def test_prerelease_ignores_auto_setting_and_missing_zips(self):
        for auto in (True, False):
            with self.subTest(auto=auto):
                self.gh = FakeGitHub()
                self.cloud.missing.add('update-Happ.x64.v4.9.0(100).zip')
                self.run_publish(False, auto)
                self.assertTrue(self.gh.releases['4.9.0']['prerelease'])
                self.assertEqual(self.gh.releases['4.9.0']['make_latest'], 'false')
                self.assertFalse(self.gh.writes)
                self.assertFalse(self.gh.prs)
                self.assertTrue(all(tag == '4.9.0' for tag, _ in self.gh.uploads))

    def test_stable_auto_commits_only_main_once(self):
        before = copy.deepcopy(self.gh.documents['main'])
        self.run_publish(True)
        self.assertEqual(self.gh.writes, ['main'])
        self.assertFalse(self.gh.prs)
        result = self.gh.documents['main']
        self.assertEqual(result['filenum'], 59)
        for channel, block in result['windows'].items():
            arch = 'arm64' if channel.endswith('_arm') else 'x64'
            self.assertEqual(block['version'], '4.9.0')
            self.assertTrue(block['link'].endswith(f'/4.9.0/setup-Happ.{arch}.exe'))
            self.assertEqual(block['hash_silent'], hashlib.sha256(f'update-Happ.{arch}.zip'.encode()).hexdigest())
            self.assertEqual(block['link_silent'],
                             f'https://github.com/Happ-proxy/happ-desktop/releases/download/'
                             f'silent_update/update-Happ.{arch}.v4.9.0%28100%29.zip')
            for key in ('details', 'update'):
                self.assertEqual(block[key], before['windows'][channel][key])
        self.assertEqual(result['linux'], before['linux'])
        self.assertEqual(result['macos'], before['macos'])
        self.assertEqual(self.gh.uploads[-2:], [('silent_update', p.source_name(name, '4.9.0', 100))
                                              for name in p.ZIPS])
        self.run_publish(True)
        self.assertEqual(self.gh.writes, ['main'])

    def test_stable_pr_leaves_main_unchanged(self):
        self.run_publish(True, False)
        self.assertEqual(self.gh.documents['main']['filenum'], 58)
        self.assertEqual(self.gh.documents['codex/release-4.9.0']['filenum'], 59)
        self.assertEqual(len(self.gh.prs), 1)
        self.run_publish(True, False)
        self.assertEqual(len(self.gh.prs), 1)
        self.assertEqual(len(self.gh.writes), 1)

    def test_open_pr_blocks_auto_and_other_versions(self):
        self.run_publish(True, False)
        for auto, branch in ((True, 'codex/release-4.9.0'), (False, 'codex/release-4.8.0')):
            self.gh.prs[0]['head']['ref'] = branch
            count = len(self.gh.uploads)
            with self.assertRaisesRegex(p.PublishError, 'First resolve'):
                self.run_publish(True, auto)
            self.assertEqual(len(self.gh.uploads), count)

    def test_promotion_preserves_installers_and_uses_original_build(self):
        self.run_publish()
        self.cloud.build = 200
        # Keep the original build available alongside the newer one.
        listing = self.cloud.listing
        self.cloud.listing = lambda platform: listing(platform) | Cloud(100).listing(platform)
        self.cloud.downloads.clear()
        self.run_publish(True)
        self.assertEqual(len(self.gh.uploads), 11)
        self.assertEqual(len(self.cloud.downloads), 4)
        self.assertTrue(all('(100)' in name for name in self.cloud.downloads))
        self.assertFalse(self.gh.releases['4.9.0']['prerelease'])

    def test_missing_metadata_fails_without_mutation(self):
        self.run_publish()
        self.gh.releases['4.9.0']['body'] = 'Manually removed metadata'
        count = len(self.gh.uploads)
        with self.assertRaisesRegex(p.PublishError, 'metadata'):
            self.run_publish(True)
        self.assertEqual(len(self.gh.uploads), count)

    def test_retry_after_partial_draft_upload(self):
        self.gh.fail_upload = 'setup-Happ.x64.exe'
        with self.assertRaises(p.PublishError):
            self.run_publish(True)
        self.assertTrue(self.gh.releases['4.9.0']['draft'])
        self.assertFalse(self.gh.writes)
        self.run_publish(True)
        self.assertEqual(self.gh.documents['main']['filenum'], 59)

    def test_retry_after_partial_silent_upload(self):
        self.gh.fail_upload = 'update-Happ.x64.v4.9.0(100).zip'
        with self.assertRaises(p.PublishError):
            self.run_publish(True)
        self.assertFalse(self.gh.releases['4.9.0']['draft'])
        self.assertFalse(self.gh.writes)
        self.run_publish(True)
        self.assertEqual(self.gh.writes, ['main'])
        self.assertEqual(sum(tag == '4.9.0' for tag, _ in self.gh.uploads), 9)
        self.assertEqual(sum(tag == 'silent_update' for tag, _ in self.gh.uploads), 2)

    def test_older_silent_assets_are_preserved(self):
        old_names = {'update-Happ.x64.zip', 'update-Happ.x64.v4.8.3(90).zip', 'other.txt'}
        self.gh.asset_names[1] = set(old_names)
        self.run_publish(True)
        self.assertTrue(old_names <= self.gh.asset_names[1])
        self.assertEqual(len(self.gh.asset_names[1]), len(old_names) + 2)
        self.assertFalse(any(name in old_names for tag, name in self.gh.uploads))

    def test_conflicting_existing_silent_asset_is_not_replaced(self):
        name = 'update-Happ.x64.v4.9.0(100).zip'
        self.gh.asset_names[1] = {name}
        self.gh.asset_digests[(1, name)] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(p.PublishError, 'different contents'):
            self.run_publish(True)
        self.assertFalse(self.gh.uploads)
        self.assertFalse(self.gh.writes)
        self.assertFalse(any(method != 'GET' for _, method, _ in self.gh.calls))

    def test_existing_asset_without_digest_is_downloaded_for_verification(self):
        name = 'update-Happ.x64.v4.9.0(100).zip'
        payload = b'existing ZIP contents'
        def download(command, check):
            self.assertEqual(command[:3], ['gh', 'release', 'download'])
            (Path(command[-1]) / name).write_bytes(payload)
        with patch.object(p.subprocess, 'run', side_effect=download):
            self.gh.verify_existing_asset('silent_update', {'name': name, 'state': 'uploaded'},
                                          hashlib.sha256(payload).hexdigest())

    def test_retry_after_commit_does_not_increment_twice(self):
        self.gh.fail_completed = True
        with self.assertRaises(p.ApiError):
            self.run_publish(True)
        self.run_publish(True)
        self.assertEqual(self.gh.documents['main']['filenum'], 59)
        self.assertEqual(self.gh.writes, ['main'])

    def test_retry_after_pr_creation_does_not_duplicate_pr(self):
        self.gh.fail_completed = True
        with self.assertRaises(p.ApiError):
            self.run_publish(True, False)
        self.run_publish(True, False)
        self.assertEqual(len(self.gh.prs), 1)
        self.assertEqual(self.gh.writes, ['codex/release-4.9.0'])

    def test_changed_zip_on_resume_is_rejected(self):
        self.gh.fail_upload = 'update-Happ.x64.v4.9.0(100).zip'
        with self.assertRaises(p.PublishError):
            self.run_publish(True)
        original_download = self.cloud.download
        def changed_download(platform, source, target):
            original_download(platform, source, target)
            if target.name == 'update-Happ.x64.zip':
                target.write_bytes(b'changed payload')
            elif target.name == 'update-Happ.x64.zip.sha256':
                target.write_text(hashlib.sha256(b'changed payload').hexdigest())
        self.cloud.download = changed_download
        count = len(self.gh.uploads)
        with self.assertRaisesRegex(p.PublishError, 'ZIP contents changed'):
            self.run_publish(True)
        self.assertEqual(len(self.gh.uploads), count)

    def test_main_conflict_recalculates_from_current_document(self):
        self.gh.conflicts = 1
        self.run_publish(True)
        self.assertEqual(self.gh.documents['main']['filenum'], 60)
        self.assertEqual(self.gh.writes, ['main'])

    def test_protected_main_is_not_bypassed(self):
        original_api = self.gh.api
        def protected(path, method='GET', data=None):
            if method == 'PUT':
                raise p.ApiError(403, 'Protected branch')
            return original_api(path, method, data)
        self.gh.api = protected
        with self.assertRaisesRegex(p.ApiError, 'Protected branch'):
            self.run_publish(True)
        self.assertFalse(self.gh.prs)
        self.assertEqual(self.gh.documents['main']['filenum'], 58)

    def test_sha256_formats(self):
        path = self.directory / 'sample.zip'
        path.write_bytes(b'hello')
        digest = hashlib.sha256(b'hello').hexdigest()
        for text in (digest, digest.upper() + '\n', digest + ' *sample.zip', 'SHA256 (sample.zip) = ' + digest):
            self.assertEqual(p.verify_zip(path, text), digest)
        with self.assertRaises(p.PublishError):
            p.verify_zip(path, 'bad checksum')

    def test_invalid_version(self):
        with self.assertRaisesRegex(p.PublishError, 'Version must'):
            p.publish(self.gh, self.cloud, '4.9.0;command', True, True, '', self.directory)
        self.assertFalse(self.gh.calls)

    def test_webdav_namespaces_encoded_paths_and_origin_filter(self):
        cloud = p.Nextcloud('user name', 'secret')
        base = '/remote.php/dav/files/user%20name/Desktop/Happ/Desktop-Windows/'
        entries = []
        for href in (base + 'setup-Happ.x64.v4.9.0%28100%29.exe',
                     'https://attacker.invalid' + base + 'bad.exe',
                     base + '../other.exe'):
            entries.append(f'<d:response><d:href>{href}</d:href><d:propstat>'
                           '<d:prop><d:resourcetype/></d:prop>'
                           '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        xml = '<d:multistatus xmlns:d="DAV:">' + ''.join(entries) + '</d:multistatus>'
        with patch.object(cloud, 'request', return_value=io.BytesIO(xml.encode())) as request:
            files = cloud.listing('Windows')
        self.assertEqual(set(files), {'setup-Happ.x64.v4.9.0(100).exe'})
        request.assert_called_once_with(cloud.base + 'Desktop-Windows/', 'PROPFIND')

    def test_nextcloud_listing_failure_prevents_publication(self):
        with patch.object(self.cloud, 'listing', side_effect=p.PublishError('Cannot list Nextcloud')):
            with self.assertRaisesRegex(p.PublishError, 'Cannot list Nextcloud'):
                self.run_publish(True)
        self.assertFalse(any(method != 'GET' for _, method, _ in self.gh.calls))


if __name__ == '__main__':
    unittest.main()
