#!/usr/bin/env python3
"""
Find which packages ship a given path, like `pacman -F`.

Answers "which packages still contain /usr/lib/perl5/5.42?" across every
repository for the target architecture, without installing anything or needing
the file databases that pacman would use locally.

Two sources, because neither is complete on its own:

  --from-db        (default) the published {repo}.files databases, fetched over
                   HTTP. This is exactly what pacman -F reads, and it is fast.
                   The copies under paths.repos_path are 0 bytes here, so the
                   remote ones are used. Note these list regular files only --
                   they hold no directory entries -- so an --exact match on a
                   bare directory will not find anything.

  --from-packages  the actual *.pkg.tar.zst under paths.repos_path. Slower, but
                   authoritative: it sees directory entries, and it sees packages
                   that are on disk but not yet published in a .files database --
                   which is the normal state for freshly built packages in a
                   -testing repo.

Usage:
    ./find_package_files.py /usr/lib/perl5/5.42
    ./find_package_files.py perl5/5.42 --list
    ./find_package_files.py -x 'perl5/5\\.4[0-2]($|/)'
    ./find_package_files.py /usr/lib/perl5/5.42 --from-packages
    ./find_package_files.py /usr/lib/perl5/5.42 --rebuild-order
"""

import argparse
import json
import os
import re
import sys
import tarfile
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from utils import config, get_target_architecture

CACHE_DIR = Path('.files_cache')
CACHE_MAX_AGE = 3600          # reuse a downloaded .files for an hour
DEFAULT_REPOS = ['core', 'extra', 'forge', 'core-testing', 'extra-testing']


def pkgname_from_dir(dirname):
    """'perl-5.44.0-1/files' dir component -> 'perl'"""
    parts = dirname.rsplit('-', 2)
    return parts[0] if len(parts) == 3 else dirname


def pkgname_from_file(filename):
    """libfoo-1.2.3-4-aarch64.pkg.tar.zst -> libfoo"""
    stem = filename
    for suffix in ('.pkg.tar.zst', '.pkg.tar.xz', '.pkg.tar.gz'):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    parts = stem.rsplit('-', 3)
    return parts[0] if len(parts) == 4 else stem


def build_matcher(patterns, exact=False, regex=False):
    """
    Return match(path) -> bool.

    Paths in both sources are stored without a leading slash, so a leading slash
    in the pattern is dropped rather than making every match fail. A pattern
    containing '/' is matched against the whole path; a bare name is matched
    against the basename, which is how `pacman -F name` behaves.
    """
    if regex:
        compiled = [re.compile(p) for p in patterns]
        return lambda path: any(c.search(path) for c in compiled)

    norm = [p.lstrip('/') for p in patterns]
    if exact:
        wanted = set(norm)
        return lambda path: path.rstrip('/') in wanted

    def match(path):
        bare = path.rstrip('/')
        base = bare.rsplit('/', 1)[-1]
        for p in norm:
            if '/' in p:
                if p in bare:
                    return True
            elif p == base:
                return True
        return False

    return match


def fetch_files_db(repo, target_arch, refresh=False, quiet=False):
    """Download (and cache) {repo}.files. Returns bytes, or None."""
    base_url = config.get('build', 'target_base_url').rstrip('/')
    url = f'{base_url}/{repo}/os/{target_arch}/{repo}.files'
    CACHE_DIR.mkdir(exist_ok=True)
    cached = CACHE_DIR / f'{repo}.files'

    if cached.exists() and not refresh:
        age = time.time() - cached.stat().st_mtime
        if age < CACHE_MAX_AGE and cached.stat().st_size > 0:
            if not quiet:
                print(f'  {repo}: using cached .files ({int(age)}s old)', file=sys.stderr)
            return cached.read_bytes()

    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            blob = resp.read()
    except Exception as e:
        print(f'WARNING: could not fetch {url}: {e}', file=sys.stderr)
        return None
    try:
        cached.write_bytes(blob)
    except Exception:
        pass
    if not quiet:
        print(f'  {repo}: downloaded {len(blob) // 1024} KiB', file=sys.stderr)
    return blob


