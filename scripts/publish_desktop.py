"""Publish Nextcloud builds. Requires Python 3.12 and GitHub CLI; no pip packages."""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import xml.etree.ElementTree as ET


PLATFORMS = {
    'Windows': ['setup-Happ.arm64.exe', 'setup-Happ.x64.exe'],
    'Linux': [f'Happ.linux.{arch}.{ext}' for arch in ('arm64', 'x64')
              for ext in ('deb', 'pkg.tar.zst', 'rpm')],
    'macOS': ['Happ.macOS.universal.dmg'],
}
ZIPS = [f'update-Happ.{arch}.zip' for arch in ('arm64', 'x64')]
MARKER = re.compile(r'<!-- happ-desktop-publisher: (.*?) -->', re.DOTALL)


class PublishError(RuntimeError):
    pass


class ApiError(PublishError):
    def __init__(self, status, message):
        super().__init__(f'GitHub HTTP {status}: {message}')
        self.status = status


def log(message):
    print(message, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write(message + '\n\n')


def source_name(name, version, build):
    # Insert version before the entire extension, including pkg.tar.zst.
    stem = name.split('.exe')[0] if name.startswith('setup-') else (
        name.split('.zip')[0] if name.startswith('update-') else
        '.'.join(name.split('.')[:3]))
    return f'{stem}.v{version}({build}){name[len(stem):]}'


def choose_builds(listings, version):
    builds = {}
    for platform, names in PLATFORMS.items():
        numbers = []
        for name in names:
            pattern = re.escape(source_name(name, version, 'BUILD')).replace('BUILD', r'(\d+)')
            for filename in listings[platform]:
                match = re.fullmatch(pattern, filename)
                if match:
                    numbers.append(int(match[1]))
        if not numbers:
            raise PublishError(f'No {platform} builds for {version}')
        builds[platform] = max(numbers)
    return builds


def required_files(listings, version, builds, stable, installers=True):
    result = []
    for platform, names in PLATFORMS.items():
        required = list(names) if installers else []
        if stable and platform == 'Windows':
            for zip_name in ZIPS:
                zip_source = source_name(zip_name, version, builds[platform])
                checksum_name = zip_name + '.sha256'
                checksum_source = source_name(checksum_name, version, builds[platform])
                if zip_source in listings[platform] and checksum_source in listings[platform]:
                    required.extend((zip_name, checksum_name))
                else:
                    missing = [name for name in (zip_source, checksum_source)
                               if name not in listings[platform]]
                    log(f'Skipping silent update {zip_name}: missing {", ".join(missing)}. '
                        'Existing link_silent and hash_silent will be preserved.')
        for name in required:
            source = source_name(name, version, builds[platform])
            if source not in listings[platform]:
                raise PublishError(f'Incomplete {platform} build {builds[platform]}: missing {source}')
            result.append((platform, source, name))
    return result


def verify_zip(path, checksum):
    # Accept a bare digest, sha256sum output, or BSD shasum output.
    value = checksum.strip()
    match = re.fullmatch(r'([0-9a-fA-F]{64})(?:\s+\*?[^\r\n]+)?', value)
    if not match:
        match = re.fullmatch(r'SHA256\s*\([^\r\n]+\)\s*=\s*([0-9a-fA-F]{64})', value)
    if not match:
        raise PublishError(f'Invalid SHA256 file for {path.name}')
    expected = match[1].lower()
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected:
        raise PublishError(f'SHA256 mismatch for {path.name}')
    return actual


def updated_document(original, repo, version, hashes, silent_links):
    document = copy.deepcopy(original)
    changed = False
    for channel in ('beta', 'stable', 'beta_arm', 'stable_arm'):
        arch = 'arm64' if channel.endswith('_arm') else 'x64'
        block = document['windows'][channel]
        values = {
            'version': version,
            'link': f'https://github.com/{repo}/releases/download/{version}/setup-Happ.{arch}.exe',
            **({'hash_silent': hashes[arch], 'link_silent': silent_links[arch]}
               if arch in hashes else {}),
        }
        changed |= any(block.get(key) != value for key, value in values.items())
        block.update(values)
    if changed:
        document['filenum'] += 1
    return document, changed


def metadata(body, version):
    matches = MARKER.findall(body or '')
    if len(matches) != 1:
        raise PublishError('Existing release has no unique publisher metadata; refusing to guess its builds')
    try:
        data = json.loads(matches[0])
        if (data['version'] != version or data['schema'] != 1
                or set(data['builds']) != set(PLATFORMS)
                or any(type(n) is not int or n < 0 for n in data['builds'].values())):
            raise ValueError('Invalid metadata')
        return data
    except (ValueError, KeyError, TypeError) as error:
        raise PublishError('Invalid publisher metadata in release description') from error


def body_with_metadata(body, data):
    clean = MARKER.sub('', body or '').rstrip()
    return clean + '\n\n<!-- happ-desktop-publisher: ' + json.dumps(data, sort_keys=True) + ' -->'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Nextcloud credentials to redirected destinations.
        return None


class Nextcloud:
    def __init__(self, username, password):
        if not username or not password:
            raise PublishError('Set NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD secrets')
        self.base = ('https://nextcloud.2kk.uk/remote.php/dav/files/'
                     + quote(username, safe='') + '/Desktop/Happ/')
        self.authorization = 'Basic ' + base64.b64encode(
            f'{username}:{password}'.encode()).decode()
        self.opener = build_opener(NoRedirect())
        self.urls = {}

    def request(self, url, method='GET'):
        headers = {'Authorization': self.authorization}
        data = None
        if method == 'PROPFIND':
            headers.update({'Depth': '1', 'Content-Type': 'application/xml'})
            data = b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
        return self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=120)

    def listing(self, platform):
        url = self.base + f'Desktop-{platform}/'
        try:
            with self.request(url, 'PROPFIND') as response:
                root = ET.fromstring(response.read())
        except (HTTPError, URLError, ET.ParseError) as error:
            raise PublishError(f'Cannot list Nextcloud Desktop-{platform}: {type(error).__name__}') from error
        files = {}
        for item in root.findall('{DAV:}response'):
            href = item.findtext('{DAV:}href')
            if not href or item.find('.//{DAV:}collection') is not None:
                continue
            statuses = [p.findtext('{DAV:}status', '') for p in item.findall('{DAV:}propstat')]
            if not any(' 200 ' in status for status in statuses):
                continue
            absolute = urljoin(url, href)
            parsed = urlsplit(absolute)
            expected = urlsplit(url)
            decoded = unquote(parsed.path)
            parent, _, name = decoded.rpartition('/')
            if (parsed.scheme != expected.scheme or parsed.netloc != expected.netloc
                    or parent + '/' != unquote(expected.path) or parsed.query or parsed.fragment):
                continue
            files[name] = absolute
        self.urls[platform] = files
        return files

    def download(self, platform, source, target):
        for attempt in range(3):
            try:
                with self.request(self.urls[platform][source]) as response, target.open('wb') as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                if not target.stat().st_size:
                    raise PublishError(f'Empty Nextcloud file: {source}')
                return
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                if attempt == 2:
                    raise PublishError(f'Cannot download {source}: {type(error).__name__}') from error
                time.sleep(2 ** attempt)


