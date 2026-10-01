#!/usr/bin/env python3
"""Build a MiSTer Downloader database of third-party arcade cores.

Reads sources.json, collects core (.rbf) and game (.mra) files from each source,
drops anything already provided by the official MiSTer / Jotego / Coin-Op databases,
drops games needing controls the cab lacks, sorts the rest into
_Arcade/_Beta/_Horizontal and _Arcade/_Beta/_Vertical, and writes:

  out/db.json.zip   the Downloader database
  out/REPORT.md     what was included, what was dropped and why
  cache/            hashes and MAME data, reused between runs

Standard library only. Needs git on PATH.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'out'
CACHE = ROOT / 'cache'
CLONES = CACHE / 'clones'
FILE_CACHE_PATH = CACHE / 'files.json'
MAME_LST_URL = 'https://raw.githubusercontent.com/mamedev/mame/master/src/mame/mame.lst'
MAME_DRIVER_URL = 'https://raw.githubusercontent.com/mamedev/mame/master/src/mame/'
DATE_SUFFIX = re.compile(r'_\d{8}[^/]*$')

log_lines = []


def log(msg):
    print(msg, flush=True)


def qurl(url):
    return urllib.parse.quote(url, safe=':/?&=%#@+!,;~')  # game file names contain spaces etc.


def fetch(url, retries=3):
    url = qurl(url)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'mister-arcade-feed'})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise RuntimeError(f'fetch failed {url}: HTTP {e.code}')
            last = e
        except Exception as e:  # network hiccups: retry
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'fetch failed {url}: {last}')


def load_db(url):
    data = fetch(url)
    if data[:2] == b'PK':
        z = zipfile.ZipFile(io.BytesIO(data))
        data = z.read(z.namelist()[0])
    return json.loads(data)


def db_file_url(db, path, entry):
    return entry.get('url') or (db.get('base_files_url', '') + path)


def norm_title(name):
    """'Bloxeed (US, C System, Rev A)' -> 'bloxeed'. Used to spot the same game across sources."""
    name = re.sub(r'^rm ', '', name.strip(), flags=re.I)
    name = re.sub(r'\[.*?\]|\(.*?\)', '', name)
    return re.sub(r'[^a-z0-9]', '', name.lower())


def rbf_base(filename):
    """'Arcade-KonamiGX_20260928.rbf' -> 'arcade-konamigx'."""
    stem = filename[:-4] if filename.lower().endswith('.rbf') else filename
    return DATE_SUFFIX.sub('', stem).lower()


def rbf_matches(tag, filename):
    """Mirror Main_MiSTer's lookup: file starts with '<tag>' or 'Arcade-<tag>' followed by '_' or '.'."""
    f = filename.lower()
    for prefix in (tag.lower(), 'arcade-' + tag.lower()):
        if f.startswith(prefix) and len(f) > len(prefix) and f[len(prefix)] in '_.':
            return True
    return False


# ---------------------------------------------------------------- caches

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


file_cache = {}  # key (git blob sha or url+hash) -> {"md5", "size", "mra": {...parsed}}


def get_bytes_and_meta(key, url, want_content):
    """Return (content or None, md5, size). Downloads only when not cached or content needed."""
    c = file_cache.get(key)
    if c and not want_content:
        return None, c['md5'], c['size']
    data = fetch(url)
    md5 = hashlib.md5(data).hexdigest()
    file_cache[key] = dict(file_cache.get(key, {}), md5=md5, size=len(data))
    return data, md5, len(data)


def parse_mra(key, url):
    c = file_cache.get(key, {})
    if 'mra' in c and 'error' not in c['mra']:
        return c['mra'], c['md5'], c['size']
    data, md5, size = get_bytes_and_meta(key, url, True)
    text = data.decode('utf-8', errors='replace').lstrip('﻿')
    # Some MRAs put comments before the <?xml?> declaration, which strict XML rejects: drop the declaration.
    text = re.sub(r'<\?xml[^>]*\?>', '', text)
    info = {'rbf': None, 'setname': None, 'rotation': None, 'name': None, 'zips': []}
    try:
        root = ET.fromstring(text.strip())
        info['rbf'] = (root.findtext('rbf') or '').strip() or None
        info['setname'] = (root.findtext('setname') or '').strip() or None
        info['rotation'] = (root.findtext('rotation') or '').strip() or None
        info['name'] = (root.findtext('name') or '').strip() or None
        for rom in root.iter('rom'):
            z = rom.get('zip')
            if z:
                info['zips'] += [p.strip()[:-4] if p.strip().lower().endswith('.zip') else p.strip()
                                 for p in z.split('|') if p.strip()]
    except ET.ParseError as e:
        # Last resort: pull the few tags we need with regexes.
        for tag in ('rbf', 'setname', 'rotation', 'name'):
            m = re.search(rf'<{tag}>\s*([^<]+?)\s*</{tag}>', text, re.I)
            info[tag] = m.group(1) if m else None
        info['zips'] = [z[:-4] if z.lower().endswith('.zip') else z
                        for zs in re.findall(r'zip="([^"]+)"', text) for z in zs.split('|')]
        if not info['rbf']:
            info['error'] = str(e)
    file_cache[key]['mra'] = info
    return info, md5, size


