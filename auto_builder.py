#!/usr/bin/env python3
"""
Auto Builder - Continuous build daemon for Arch Linux AArch64 packages.

Periodically runs generate_build_list.py to find outdated packages,
then runs the full build + post-build pipeline. Repeats on a configurable interval.

Usage:
    ./auto_builder.py                      # Run with defaults (180s interval)
    ./auto_builder.py --interval 300       # Run every 5 minutes
    ./auto_builder.py --once               # Run once and exit
"""

import argparse
import subprocess
import os
import sys
import signal
import time
import datetime
import configparser
from pathlib import Path

from dynamo_reporter import (
    sync_repo_stats as dynamo_sync_stats,
    mark_queued as dynamo_mark_queued,
    mark_aborted as dynamo_mark_aborted,
    update_build_status as dynamo_update_build,
    update_repo_stat as dynamo_update_stat,
    upload_build_log as dynamo_upload_log,
    get_latest_build_id as dynamo_get_build_id,
)


running = True

# Load configuration
config = configparser.ConfigParser()
config.read('config.ini')

REPOS_PATH = config.get('paths', 'repos_path', fallback='/mnt/repos')
MOVE_SCRIPT = config.get('paths', 'move_to_release_script', fallback=f'{REPOS_PATH}/move-from-testing-to-release.sh')


def signal_handler(signum, frame):
    global running
    print(f"\n[{timestamp()}] Received signal {signum}, stopping after current cycle...")
    running = False