class GitHub:
    def __init__(self, repo, token):
        self.repo = repo
        self.token = token

    def api(self, path, method='GET', data=None):
        request = Request('https://api.github.com/repos/' + self.repo + '/' + path,
                          data=json.dumps(data).encode() if data is not None else None,
                          headers={'Authorization': f'Bearer {self.token}',
                                   'Accept': 'application/vnd.github+json',
                                   'X-GitHub-Api-Version': '2022-11-28',
                                   'Content-Type': 'application/json'}, method=method)
        try:
            with build_opener(NoRedirect()).open(request, timeout=120) as response:
                content = response.read()
                return json.loads(content) if content else None
        except HTTPError as error:
            try:
                message = json.loads(error.read()).get('message', 'Request failed')
            except ValueError:
                message = 'Request failed'
            raise ApiError(error.code, message) from error

    def optional(self, path):
        try:
            return self.api(path)
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def pages(self, path):
        result = []
        for page in range(1, 10000):
            batch = self.api(f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise PublishError('Too many GitHub API pages')

    def release(self, tag):
        return self.optional('releases/tags/' + quote(tag, safe=''))

    def assets(self, release):
        return self.pages(f'releases/{release["id"]}/assets')

    def upload(self, tag, path, replace=False):
        command = ['gh', 'release', 'upload', tag, str(path), '--repo', self.repo]
        if replace:
            command.append('--clobber')
        subprocess.run(command, check=True)

    def verify_existing_asset(self, tag, asset, expected_hash):
        if asset.get('state') != 'uploaded':
            raise PublishError(f'Existing asset is incomplete: {asset["name"]}; remove it manually before retrying')
        digest = asset.get('digest')
        if not digest or not digest.startswith('sha256:'):
            # Older GitHub assets may not expose a digest. Download without changing them.
            with tempfile.TemporaryDirectory(prefix='happ-existing-') as temporary:
                subprocess.run(['gh', 'release', 'download', tag, '--repo', self.repo,
                                '--pattern', asset['name'], '--dir', temporary], check=True)
                with (Path(temporary) / asset['name']).open('rb') as stream:
                    digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest.lower() != 'sha256:' + expected_hash:
            raise PublishError(f'Existing asset has different contents: {asset["name"]}; not replacing it')

    def read_document(self, branch):
        entry = self.api('contents/release?ref=' + quote(branch, safe=''))
        return json.loads(base64.b64decode(entry['content'])), entry['sha']

    def write_document(self, branch, version, hashes, silent_links):
        for attempt in range(3):
            original, sha = self.read_document(branch)
            document, changed = updated_document(original, self.repo, version, hashes, silent_links)
            if not changed:
                return False
            content = (json.dumps(document, indent=2, ensure_ascii=False) + '\n').encode()
            try:
                self.api('contents/release', 'PUT', {
                    'message': f'release: update desktop to {version}', 'branch': branch,
                    'sha': sha, 'content': base64.b64encode(content).decode(),
                })
                return True
            except ApiError as error:
                if error.status != 409 or attempt == 2:
                    raise
        return False


def publish(github, cloud, version, stable, auto, description, directory):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise PublishError('Version must contain three numbers, e.g. 4.9.0 (no v prefix)')
    if 'happ-desktop-publisher:' in description:
        raise PublishError('Description contains a reserved publisher marker')
    branch = 'codex/release-' + version
    existing = github.release(version)
    data = metadata(existing.get('body'), version) if existing else None
    open_pr = None
    if stable:
        for pr in github.pages('pulls?state=open&base=main'):
            if pr['head']['ref'].startswith('codex/release-'):
                if pr['head']['ref'] != branch or auto:
                    raise PublishError(f'First resolve the existing release PR: {pr["html_url"]}')
                open_pr = pr
        # Fail before publishing if the target branch/document is absent or invalid.
        original, _ = github.read_document('main')
        updated_document(original, github.repo, version, {'x64': '', 'arm64': ''},
                         {'x64': '', 'arm64': ''})
    if data and data.get('completed'):
        log(f'Release {version} has already completed; no changes made.')
        return
    if existing and not existing['draft'] and not stable:
        log(f'Release {version} already exists; its status and files are unchanged.')
        return
    if existing and existing.get('immutable') and existing['prerelease'] and stable:
        raise PublishError('Cannot promote an immutable prerelease')

    listings = {platform: cloud.listing(platform) for platform in PLATFORMS}
    if data is None:
        data = {'schema': 1, 'version': version, 'builds': choose_builds(listings, version)}
    published = bool(existing and not existing['draft'])
    files = required_files(listings, version, data['builds'], stable, installers=not published)
    silent = None
    if stable and any(name in ZIPS for _, _, name in files):
        silent = github.release('silent_update')
        if not silent or silent.get('draft') or silent.get('immutable'):
            raise PublishError('silent_update must exist, be published and allow adding assets')
    expected_assets = {name for names in PLATFORMS.values() for name in names}
    if published:
        actual_assets = {asset['name'] for asset in github.assets(existing)}
        if not expected_assets <= actual_assets:
            raise PublishError('Published release is missing installers; refusing to replace published files')
    log(f'Selected {version} builds: ' + json.dumps(data['builds'], sort_keys=True))
    hashes = {}
    for platform, source, name in files:
        log(f'Downloading {source}')
        cloud.download(platform, source, directory / name)
    if stable:
        for arch in ('x64', 'arm64'):
            if not any(name == f'update-Happ.{arch}.zip' for _, _, name in files):
                continue
            path = directory / f'update-Happ.{arch}.zip'
            hashes[arch] = verify_zip(path, Path(str(path) + '.sha256').read_text(encoding='utf-8-sig'))
        previous_hashes = data.get('hashes', {})
        if any(arch in previous_hashes and previous_hashes[arch] != value
               for arch, value in hashes.items()):
            raise PublishError('ZIP contents changed since the previous attempt for this build')
        data['hashes'] = {**previous_hashes, **hashes}
        silent_names = {arch: source_name(f'update-Happ.{arch}.zip', version, data['builds']['Windows'])
                        for arch in hashes}
        silent_links = {arch: f'https://github.com/{github.repo}/releases/download/silent_update/'
                       + quote(name, safe='') for arch, name in silent_names.items()}
        existing_silent = {asset['name']: asset for asset in github.assets(silent)} if silent else {}
        for arch, name in silent_names.items():
            if name in existing_silent:
                github.verify_existing_asset('silent_update', existing_silent[name], hashes[arch])
    log('All required downloads and checks succeeded.')

    if not existing:
        existing = github.api('releases', 'POST', {
            'tag_name': version, 'target_commitish': 'main', 'name': version,
            'body': body_with_metadata(description or 'comming soon', data),
            'draft': True, 'prerelease': not stable,
        })
    else:
        # Keep manually edited release notes when resuming or promoting.
        existing = github.api(f'releases/{existing["id"]}', 'PATCH', {
            'body': body_with_metadata(existing.get('body'), data),
        })
    if existing['draft']:
        for name in sorted(expected_assets):
            github.upload(version, directory / name, replace=True)
    existing = github.api(f'releases/{existing["id"]}', 'PATCH', {
        'draft': False, 'prerelease': not stable, 'make_latest': 'true' if stable else 'false',
    })
    log(f'Published {"Latest Release" if stable else "PreRelease"}: {existing["html_url"]}')
    if not stable:
        return

    for arch, name in silent_names.items():
        if name in existing_silent:
            log(f'Already uploaded and verified: silent_update/{name}')
            continue
        path = directory / name
        (directory / f'update-Happ.{arch}.zip').replace(path)
        github.upload('silent_update', path)
        log(f'Added silent_update/{name}')
    if auto:
        github.write_document('main', version, hashes, silent_links)
        log('release committed to main (or already up to date).')
    else:
        ref = github.optional('git/ref/heads/' + branch)
        if not ref:
            head = github.api('git/ref/heads/main')
            github.api('git/refs', 'POST', {'ref': 'refs/heads/' + branch, 'sha': head['object']['sha']})
        github.write_document(branch, version, hashes, silent_links)
        if not open_pr:
            open_pr = github.api('pulls', 'POST', {
                'title': f'release: update desktop to {version}', 'head': branch, 'base': 'main',
                'body': f'Update Windows release metadata to {version}.\n\n'
                        f'Installers: {existing["html_url"]}\n\n'
                        'Available silent_update ZIP files were SHA256-verified and added. '
                        'Silent-update fields change only for architectures with ZIP files. '
                        'This PR changes only release; filenum increases once.',
            })
        log(f'Release metadata PR: {open_pr["html_url"]}')
    data['completed'] = True
    data['mode'] = 'auto' if auto else 'pr'
    github.api(f'releases/{existing["id"]}', 'PATCH', {
        'body': body_with_metadata(existing.get('body'), data),
    })
    log('All publication stages completed.')


def main():
    try:
        github = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'])
        cloud = Nextcloud(os.environ.get('NEXTCLOUD_USERNAME'), os.environ.get('NEXTCLOUD_PASSWORD'))
        with tempfile.TemporaryDirectory(prefix='happ-release-') as temporary:
            publish(github, cloud, os.environ.get('INPUT_VERSION', '').strip(),
                    os.environ.get('INPUT_RELEASE', 'false') == 'true',
                    os.environ.get('INPUT_AUTO_RELEASE', 'true') == 'true',
                    os.environ.get('INPUT_DESCRIPTION', 'comming soon'), Path(temporary))
    except (PublishError, KeyError, ValueError, URLError, OSError, subprocess.CalledProcessError) as error:
        log(f'Publication failed: {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