def search_files_db(blob, repo, match):
    """Scan one .files database. Returns {pkgname: (repo, version, [paths])}."""
    found = {}
    import io
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tf:
        for member in tf:
            if not member.name.endswith('/files'):
                continue
            dirname = member.name.split('/', 1)[0]
            handle = tf.extractfile(member)
            if handle is None:
                continue
            body = handle.read().decode('utf-8', 'replace')
            hits = [line for line in body.split('\n')
                    if line and not line.startswith('%') and match(line)]
            if hits:
                name = pkgname_from_dir(dirname)
                version = dirname[len(name) + 1:] if dirname.startswith(name + '-') else ''
                found[name] = (repo, version, sorted(hits))
    return found


def _scan_one_package(path_str, patterns, exact, regex):
    """Worker: list a package's members and match. Returns (pkg, repo, ver, hits)."""
    match = build_matcher(patterns, exact, regex)
    path = Path(path_str)
    try:
        try:
            from compression import zstd
            opener = lambda: zstd.open(path, 'rb')
        except ImportError:
            import subprocess
            opener = lambda: subprocess.Popen(
                ['zstd', '-dc', str(path)], stdout=subprocess.PIPE).stdout
        hits = []
        with opener() as stream:
            with tarfile.open(fileobj=stream, mode='r|') as tf:
                for member in tf:
                    if member.name.startswith('.'):
                        continue      # .PKGINFO, .BUILDINFO, .MTREE
                    if match(member.name):
                        hits.append(member.name)
        return (pkgname_from_file(path.name), path.parent.name, path.name, sorted(hits), None)
    except Exception as e:
        return (pkgname_from_file(path.name), path.parent.name, path.name, [], str(e))