def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_step(cmd, description, shell=False):
    """Run a command, return True on success."""
    print(f"[{timestamp()}] {description}")
    if shell:
        print(f"  Command: {cmd}")
    else:
        print(f"  Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, shell=shell)
    if result.returncode != 0:
        print(f"[{timestamp()}] {description} failed (exit code {result.returncode})")
        return False
    return True


def _get_build_status(package, build_id):
    """Get the current status of a specific build from DynamoDB."""
    try:
        import boto3
        table = boto3.resource("dynamodb", region_name="eu-central-1").Table("ArchBuilder-Builds")
        resp = table.get_item(Key={"PkgName": package, "BuildId": build_id}, ProjectionExpression="#s", ExpressionAttributeNames={"#s": "Status"})
        return resp.get("Item", {}).get("Status")
    except Exception:
        return None



def _sync_db():
    """Update heartbeat, load, and memory stats in DynamoDB."""
    dynamo_sync_stats()


def _promote_if_testing_has_packages():
    """Only run move-from-testing-to-release if testing repos have packages."""
    import glob
    has_packages = (glob.glob(f"{REPOS_PATH}/core-testing_aarch64/*.zst") or
                    glob.glob(f"{REPOS_PATH}/extra-testing_aarch64/*.zst"))
    if has_packages:
        run_step(["sudo", "sh", MOVE_SCRIPT],
                 "Moving testing packages to stable")



def _background_reporter(stop_event):
    """Background thread: sync DB every 30s so the web page sees updates."""
    while not stop_event.is_set():
        try:
            _sync_db()
        except Exception as _e:
            print(f"[{timestamp()}] Background reporter error: {_e}")
        stop_event.wait(30)


def run_cycle(args):
    """Run one generate + build + post-build cycle."""
    print(f"\n{'='*60}")
    print(f"[{timestamp()}] Starting build cycle")
    print(f"{'='*60}")

    # Update heartbeat so the report page knows the builder is alive
    _sync_db()

    # Load failed packages to exclude from this cycle
    failed_file = Path("auto_builder_failures.json")
    failed_packages = {}
    if failed_file.exists():
        import json
        try:
            failed_packages = json.loads(failed_file.read_text())
        except Exception:
            failed_packages = {}

    # Prune failures for packages now in blacklist (no point tracking them)
    if failed_packages and args.blacklist and Path(args.blacklist).exists():
        import fnmatch
        blacklist = [l.strip() for l in open(args.blacklist) if l.strip() and not l.startswith('#')]
        pruned = {k: v for k, v in failed_packages.items()
                  if not any(fnmatch.fnmatch(k, p) for p in blacklist)}
        if len(pruned) < len(failed_packages):
            print(f"[{timestamp()}] Pruned {len(failed_packages) - len(pruned)} blacklisted entries from failure tracker")
            failed_packages = pruned
            failed_file.write_text(json.dumps(failed_packages, indent=2))

    # Sync ARCH=any packages from upstream before checking for builds
    run_step(["./sync_any_packages.py"], "Syncing ARCH=any packages")
    # Generate build list
    gen_cmd = ["./generate_build_list.py"]
    if args.quiet:
        gen_cmd.append("-q")
    gen_cmd += args.generate_args

    if not run_step(gen_cmd, "Generating build list"):
        print(f"[{timestamp()}] generate_build_list.py failed, exiting")
        sys.exit(1)

    # Check if there's anything to build
    packages_file = Path("packages_to_build.json")
    if not packages_file.exists():
        print(f"[{timestamp()}] No packages to build")
        # Clear retry entries - if nothing to build, nothing to retry
        if failed_file.exists():
            import json
            try:
                fp = json.loads(failed_file.read_text())
                pruned = {k: v for k, v in fp.items() if isinstance(v, dict) and v.get("count", 0) >= 2}
                if len(pruned) < len(fp):
                    print(f"[{timestamp()}] Cleared {len(fp) - len(pruned)} retry entries (packages no longer outdated)")
                    if pruned:
                        failed_file.write_text(json.dumps(pruned, indent=2))
                    else:
                        failed_file.unlink()
            except Exception:
                pass
        _promote_if_testing_has_packages()
        run_step(["./generate_report.py"], "Refreshing report")
        return False

    # Filter out previously failed packages (same version)
    if failed_packages:
        import json
        data = json.loads(packages_file.read_text())
        original_count = len(data.get("packages", []))

        # Build set of failed package names that are STILL failed (same version, failed twice)
        still_failed = set()
        for name, info in failed_packages.items():
            if isinstance(info, dict):
                ver, count = info.get("version"), info.get("count", 1)
            else:
                ver, count = info, 1  # Legacy format
            if count >= 2 and any(p["name"] == name and p.get("version") == ver
                                  for p in data.get("packages", [])):
                still_failed.add(name)

        # Also skip packages that depend on still-failed packages
        def has_failed_dep(pkg):
            for dep_type in ('depends', 'makedepends', 'checkdepends'):
                for dep in pkg.get(dep_type, []):
                    dep_name = dep.split('=')[0].split('>')[0].split('<')[0].strip()
                    if dep_name in still_failed:
                        return dep_name
            return None

        kept = []
        for p in data.get("packages", []):
            if p["name"] in still_failed:
                continue  # Same version already failed
            failed_dep = has_failed_dep(p)
            if failed_dep:
                print(f"[{timestamp()}] Skipping {p['name']} (depends on failed: {failed_dep})")
                continue
            kept.append(p)

        filtered = original_count - len(kept)
        data["packages"] = kept
        if filtered:
            print(f"[{timestamp()}] Skipped {filtered} packages (failed or depend on failed)")
        if not data["packages"]:
            print(f"[{timestamp()}] No new packages to build (all failed or blocked, delete auto_builder_failures.json to retry)")
            packages_file.unlink()
            _promote_if_testing_has_packages()
            return False
        packages_file.write_text(json.dumps(data, indent=2))

    # Start background reporter to ingest logs while building
    import threading
    stop_reporter = threading.Event()
    reporter_thread = threading.Thread(target=_background_reporter, args=(stop_reporter,), daemon=True)
    reporter_thread.start()

    # Build packages (may have partial failures)
    success = run_step(["./build_packages.py", "--continue", "--parallel-jobs", "10"], "Building packages")

    # Stop background reporter
    stop_reporter.set()
    reporter_thread.join(timeout=60)

    # Ingest logs
    run_step(["./generate_report.py"], "Ingesting build logs")
    _sync_db()

    post_pipeline = (
        "sync && sleep 3"
        " && sudo sh " + MOVE_SCRIPT + ""
        " && (fd -I pkg.tar.zst pkgbuilds | xargs rm -v )"
    )
    run_step(post_pipeline, "Post-build pipeline", shell=True)

    # Refresh repo stats after move (clears in_testing)
    run_step(["./generate_report.py"], "Refreshing report")

    # Final report + upload + cleanup
    post_cleanup = (
        "grep -l SUCCESS logs/*-build.log | while read f; do"
        "   pkg=$(echo \"$f\" | sed 's|logs/||;s|-[0-9]\\{8\\}-.*||');"
        "   rm -fv logs/${pkg}-*-build.log;"
        " done"
    )
    _sync_db()
    run_step(post_cleanup, "Cleanup", shell=True)

    # Record any new failures (only if build wasn't interrupted)
    failed_json = Path("failed_packages.json")
    if failed_json.exists() and running:
        import json, glob
        try:
            new_failures = json.loads(failed_json.read_text())
            for pkg in new_failures.get("packages", []):
                name = pkg["name"]
                ver = pkg.get("version", "unknown")
                # Increment count if same version, reset if new version
                existing = failed_packages.get(name, {})
                if isinstance(existing, str):
                    existing = {"version": existing, "count": 1}  # Legacy upgrade
                if isinstance(existing, dict) and existing.get("version") == ver:
                    existing["count"] = existing.get("count", 1) + 1
                else:
                    existing = {"version": ver, "count": 1}
                failed_packages[name] = existing
                # Update DB status: RETRY if first failure, keep FAILED if second
                # Only update if the latest build isn't already SUCCESS
                bid = dynamo_get_build_id(name)
                if bid:
                    current = _get_build_status(name, bid)
                    if current not in ("SUCCESS",):
                        if existing["count"] < 2:
                            dynamo_update_build(name, bid, "RETRY", repo=pkg.get("repo", "extra"))
                        elif not any(glob.glob(f"logs/{name}-*-build.log")):
                            dynamo_update_build(name, bid, "SKIPPED",
                                                repo=pkg.get("repo", "extra"))
            failed_file.write_text(json.dumps(failed_packages, indent=2))
            print(f"[{timestamp()}] Tracking {len(failed_packages)} failed packages")
        except Exception:
            pass
        failed_json.unlink()

    print(f"[{timestamp()}] Build cycle {'completed' if success else 'finished with failures'}")
    _sync_db()
    return success


def main():
    parser = argparse.ArgumentParser(description="Continuous build daemon")
    parser.add_argument("--interval", type=int, default=180,
                        help="Seconds between cycles (default: 180)")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle and exit")
    parser.add_argument("--blacklist", default="blacklist.txt",
                        help="Blacklist file (default: blacklist.txt)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Quiet mode for generate_build_list")
    parser.add_argument("--generate-args", nargs=argparse.REMAINDER, default=[],
                        help="Extra args passed to generate_build_list.py")

    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    lock_file = Path("auto_builder.lock")
    if lock_file.exists():
        old_pid = lock_file.read_text().strip()
        try:
            os.kill(int(old_pid), 0)
            print(f"Auto builder already running (PID {old_pid})")
            sys.exit(1)
        except (OSError, ValueError):
            print(f"Removing stale lock file (PID {old_pid})")
            lock_file.unlink()

    lock_file.write_text(str(os.getpid()))

    try:
        print(f"[{timestamp()}] Auto builder started (PID {os.getpid()}, interval: {args.interval}s)")
        os.environ["AUTO_BUILDER"] = "1"
        # Mark auto_builder as running in DB
        dynamo_update_stat("auto_builder_pid", str(os.getpid()))
        _sync_db()

        while running:
            built = run_cycle(args)

            if args.once or not running:
                break

            if built:
                continue

            # Skip wait if there are packages to retry
            try:
                import json as _json
                _fp = _json.loads(Path("auto_builder_failures.json").read_text()) if Path("auto_builder_failures.json").exists() else {}
                has_retry = any(isinstance(v, dict) and v.get("count", 0) < 2 for v in _fp.values())
            except Exception:
                has_retry = False
            if has_retry:
                print(f"[{timestamp()}] Packages pending retry, starting next cycle immediately")
                continue

            print(f"[{timestamp()}] Next cycle in {args.interval}s")
            for i in range(args.interval):
                if not running:
                    break
                time.sleep(1)
                if i % 30 == 29:
                    _sync_db()

        print(f"[{timestamp()}] Auto builder stopped")
    finally:
        # Clear auto_builder PID from DB
        dynamo_update_stat("auto_builder_pid", "")
        _sync_db()
        lock_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
