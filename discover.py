#!/usr/bin/env python3
"""Look for new third-party MiSTer arcade core repos on GitHub that aren't in sources.json yet.

Writes out/CANDIDATES.md. Nothing is added automatically: to accept a repo, add it to
"sources" in sources.json; to dismiss it, add it to "ignored_repos".
Needs GITHUB_TOKEN (the Actions token is enough).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OFFICIAL_OWNERS = {'mister-devel', 'jotego', 'coin-opcollection', 'mister-unstable-nightlies', 'mister-db9',
                   'mister-llapi', 'theypsilon'}
QUERIES = ['arcade mister in:name', 'arcade in:name mister in:readme', '_MiSTer in:name arcade in:description']
LOOKBACK_DAYS = 45


def api(url):
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                               'User-Agent': 'mister-arcade-feed',
                                               'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN']})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    cfg = json.loads((ROOT / 'sources.json').read_text(encoding='utf-8'))
    known = {s['repo'].lower() for s in cfg['sources'] if s.get('repo')}
    known |= {r.lower() for r in cfg.get('ignored_repos', [])}
    since = time.strftime('%Y-%m-%d', time.gmtime(time.time() - LOOKBACK_DAYS * 86400))
    found = {}
    for q in QUERIES:
        url = 'https://api.github.com/search/repositories?per_page=100&sort=updated&q=' + \
              urllib.parse.quote(f'{q} pushed:>{since} fork:false')
        try:
            for r in api(url).get('items', []):
                found[r['full_name']] = r
        except Exception as e:
            print('search failed:', q, e)
        time.sleep(3)

    rows = []
    for name, r in sorted(found.items()):
        if name.lower() in known or r['owner']['login'].lower() in OFFICIAL_OWNERS:
            continue
        try:
            tree = api(f'https://api.github.com/repos/{name}/git/trees/{r["default_branch"]}?recursive=1')
            paths = [t['path'] for t in tree.get('tree', [])]
        except Exception:
            continue
        n_rbf = sum(p.lower().endswith('.rbf') for p in paths)
        n_mra = sum(p.lower().endswith('.mra') for p in paths)
        if n_rbf and n_mra:  # only repos that actually ship a playable build
            rows.append(f'| [{name}]({r["html_url"]}) | {n_mra} | {r["created_at"][:10]} | {r["pushed_at"][:10]} '
                        f'| {(r.get("description") or "").replace("|", "/")[:90]} |')
        time.sleep(0.5)

    out = ROOT / 'out'
    out.mkdir(exist_ok=True)
    text = ['# New arcade core repos to review', '',
            'Add a repo to `sources` in sources.json to include it, or to `ignored_repos` to hide it.', '']
    if rows:
        text += ['| Repo | Game files | Created | Last push | Description |', '|---|---|---|---|---|'] + rows
    else:
        text.append('Nothing new.')
    (out / 'CANDIDATES.md').write_text('\n'.join(text) + '\n', encoding='utf-8')
    print(f'{len(rows)} candidate repos')


if __name__ == '__main__':
    main()