# ---------------------------------------------------------------- MAME data (rotation + parent)

class Mame:
    def __init__(self):
        self.set_to_driver = None
        self.games = load_json(CACHE / 'mame_games.json', {})  # setname -> [parent, rot]
        self.drivers_done = set(load_json(CACHE / 'mame_drivers.json', []))

    def _index(self):
        if self.set_to_driver is not None:
            return
        self.set_to_driver = {}
        driver = None
        for line in fetch(MAME_LST_URL).decode('utf-8', errors='replace').splitlines():
            line = line.strip()
            if line.startswith('@source:'):
                driver = line[len('@source:'):]
            elif line and not line.startswith(('/', '#', '@')) and driver:
                self.set_to_driver[line] = driver

    def lookup(self, setname):
        """Return (parent, rotation_degrees) or (None, None)."""
        if not setname:
            return None, None
        if setname not in self.games:
            self._index()
            drv = self.set_to_driver.get(setname)
            if drv and drv not in self.drivers_done:
                try:
                    src = fetch(MAME_DRIVER_URL + drv).decode('utf-8', errors='replace')
                    for m in re.finditer(r'\bGAME[A-Z]*\s*\(\s*[^,]+,\s*(\w+)\s*,\s*(\w+)\s*,[^\n]*?\b(ROT\d+)', src):
                        self.games[m.group(1)] = [None if m.group(2) == '0' else m.group(2), int(m.group(3)[3:])]
                except Exception as e:
                    log(f'  ! MAME driver {drv}: {e}')
                self.drivers_done.add(drv)
        g = self.games.get(setname)
        return (g[0], g[1]) if g else (None, None)

    def save(self):
        (CACHE / 'mame_games.json').write_text(json.dumps(self.games), encoding='utf-8')
        (CACHE / 'mame_drivers.json').write_text(json.dumps(sorted(self.drivers_done)), encoding='utf-8')


def orientation(mra_rotation, mame_rot):
    if mra_rotation:
        r = mra_rotation.lower()
        if 'vertical' in r or r.strip() in ('90', '270'):
            return 'Vertical'
        if 'horizontal' in r or r.strip() in ('0', '180'):
            return 'Horizontal'
    if mame_rot is not None:
        return 'Vertical' if mame_rot in (90, 270) else 'Horizontal'
    return None


# ---------------------------------------------------------------- sources

def list_github(repo):
    """Blob-less shallow clone, return (commit, [(path, blob_sha, size)])."""
    d = CLONES / repo.replace('/', '__')
    url = f'https://github.com/{repo}'
    # Never prompt for credentials: a missing repo should fail, not pop up a login.
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')

    def git(*args, **kw):
        r = subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8', env=env, **kw)
        if r.returncode:
            raise RuntimeError(f'git {args[0]} failed ({r.returncode}): {r.stderr.strip()[:200]}')
        return r.stdout

    if d.exists():
        git('-C', str(d), 'fetch', '-q', '--depth', '1', '--filter=blob:none', 'origin', 'HEAD')
        ref = 'FETCH_HEAD'
    else:
        git('clone', '-q', '--depth', '1', '--filter=blob:none', '--no-checkout', url, str(d))
        ref = 'HEAD'
    commit = git('-C', str(d), 'rev-parse', ref).strip()
    # No '-l': asking for sizes would make a blob-less clone download every file. Sizes come from the download.
    files = []
    for line in git('-C', str(d), 'ls-tree', '-r', commit).splitlines():
        meta, path = line.split('\t', 1)
        _mode, typ, sha = meta.split()
        if typ == 'blob':
            files.append((path, sha, None))
    return commit, files


