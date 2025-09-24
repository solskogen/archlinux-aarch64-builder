#!/usr/bin/env python3
"""
Shared utilities for package building scripts.
"""
import subprocess
import sys
from pathlib import Path

# Default build paths
BUILD_ROOT = "/var/tmp/builder"
CACHE_PATH = f"{BUILD_ROOT}/pacman-cache"

class BuildUtils:
    """Shared utilities for package builders"""
    
    UPLOAD_BUCKET = "arch-linux-repos.drzee.net"
    LOG_RETENTION_COUNT = 3
    
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.logs_dir = Path("logs")
    
    def run_command(self, cmd, cwd=None, capture_output=False):
        """Unified command runner with consistent error handling and dry-run support"""
        if self.dry_run:
            print(f"[DRY RUN] Would run: {' '.join(cmd)}")
            if cwd:
                print(f"[DRY RUN] In directory: {cwd}")
            # Return realistic output for git stash to make dry-run logic work
            if cmd[0] == "git" and len(cmd) > 1 and cmd[1] == "stash":
                return subprocess.CompletedProcess(cmd, 0, "No local changes to save", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, cwd=cwd, capture_output=capture_output, text=True, check=True)
    
    def format_dry_run(self, action, details=None):
        """Format dry-run output consistently"""
        if self.dry_run:
            print(f"[DRY RUN] {action}")
            if details:
                for detail in details:
                    print(f"[DRY RUN]   {detail}")
    
    def cleanup_old_logs(self, package_name, keep_count=None):
        """Keep only the most recent N log files for a package"""
        if keep_count is None:
            keep_count = self.LOG_RETENTION_COUNT
            
        if not self.logs_dir.exists():
            return
        
        # Find all log files for this package
        log_pattern = f"{package_name}-*-build.log"
        log_files = list(self.logs_dir.glob(log_pattern))
        
        # Sort by modification time (newest first)
        log_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        
        # Remove old logs beyond keep_count
        for old_log in log_files[keep_count:]:
            old_log.unlink()
    
    def setup_chroot(self, chroot_path, cache_path):
        """Set up or create build chroot environment"""
        chroot_path = Path(chroot_path)
        cache_path = Path(cache_path)
        
        if self.dry_run:
            self.format_dry_run(f"Would setup chroot at {chroot_path}", [
                f"Create cache directory: {cache_path}",
                f"Create chroot with mkarchroot" if not (chroot_path / "root").exists() else f"Use existing chroot"
            ])
            return
        
        # Create cache directory
        cache_path.mkdir(parents=True, exist_ok=True)
        
        # Create chroot if it doesn't exist
        if not (chroot_path / "root").exists():
            print(f"Creating chroot at {chroot_path}")
            chroot_path.mkdir(parents=True, exist_ok=True)
            self.run_command([
                "mkarchroot", 
                "-C", "chroot-config/pacman.conf",
                "-M", "chroot-config/makepkg.conf",
                "-c", str(cache_path),
                str(chroot_path / "root"),
                "base-devel"
            ])
        else:
            print("Using existing chroot...")
    
    def upload_packages(self, pkg_dir, target_repo):
        """Upload all built packages to repository"""
        built_packages = [str(f) for f in pkg_dir.glob("*.pkg.tar.*") if not f.name.endswith('.sig')]
        
        if not built_packages:
            print(f"ERROR: No packages found to upload in {pkg_dir}")
            sys.exit(1)
        
        for pkg in built_packages:
            try:
                self.run_command([
                    "repo-upload", pkg,
                    "--arch", "aarch64",
                    "--repo", target_repo,
                    "--bucket", self.UPLOAD_BUCKET
                ])
                print(f"Uploaded {Path(pkg).name} to {target_repo}")
            except subprocess.CalledProcessError as e:
                print(f"ERROR: Failed to upload {Path(pkg).name}: {e}")
                sys.exit(1)
        
        return len(built_packages)