def main():
    target_arch = get_target_architecture()
    repos_path = Path(config.get('paths', 'repos_path', fallback='/mnt/repos'))

    ap = argparse.ArgumentParser(
        description=f'Find which {target_arch} packages ship a given path (like pacman -F)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  %(prog)s /usr/lib/perl5/5.42\n'
               '  %(prog)s perl5/5.42 --list\n'
               "  %(prog)s -x 'perl5/5\\.4[0-2]($|/)'\n"
               '  %(prog)s /usr/lib/perl5/5.42 --from-packages\n'
               '  %(prog)s /usr/lib/perl5/5.42 --rebuild-order\n')
    ap.add_argument('pattern', nargs='+',
                    help='Path or name to look for. A leading / is optional; a pattern '
                         'containing / matches anywhere in the path, a bare name matches '
                         'the file name')
    ap.add_argument('--repos', nargs='+', default=DEFAULT_REPOS,
                    help=f'Repositories to search (default: {" ".join(DEFAULT_REPOS)})')
    ap.add_argument('-e', '--exact', action='store_true',
                    help='Require the whole path to equal the pattern')
    ap.add_argument('-x', '--regex', action='store_true',
                    help='Treat patterns as regular expressions')
    ap.add_argument('-l', '--list', action='store_true',
                    help='List every matching path, not just the owning packages')
    ap.add_argument('--from-packages', action='store_true',
                    help='Read the *.pkg.tar.zst files on disk instead of the published '
                         '.files databases. Slower, but sees packages that are built but '
                         'not yet in a database')
    ap.add_argument('--jobs', type=int, default=min(12, os.cpu_count() or 4),
                    help='Parallel workers for --from-packages (default: min(12, ncpu))')
    ap.add_argument('--refresh', action='store_true', help='Ignore cached .files downloads')
    ap.add_argument('--rebuild-order', action='store_true',
                    help='Print the owning packages in dependency build order')
    ap.add_argument('--json', dest='json_out', help='Write results to this JSON file')
    ap.add_argument('-q', '--quiet', action='store_true', help='Only print results')
    args = ap.parse_args()

    if args.exact and args.regex:
        print('ERROR: --exact and --regex are mutually exclusive', file=sys.stderr)
        return 2

    match = build_matcher(args.pattern, args.exact, args.regex)
    results = {}        # pkgname -> (repo, version, [paths])
    errors = {}

    if args.from_packages:
        pkg_files = []
        for repo in args.repos:
            d = repos_path / f'{repo}_{target_arch}'
            if not d.is_dir():
                if not args.quiet:
                    print(f'  {repo}: {d} not found, skipping', file=sys.stderr)
                continue
            pkg_files.extend(sorted(p for p in d.glob('*.pkg.tar.*')
                                    if not p.name.endswith('.sig')))
        if not pkg_files:
            print('ERROR: no package files found', file=sys.stderr)
            return 1
        if not args.quiet:
            print(f'Scanning {len(pkg_files)} package(s) with {args.jobs} worker(s)...',
                  file=sys.stderr)
        t0 = time.time()
        done = 0
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futs = [ex.submit(_scan_one_package, str(p), args.pattern,
                              args.exact, args.regex) for p in pkg_files]
            for fut in as_completed(futs):
                name, repo_dir, filename, hits, err = fut.result()
                done += 1
                if err:
                    errors[filename] = err
                elif hits:
                    repo = repo_dir.rsplit(f'_{target_arch}', 1)[0]
                    results[name] = (repo, '', hits)
                if not args.quiet and done % 2000 == 0:
                    print(f'  scanned {done}/{len(pkg_files)} '
                          f'({time.time() - t0:.0f}s)', file=sys.stderr)
    else:
        if not args.quiet:
            print(f'Searching .files databases for {target_arch}...', file=sys.stderr)
        for repo in args.repos:
            blob = fetch_files_db(repo, target_arch, args.refresh, args.quiet)
            if not blob:
                continue
            try:
                found = search_files_db(blob, repo, match)
            except Exception as e:
                print(f'WARNING: could not read {repo}.files: {e}', file=sys.stderr)
                continue
            # Earlier repos in the list win, matching pacman's repo precedence.
            for name, info in found.items():
                results.setdefault(name, info)

    # ---- output ----
    if not results:
        print(f'No packages ship a path matching: {", ".join(args.pattern)}')
        if not args.from_packages:
            print('Note: .files databases record regular files only -- they contain no '
                  'directory entries -- and they can lag freshly built packages. To '
                  'match a bare directory, or to see packages not yet published in a '
                  'database, re-run with --from-packages.', file=sys.stderr)
        return 1

    total_paths = sum(len(v[2]) for v in results.values())
    print(f'{len(results)} package(s) ship a matching path '
          f'({total_paths} path(s) total):')
    for name in sorted(results):
        repo, version, paths = results[name]
        label = f'{repo}/{name}' + (f' {version}' if version else '')
        print(f'  {label}  ({len(paths)} path(s))')
        if args.list:
            for p in paths:
                print(f'      /{p}')

    if args.rebuild_order:
        print('\nRebuild order (dependency-sorted pkgbase names):')
        try:
            import subprocess
            r = subprocess.run(['./find_dependents.py', '-r'] + sorted(results),
                               capture_output=True, text=True, timeout=600)
            print('  ' + (r.stdout.strip() or ' '.join(sorted(results))))
        except Exception:
            print('  ' + ' '.join(sorted(results)))

    if errors:
        print(f'\nWARNING: {len(errors)} package(s) could not be read:', file=sys.stderr)
        for filename, err in list(errors.items())[:10]:
            print(f'  {filename}: {err}', file=sys.stderr)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            'pattern': args.pattern,
            'arch': target_arch,
            'repos': args.repos,
            'source': 'packages' if args.from_packages else 'files-db',
            'packages': {k: {'repo': v[0], 'version': v[1], 'paths': v[2]}
                         for k, v in results.items()},
        }, indent=2))
        print(f'\nWrote {args.json_out}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