def release_files(paths):
    """Pick the folder(s) that hold the release: prefer dirs named release(s)/_Arcade; skip sims/tests/docs."""
    skip = re.compile(r'(^|/)(_?dev|debug|old|archive|wip|sim|test|tests|tb|docs?|tools|scripts|work|build|output_files|\.github)(/|$)', re.I)
    cand = [p for p in paths if not skip.search(p)] or         [p for p in paths if re.search(r'(^|/)output_files/', p)]
    for pattern in (r'(^|/)releases?(/|$)', r'(^|/)_Arcade(/|$)', r'(^|/)mra(/|$)'):
        pref = [p for p in cand if re.search(pattern, p, re.I)]
        if pref:
            return pref
    return cand


def collect_github(src):
    repo = src['repo']
    commit, files = list_github(repo)
    raw = f'https://raw.githubusercontent.com/{repo}/{commit}/'
    rbfs, mras = [], []
    all_rbf = [f for f in files if f[0].lower().endswith('.rbf')]
    all_mra = [f for f in files if f[0].lower().endswith('.mra')]
    keep_rbf = set(release_files([f[0] for f in all_rbf]))
    keep_mra = set(release_files([f[0] for f in all_mra]))
    for path, sha, size in all_rbf:
        if path in keep_rbf:
            rbfs.append({'src': repo, 'path': path, 'key': sha, 'url': raw + path, 'size': size})
    for path, sha, size in all_mra:
        if path in keep_mra:
            mras.append({'src': repo, 'path': path, 'key': sha, 'url': raw + path, 'size': size})
    return rbfs, mras, commit


def collect_downloader_db(src):
    db = load_db(src['url'])
    rbfs, mras = [], []
    for path, e in db['files'].items():
        item = {'src': src['id'], 'path': path, 'key': 'md5:' + e['hash'], 'url': db_file_url(db, path, e),
                'size': e['size']}
        file_cache.setdefault(item['key'], {'md5': e['hash'], 'size': e['size']})
        if path.lower().endswith('.rbf'):
            rbfs.append(item)
        elif path.lower().endswith('.mra'):
            mras.append(item)
    return rbfs, mras, str(db.get('timestamp'))


def latest_rbfs(rbfs):
    """One file per core name: the newest date suffix wins (same rule MiSTer uses)."""
    best = {}
    for r in rbfs:
        name = r['path'].rsplit('/', 1)[-1]
        b = rbf_base(name)
        if b not in best or name > best[b]['path'].rsplit('/', 1)[-1]:
            best[b] = r
    return list(best.values())


# ---------------------------------------------------------------- official data

def official_data(cfg):
    titles, setnames, rbf_names, mra_files = {}, {}, {}, {}
    for label, url in cfg['official_dbs'].items():
        try:
            db = load_db(url)
        except Exception as e:
            log(f'! could not load official db {label}: {e}')
            continue
        for path in db['files']:
            name = path.rsplit('/', 1)[-1]
            if name.lower().endswith('.mra'):
                titles.setdefault(norm_title(name[:-4]), label)
                mra_files.setdefault(name.lower(), label)
            elif name.lower().endswith('.rbf') and path.lower().startswith('_arcade/cores/'):
                rbf_names.setdefault(rbf_base(name), label)
    # MAD DB: the arcade database Update All's organizer uses, keyed by setname.
    try:
        ua = json.loads(fetch(cfg['update_all_db']))
        mad_url = next(v['url'] for p, v in ua['files'].items() if p.endswith('mad_db.json.zip'))
        mad = load_db(mad_url)
        # MAD DB also covers community cores, so only trust a setname whose game file is in an official feed.
        for s, v in mad.items():
            f = (v.get('file') or '').lower()
            if s and f in mra_files:
                setnames.setdefault(s, f'{mra_files[f]} ({v.get("file")})')
    except Exception as e:
        log(f'! MAD DB unavailable: {e}')
    return titles, setnames, rbf_names


# ---------------------------------------------------------------- main

