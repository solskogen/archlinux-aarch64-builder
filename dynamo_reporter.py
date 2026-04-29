"""
dynamo_reporter.py — DynamoDB reporting for the Arch Linux AArch64 builder.

Writes build status and repo stats to DynamoDB tables, uploads build logs to S3.

Tables:
  ArchBuilder-Builds    — PK: PkgName, SK: BuildId ({timestamp}#{version})
  ArchBuilder-RepoStats — PK: key
"""

import gzip
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

BUILDS_TABLE = "ArchBuilder-Builds"
LATEST_TABLE = "ArchBuilder-Latest"
STATS_TABLE = "ArchBuilder-RepoStats"
S3_BUCKET = "arch-linux-repos.drzee.net"
S3_LOG_PREFIX = "arch/reports/logs"
REGION = "eu-central-1"

MAX_BUILDS = 3

TIER_THRESHOLDS = [(300, "small"), (1800, "medium"), (7200, "large")]

_ddb = None
_s3 = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        import boto3
        _ddb = boto3.resource("dynamodb", region_name=REGION)
    return _ddb


def _get_s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=REGION)
    return _s3


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _now_safe():
    """S3/key-safe timestamp (underscores instead of colons)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H_%M_%S")


def _make_build_id(version, timestamp=None):
    """Create a build_id: {safe-timestamp}#{version}."""
    return f"{timestamp or _now_safe()}#{version}"


def _update_latest(package, status):
    """Update the ArchBuilder-Latest table with current status."""
    try:
        _get_ddb().Table(LATEST_TABLE).put_item(Item={"PkgName": package, "Status": status})
    except Exception:
        pass


def _classify_tier(secs):
    for threshold, name in TIER_THRESHOLDS:
        if secs <= threshold:
            return name
    return "xlarge"


