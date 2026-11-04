"""Finds, filters and checks extension repos for AnimeBay's catalog.

Runs daily on GitHub Actions. It never changes the catalog directly: it
writes the proposed catalog.json and a report, and the workflow opens a pull
request for the owner to approve. Extensions are code that runs on every
user's phone, so a person approves every new repo.

What it does:
  1. Checks the repos already in the catalog: the index still loads and its
     usable sources' sites still answer. A repo failing 7 runs in a row is
     proposed for removal.
  2. Searches GitHub for Aniyomi, CloudStream and Mangayomi repos.
  3. Keeps only usable sources: English, not adult, anime, not blocked.
  4. Checks each candidate: index valid, repo updated in the last 6 months,
     enough usable sources whose sites answer, and new sources (not already
     in the catalog).
  5. Proposes the best few per engine, with a report.

The phones then test every source for real (search, episodes, video links)
and rank them, as they already do.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, 'catalog.json')
STATE = os.path.join(ROOT, 'tools', 'state.json')
REPORT = os.path.join(ROOT, 'tools', 'report.md')

TOKEN = os.environ.get('GITHUB_TOKEN', '')
UA = 'AnimeBay-catalog-discovery'

MAX_NEW_PER_BACKEND = 3
MIN_ALIVE_SOURCES = 2
MAX_AGE_DAYS = 180
REMOVE_AFTER_FAILED_RUNS = 7

ADULT_WORDS = re.compile(
    r'hentai|porn|xxx|nsfw|18\+|adult|erotic|jav\b|sex|ecchi|r18|haho|hanime',
    re.I,
)
ANIME_TYPES_CS = {'Anime', 'AnimeMovie', 'OVA'}


def http_get(url, accept_json=True, timeout=20):
    headers = {'User-Agent': UA}
    if 'api.github.com' in url and TOKEN:
        headers['Authorization'] = f'Bearer {TOKEN}'
        headers['Accept'] = 'application/vnd.github+json'
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if accept_json else body


def site_alive(url):
    """A source's website answers (anything below a server error)."""
    if not url or not url.startswith('http'):
        return False
    for method in ('HEAD', 'GET'):
        try:
            req = urllib.request.Request(
                url, method=method,
                headers={'User-Agent': 'Mozilla/5.0 AnimeBay-check'},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status < 500
        except urllib.error.HTTPError as e:
            # 403/405 often just mean "no bots" or "no HEAD": the site is up.
            if e.code < 500 and e.code not in (404, 410):
                return True
        except Exception:
            pass
    return False


def key(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


# --- Reading each engine's index into usable sources -----------------------

def usable_sources(backend, index, blocked):
    """(name, baseUrl) of English, non-adult anime sources in an index."""
    out = []
    if backend == 'aniyomi':
        for ext in index:
            if ext.get('nsfw') == 1:
                continue
            if 'animeextension' not in ext.get('pkg', ''):
                continue
            for s in ext.get('sources', []):
                if s.get('lang') == 'en':
                    out.append((s.get('name'), s.get('baseUrl')))
    elif backend == 'cloudstream':
        for p in index:
            types = set(p.get('tvTypes') or [])
            if 'NSFW' in types or not types & ANIME_TYPES_CS:
                continue
            if (p.get('language') or '') != 'en':
                continue
            if p.get('status', 1) == 0:
                continue
            out.append((p.get('name'), p.get('url')))
    elif backend == 'mangayomi':
        for s in index:
            if s.get('isNsfw') or s.get('lang') != 'en':
                continue
            if s.get('itemType') != 1:  # 1 = anime
                continue
            out.append((s.get('name'), s.get('baseUrl')))
    return [
        (n, u) for n, u in out
        if n and key(n) not in blocked and not ADULT_WORDS.search(n)
        and not ADULT_WORDS.search(u or '')
    ]


def looks_like(backend, index):
    if not isinstance(index, list) or not index:
        return False
    first = index[0]
    return {
        'aniyomi': 'pkg' in first and 'sources' in first,
        'cloudstream': 'internalName' in first or 'tvTypes' in first,
        'mangayomi': 'itemType' in first and 'sourceCodeUrl' in first,
    }[backend]


def check_index(backend, url, blocked):
    """Loads an index and counts usable sources whose sites answer."""
    try:
        index = http_get(url)
    except Exception as e:
        return {'ok': False, 'error': f'index did not load ({e.__class__.__name__})'}
    if not looks_like(backend, index):
        return {'ok': False, 'error': 'not a valid index'}
    usable = usable_sources(backend, index, blocked)
    if backend == 'cloudstream':
        # Plugin files, not sites: check the plugin downloads.
        alive_flags = [site_alive(u) for _, u in usable[:20]]
    else:
        with ThreadPoolExecutor(8) as pool:
            alive_flags = list(pool.map(lambda s: site_alive(s[1]), usable[:20]))
    alive = [s for s, ok in zip(usable[:20], alive_flags) if ok]
    return {
        'ok': len(alive) > 0,
        'usable': len(usable),
        'alive': len(alive),
        'names': sorted({key(n) for n, _ in alive}),
        'display': {key(n): n for n, _ in alive},
    }


# --- Finding candidates on GitHub ------------------------------------------

SEARCHES = {
    'aniyomi': [
        'aniyomi extensions repo in:name,description,topics',
        'topic:aniyomi-extensions',
        'topic:aniyomi',
    ],
    'cloudstream': [
        'topic:cloudstream-extensions',
        'cloudstream extensions anime in:name,description,topics',
        'topic:cloudstream',
    ],
    'mangayomi': [
        'mangayomi extensions in:name,description,topics',
        'topic:mangayomi',
    ],
}

# Where each engine's index usually lives in a repo.
INDEX_PATHS = {
    'aniyomi': [('repo', 'index.min.json'), ('main', 'index.min.json'),
                ('master', 'index.min.json')],
    'cloudstream': [('builds', 'plugins.json'), ('builds', 'repo.json'),
                    ('main', 'plugins.json')],
    'mangayomi': [('main', 'anime_index.json'), ('master', 'anime_index.json')],
}


def search_repos(query):
    url = ('https://api.github.com/search/repositories?per_page=50&sort=updated&q='
           + urllib.parse.quote(query))
    try:
        return http_get(url).get('items', [])
    except Exception as e:
        print(f'search failed: {query}: {e}', file=sys.stderr)
        return []


def candidates(backend):
    seen = {}
    for q in SEARCHES[backend]:
        for repo in search_repos(q):
            if repo.get('archived') or repo.get('disabled'):
                continue
            seen[repo['full_name']] = repo
        time.sleep(2)  # search API rate limit
    return list(seen.values())


def find_index(backend, repo):
    for branch, path in INDEX_PATHS[backend]:
        url = f'https://raw.githubusercontent.com/{repo["full_name"]}/{branch}/{path}'
        try:
            index = http_get(url, timeout=15)
            if looks_like(backend, index):
                return url
        except Exception:
            continue
    return None


def age_days(iso):
    t = datetime.strptime(iso, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).days


def norm(url):
    return url.lower().replace('/refs/heads/', '/').rstrip('/')


# --- Main --------------------------------------------------------------------

def main():
    catalog = json.load(open(CATALOG, encoding='utf-8'))
    state = json.load(open(STATE, encoding='utf-8')) if os.path.exists(STATE) else {}
    failures = state.setdefault('failures', {})
    blocked = {key(n) for n in catalog.get('blockedSources', [])}
    report = ['# Catalog discovery report', '',
              f'Run: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC', '']
    changed = False

    for backend in ('aniyomi', 'cloudstream', 'mangayomi'):
        repos = catalog.get(backend, [])
        report.append(f'## {backend.capitalize()}')
        report.append('')

        # 1. Repos already in the catalog.
        known_names = set()
        keep = []
        for url in repos:
            r = check_index(backend, url, blocked)
            if r['ok']:
                failures.pop(url, None)
                known_names.update(r['names'])
                keep.append(url)
                report.append(f'- OK `{url}`: {r["alive"]} of {r["usable"]} usable sources answer')
            else:
                failures[url] = failures.get(url, 0) + 1
                n = failures[url]
                if n >= REMOVE_AFTER_FAILED_RUNS:
                    changed = True
                    report.append(f'- **Remove** `{url}`: failing {n} runs in a row ({r.get("error", "no usable source answers")})')
                    failures.pop(url, None)
                else:
                    keep.append(url)
                    report.append(f'- Failing ({n}/{REMOVE_AFTER_FAILED_RUNS}) `{url}`: {r.get("error", "no usable source answers")}')

        # 2-4. New candidates.
        found = []
        known = {norm(u) for u in repos}
        for repo in candidates(backend):
            if age_days(repo['pushed_at']) > MAX_AGE_DAYS:
                continue
            url = find_index(backend, repo)
            if not url or norm(url) in known:
                continue
            r = check_index(backend, url, blocked)
            new = [n for n in r.get('names', []) if n not in known_names]
            if r['ok'] and r['alive'] >= MIN_ALIVE_SOURCES and new:
                found.append((len(new), r['alive'], repo, url, r, new))
        found.sort(key=lambda f: (f[0], f[1]), reverse=True)

        added = 0
        for n_new, alive, repo, url, r, new in found:
            if added >= MAX_NEW_PER_BACKEND:
                break
            keep.append(url)
            known_names.update(new)
            added += 1
            changed = True
            stars = repo.get('stargazers_count', 0)
            report.append(
                f'- **Add** `{url}` ([{repo["full_name"]}]({repo["html_url"]}), '
                f'{stars} stars, updated {age_days(repo["pushed_at"])} days ago): '
                f'{alive} usable sources answer, {n_new} new: '
                + ', '.join(sorted(r['display'][k] for k in new)[:15])
            )
        if not found:
            report.append('- No new repos passed the checks.')
        report.append('')
        catalog[backend] = keep

    report += [
        '---',
        'Checks: English, not adult (repo flags + name/address words + '
        'blocked list), anime, index valid, repo updated in the last '
        f'{MAX_AGE_DAYS} days, at least {MIN_ALIVE_SOURCES} source sites '
        'answering, and sources not already in the catalog. Phones test every '
        'source fully (search, episodes, video links) before using it.',
    ]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, 'w', encoding='utf-8'), indent=2)
    open(REPORT, 'w', encoding='utf-8').write('\n'.join(report) + '\n')
    if changed:
        json.dump(catalog, open(CATALOG, 'w', encoding='utf-8'), indent=2)
        open(CATALOG, 'a', encoding='utf-8').write('\n')
    print('\n'.join(report))
    with open(os.environ.get('GITHUB_OUTPUT', os.devnull), 'a') as f:
        f.write(f'changed={str(changed).lower()}\n')


if __name__ == '__main__':
    main()