def main():
    cfg = json.loads((ROOT / 'sources.json').read_text(encoding='utf-8'))
    OUT.mkdir(exist_ok=True)
    CLONES.mkdir(parents=True, exist_ok=True)
    file_cache.update(load_json(FILE_CACHE_PATH, {}))
    mame = Mame()
    beta = cfg['beta_dir'].strip('/')
    excludes = {k: v for k, v in cfg['exclude_titles'].items() if not k.startswith('_')}

    log('Loading official databases...')
    off_titles, off_setnames, off_rbfs = official_data(cfg)
    log(f'  {len(off_titles)} official titles, {len(off_setnames)} setnames, {len(off_rbfs)} arcade cores')

    files, folders = {}, {}
    report = {'included': [], 'dup': [], 'controls': [], 'norbf': [], 'conflict': [], 'errors': [],
              'sources': [], 'unsorted': []}
    seen_titles = {}   # norm title -> source, to stop two third-party sources duplicating each other
    used_rbf_bases = {}

    for src in cfg['sources']:
        sid = src.get('repo') or src.get('id')
        log(f'Source {sid}')
        try:
            rbfs, mras, version = (collect_github if src['type'] == 'github' else collect_downloader_db)(src)
        except Exception as e:
            report['errors'].append(f'{sid}: {e}')
            log(f'  ! {e}')
            continue
        rbfs = latest_rbfs(rbfs)
        src_included = 0

        # Read all game files in parallel up front (cached ones are skipped).
        def warm(m):
            try:
                parse_mra(m['key'], m['url'])
            except Exception:
                pass  # reported later when the file is used
        with ThreadPoolExecutor(16) as pool:
            list(pool.map(warm, [m for m in mras if 'mra' not in file_cache.get(m['key'], {})]))

        # Group MRAs: parents and their _alternatives, keyed by the parent title.
        groups = {}
        for m in mras:
            parts = m['path'].split('/')
            alt_idx = next((i for i, p in enumerate(parts) if p.lower() == '_alternatives'), None)
            if alt_idx is not None and alt_idx + 1 < len(parts) - 1:
                gname = parts[alt_idx + 1].lstrip('_')
                m['alt_folder'] = parts[alt_idx + 1]
            else:
                gname = parts[-1][:-4]
                m['alt_folder'] = None
            groups.setdefault(norm_title(gname), []).append(m)

        needed_rbfs = {}
        for gkey, members in sorted(groups.items()):
            parsed = []
            for m in members:
                try:
                    info, md5, size = parse_mra(m['key'], m['url'])
                except Exception as e:
                    report['errors'].append(f'{sid}: {m["path"]}: {e}')
                    continue
                parsed.append((m, info, md5, size))
            if not parsed:
                continue
            title = parsed[0][0]['path'].rsplit('/', 1)[-1][:-4]
            label = f'{title} [{sid}]'

            # 1. Wrong controls for a JAMMA cab
            hit = next((k for k in excludes if k.lower() in ' '.join(
                [m['path'] for m, *_ in parsed] + [i.get('name') or '' for _, i, *_ in parsed]).lower()), None)
            if hit:
                report['controls'].append(f'{label}: {excludes[hit]}')
                continue

            # 2. Duplicate of an official game (by title, setname, or MAME parent setname)
            dup = None
            for m, info, *_ in parsed:
                t = norm_title(m['path'].rsplit('/', 1)[-1][:-4])
                if t in off_titles:
                    dup = f'title matches {off_titles[t]}'
                    break
                for s in filter(None, [info.get('setname')] + info.get('zips', [])[:1]):
                    parent, _ = mame.lookup(s)
                    for cand in filter(None, [s, parent]):
                        if cand in off_setnames:
                            dup = f'setname {cand} is in {off_setnames[cand]}'
                            break
                    if dup:
                        break
                if dup:
                    break
            if not dup and gkey in seen_titles:
                dup = f'already provided by {seen_titles[gkey]}'
            if dup:
                report['dup'].append(f'{label}: {dup}')
                continue

            # 3. The core file each game needs must be in this source and must not clash with an official core
            ok_members = []
            for m, info, md5, size in parsed:
                tag = info.get('rbf')
                match = [r for r in rbfs if tag and rbf_matches(tag, r['path'].rsplit('/', 1)[-1])]
                if not match and tag:
                    # Developer named the file differently from what the MRA asks for (e.g. MRA wants
                    # 'Arcade-SegaVCO', file is 'SegaVCO_20260801.rbf'): install it under the name the MRA expects.
                    bare = lambda x: re.sub(r'^arcade-', '', x.lower())
                    loose = [r for r in rbfs if bare(rbf_base(r['path'].rsplit('/', 1)[-1])) == bare(tag)]
                    if loose:
                        r0 = max(loose, key=lambda r: r['path'].rsplit('/', 1)[-1])
                        fname = r0['path'].rsplit('/', 1)[-1]
                        suffix = DATE_SUFFIX.search(fname[:-4])
                        match = [dict(r0, dest_name=tag + (suffix.group(0) if suffix else '') + '.rbf')]
                if not match:
                    report['norbf'].append(f'{m["path"]} [{sid}]: needs core "{tag}", not found in this source')
                    continue
                r = max(match, key=lambda r: r['path'].rsplit('/', 1)[-1])
                b = rbf_base(r.get('dest_name') or r['path'].rsplit('/', 1)[-1])
                clash = [ob for ob in off_rbfs if rbf_matches(tag, ob + '_x') or ob == b]
                if clash:
                    report['conflict'].append(f'{m["path"]} [{sid}]: core "{tag}" clashes with official core {clash[0]}')
                    continue
                if b in used_rbf_bases and used_rbf_bases[b] != sid:
                    report['conflict'].append(f'{m["path"]} [{sid}]: core "{b}" also shipped by {used_rbf_bases[b]}')
                    continue
                ok_members.append((m, info, md5, size, r))
            if not ok_members:
                continue

            # 4. Orientation from the MRA, else from MAME's source
            orient = None
            for m, info, *_ in ok_members:
                s = info.get('setname') or (info.get('zips') or [None])[0]
                _, rot = mame.lookup(s)
                orient = orientation(info.get('rotation'), rot)
                if orient:
                    break
            folder = f'_{orient}' if orient else '_Unsorted'
            if not orient:
                report['unsorted'].append(label)

            for m, info, md5, size, r in ok_members:
                fname = m['path'].rsplit('/', 1)[-1]
                if m['alt_folder']:
                    dest = f'{beta}/{folder}/_alternatives/{m["alt_folder"]}/{fname}'
                else:
                    dest = f'{beta}/{folder}/{fname}'
                files[dest] = {'hash': md5, 'size': size, 'url': qurl(m['url'])}
                needed_rbfs[rbf_base(r.get('dest_name') or r['path'].rsplit('/', 1)[-1])] = r
            seen_titles[gkey] = sid
            src_included += 1
            report['included'].append((orient or 'Unsorted', title, sid, len(ok_members)))

        for b, r in needed_rbfs.items():
            _, md5, size = get_bytes_and_meta(r['key'], r['url'], False)
            dest = f'_Arcade/cores/{r.get("dest_name") or r["path"].rsplit("/", 1)[-1]}'
            files[dest] = {'hash': md5, 'size': size, 'url': qurl(r['url']), 'tangle': [b + '_core']}
            used_rbf_bases[b] = sid
        report['sources'].append((sid, version, src_included, len(needed_rbfs)))
        FILE_CACHE_PATH.write_text(json.dumps(file_cache), encoding='utf-8')

    for path in files:
        parts = path.split('/')
        for i in range(1, len(parts)):
            folders['/'.join(parts[:i])] = {}

    db = {'v': 1, 'db_id': cfg['db_id'], 'timestamp': int(time.time()), 'files': files, 'folders': folders}
    with zipfile.ZipFile(OUT / 'db.json.zip', 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('db.json', json.dumps(db, indent=None, sort_keys=True))
    FILE_CACHE_PATH.write_text(json.dumps(file_cache), encoding='utf-8')
    mame.save()
    write_report(report, files)
    log(f'Done: {sum(1 for p in files if p.endswith(".mra"))} game files, '
        f'{sum(1 for p in files if p.endswith(".rbf"))} cores.')
    return 0 if not report['errors'] else 0


def write_report(r, files):
    L = ['# MiSTer arcade feed: build report', '', time.strftime('Built %Y-%m-%d %H:%M UTC', time.gmtime()), '']
    inc = sorted(r['included'])
    L += [f'**{len(inc)} games** ({sum(n for *_, n in inc)} files incl. regional versions), '
          f'**{sum(1 for p in files if p.endswith(".rbf"))} cores**.', '']
    for o in ('Horizontal', 'Vertical', 'Unsorted'):
        rows = [x for x in inc if x[0] == o]
        if rows:
            L += [f'## {o} ({len(rows)})', '', '| Game | Source | Versions |', '|---|---|---|']
            L += [f'| {t} | {s} | {n} |' for _, t, s, n in rows]
            L.append('')
    sections = [('Dropped: duplicate of an official game', 'dup'),
                ('Dropped: controls a JAMMA cab lacks', 'controls'),
                ('Dropped: core file clash', 'conflict'),
                ('Dropped: core file not found', 'norbf'),
                ('Errors', 'errors')]
    for head, key in sections:
        if r[key]:
            L += [f'## {head} ({len(r[key])})', ''] + [f'- {x}' for x in sorted(r[key])] + ['']
    L += ['## Sources', '', '| Source | Version | Games | Cores |', '|---|---|---|---|']
    L += [f'| {s} | {v[:12]} | {g} | {c} |' for s, v, g, c in r['sources']]
    (OUT / 'REPORT.md').write_text('\n'.join(L) + '\n', encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main())