def update_build_status(package, build_id, status, version=None, repo="extra",
                        started=None, finished=None, duration_secs=None,
                        avg_cpu_pct=None, peak_mem_mb=None):
    """Write or update a build record in DynamoDB."""
    try:
        table = _get_ddb().Table(BUILDS_TABLE)
        expr_parts = ["#s = :s", "Repo = :r"]
        names = {"#s": "Status"}
        values = {":s": status, ":r": repo}

        if version:
            expr_parts.append("PkgVersion = :v")
            values[":v"] = version
        if started:
            expr_parts.append("BuildStart = :bs")
            values[":bs"] = started
        if finished:
            expr_parts.append("BuildEnd = :be")
            values[":be"] = finished
        if duration_secs is not None and duration_secs > 0:
            expr_parts.append("Tier = :t")
            values[":t"] = _classify_tier(duration_secs)
        if avg_cpu_pct is not None:
            expr_parts.append("AvgCpuPct = :cpu")
            values[":cpu"] = str(avg_cpu_pct)
        if peak_mem_mb is not None:
            expr_parts.append("PeakMemMb = :mem")
            values[":mem"] = peak_mem_mb

        table.update_item(
            Key={"PkgName": package, "BuildId": build_id},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        _update_latest(package, status)
    except Exception as e:
        print(f"Warning: DynamoDB update_build_status failed for {package}: {e}")


def upload_build_log(package, build_id, log_path):
    """Upload a build log to S3 as gzip, update DynamoDB with the key."""
    try:
        log_path = Path(log_path)
        if not log_path.exists() or log_path.stat().st_size == 0:
            return ""
        # Use timestamp portion of build_id for S3 key (avoid # in path)
        ts = build_id.split("#")[0]
        s3_key = f"{S3_LOG_PREFIX}/{package}/{ts}.log.gz"
        _get_s3().put_object(
            Bucket=S3_BUCKET, Key=s3_key,
            Body=gzip.compress(log_path.read_bytes()),
            ContentType="application/gzip",
        )
        _get_ddb().Table(BUILDS_TABLE).update_item(
            Key={"PkgName": package, "BuildId": build_id},
            UpdateExpression="SET LogS3Key = :l",
            ExpressionAttributeValues={":l": s3_key},
        )
        _cleanup_old_builds(package)
        return s3_key
    except Exception as e:
        print(f"Warning: log upload failed for {package}: {e}")
        return ""


class LiveLogUploader:
    """Periodically uploads a build log to S3 while building."""

    def __init__(self, package, build_id, log_path, interval=30):
        ts = build_id.split("#")[0]
        self.package = package
        self.build_id = build_id
        self.s3_key = f"{S3_LOG_PREFIX}/{package}/{ts}.log.gz"
        self.log_path = Path(log_path)
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def _upload_loop(self):
        while not self._stop.wait(self.interval):
            try:
                if self.log_path.exists() and self.log_path.stat().st_size > 0:
                    _get_s3().put_object(
                        Bucket=S3_BUCKET, Key=self.s3_key,
                        Body=gzip.compress(self.log_path.read_bytes()),
                        ContentType="application/gzip",
                    )
            except Exception:
                pass

    def __enter__(self):
        try:
            content = self.log_path.read_bytes() if self.log_path.exists() else b"Build starting...\n"
            _get_s3().put_object(
                Bucket=S3_BUCKET, Key=self.s3_key,
                Body=gzip.compress(content), ContentType="application/gzip",
            )
            _get_ddb().Table(BUILDS_TABLE).update_item(
                Key={"PkgName": self.package, "BuildId": self.build_id},
                UpdateExpression="SET LogS3Key = :l",
                ExpressionAttributeValues={":l": self.s3_key},
            )
        except Exception:
            pass
        self._thread = threading.Thread(target=self._upload_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


def _cleanup_old_builds(package):
    """Keep only the newest MAX_BUILDS entries per package, delete the rest + their S3 logs."""
    try:
        table = _get_ddb().Table(BUILDS_TABLE)
        resp = table.query(
            KeyConditionExpression="PkgName = :p",
            ExpressionAttributeValues={":p": package},
            ProjectionExpression="PkgName, BuildId, LogS3Key",
            ScanIndexForward=False,  # newest first (BuildId starts with timestamp)
        )
        items = resp.get("Items", [])
        if len(items) <= MAX_BUILDS:
            return
        for item in items[MAX_BUILDS:]:
            table.delete_item(Key={"PkgName": item["PkgName"], "BuildId": item["BuildId"]})
            s3_key = item.get("LogS3Key", "")
            if s3_key:
                try:
                    _get_s3().delete_object(Bucket=S3_BUCKET, Key=s3_key)
                except Exception:
                    pass
    except Exception as e:
        print(f"Warning: cleanup_old_builds failed for {package}: {e}")


def mark_queued(packages):
    """Mark a list of packages as QUEUED. Returns {pkg_name: build_id} mapping."""
    now = _now_iso()
    now_safe = _now_safe()
    build_ids = {}
    try:
        table = _get_ddb().Table(BUILDS_TABLE)
        latest = _get_ddb().Table(LATEST_TABLE)
        with table.batch_writer() as batch, latest.batch_writer() as lbatch:
            for pkg in packages:
                name = pkg["name"]
                if name in build_ids:
                    continue  # Skip duplicates (cycle packages)
                ver = pkg.get("version", "")
                bid = _make_build_id(ver, now_safe)
                build_ids[pkg["name"]] = bid
                batch.put_item(Item={
                    "PkgName": pkg["name"],
                    "BuildId": bid,
                    "PkgVersion": ver,
                    "Status": "QUEUED",
                    "BuildStart": now,
                    "Repo": pkg.get("repo", "extra"),
                })
                lbatch.put_item(Item={"PkgName": pkg["name"], "Status": "QUEUED"})
    except Exception as e:
        print(f"Warning: DynamoDB mark_queued failed: {e}")
    return build_ids


def mark_building(package, build_id):
    """Mark a package as BUILDING with current timestamp."""
    update_build_status(package, build_id, "BUILDING", started=_now_iso())


def mark_aborted():
    """Mark all QUEUED/BUILDING items as ABORTED."""
    try:
        table = _get_ddb().Table(BUILDS_TABLE)
        resp = table.scan(
            FilterExpression="#s IN (:q, :b)",
            ExpressionAttributeNames={"#s": "Status"},
            ExpressionAttributeValues={":q": "QUEUED", ":b": "BUILDING"},
        )
        for item in resp.get("Items", []):
            table.update_item(
                Key={"PkgName": item["PkgName"], "BuildId": item["BuildId"]},
                UpdateExpression="SET #s = :a",
                ExpressionAttributeNames={"#s": "Status"},
                ExpressionAttributeValues={":a": "ABORTED"},
            )
            # Only update Latest for packages that were actually BUILDING
            # QUEUED packages may have a previous SUCCESS that we shouldn't overwrite
            if item.get("Status", {}).get("S", item.get("Status")) == "BUILDING":
                _update_latest(item["PkgName"], "ABORTED")
    except Exception as e:
        print(f"Warning: DynamoDB mark_aborted failed: {e}")


def get_latest_build_id(package):
    """Return the most recent BuildId for a package, or None."""
    try:
        resp = _get_ddb().Table(BUILDS_TABLE).query(
            KeyConditionExpression="PkgName = :p",
            ExpressionAttributeValues={":p": package},
            ScanIndexForward=False, Limit=1,
            ProjectionExpression="BuildId",
        )
        items = resp.get("Items", [])
        return items[0]["BuildId"] if items else None
    except Exception:
        return None


def update_repo_stat(key, value):
    """Write a single key-value pair to RepoStats."""
    try:
        _get_ddb().Table(STATS_TABLE).put_item(
            Item={"key": key, "value": str(value), "updated_at": _now_iso()}
        )
    except Exception as e:
        print(f"Warning: DynamoDB update_repo_stat({key}) failed: {e}")


def sync_repo_stats():
    """Write heartbeat, load average, and memory stats."""
    try:
        update_repo_stat("last_check", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        la = os.getloadavg()
        update_repo_stat("load_avg", f"{la[0]:.1f} {la[1]:.1f} {la[2]:.1f}")
        mi = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                mi[parts[0].rstrip(":")] = int(parts[1])
        update_repo_stat("memory",
            f"{(mi['MemTotal']-mi['MemAvailable'])//1024}/{mi['MemTotal']//1024}MB ram, "
            f"{(mi['SwapTotal']-mi['SwapFree'])//1024}/{mi['SwapTotal']//1024}MB swap")
    except Exception as e:
        print(f"Warning: sync_repo_stats failed: {e}")


