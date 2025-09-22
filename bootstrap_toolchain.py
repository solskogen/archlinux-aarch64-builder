#!/usr/bin/env python3
"""
Bootstrap toolchain builder for Arch Linux packages.

Builds core toolchain packages (gcc, glibc, binutils, etc.) in a specific order
required for a complete toolchain rebuild. Has special handling for gcc/glibc
which must be manually checked out from special repositories.
"""
import subprocess
import os
import sys
import shutil
import argparse
import signal
from pathlib import Path
from build_utils import BuildUtils, BUILD_ROOT, CACHE_PATH

class BootstrapBuilder(BuildUtils):
    """Bootstrap toolchain package builder"""
    
    # Toolchain packages in build order (some repeated for multi-pass builds)
    TOOLCHAIN_PACKAGES = [
        "linux-api-headers", "glibc", "binutils", "gcc", "gmp", "mpfr", 
        "libmpc", "libisl", "glibc", "binutils", "gcc", "gmp", "mpfr", 
        "libmpc", "libisl", "libtool", "valgrind"
    ]
    
    REQUIRED_TOOLS = ['makechrootpkg', 'pkgctl', 'repo-upload', 'arch-nspawn']
    
    def __init__(self, chroot_path=BUILD_ROOT, cache_path=CACHE_PATH, dry_run=False):
        super().__init__(dry_run)
        self.chroot_path = Path(chroot_path)
        self.cache_path = Path(cache_path)
        self.build_dir = Path("pkgbuilds")
    
    def setup_environment(self):
        """Setup chroot environment for bootstrap"""
        if self.dry_run:
            self.format_dry_run("Would setup bootstrap environment", [
                f"Create directories: {self.build_dir}, {self.cache_path}",
                f"Create chroot at: {self.chroot_path}" if not (self.chroot_path / "root").exists() else f"Use existing chroot: {self.chroot_path}"
            ])
            return
            
        print("Setting up bootstrap environment...")
        
        # Create directories
        self.build_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.cache_path.mkdir(parents=True, exist_ok=True)
        
        # Create chroot if it doesn't exist
        if not (self.chroot_path / "root").exists():
            print("Creating clean chroot...")
            self.chroot_path.mkdir(parents=True, exist_ok=True)
            self.run_command([
                "mkarchroot", 
                "-C", "chroot-config/pacman.conf",
                "-M", "chroot-config/makepkg.conf",
                "-c", str(self.cache_path),
                str(self.chroot_path / "root"),
                "base-devel"
            ])
        else:
            print("Using existing chroot...")
    
    def bootstrap_build_package(self, pkg_name):
        """Build a single package in bootstrap mode"""
        pkg_dir = self.build_dir / pkg_name
        
        if not pkg_dir.exists():
            if pkg_name in ["gcc", "glibc"]:
                print(f"ERROR: {pkg_dir} not found - please checkout {pkg_name} from special repo manually")
                sys.exit(1)
        
        if self.dry_run:
            if pkg_name in ["gcc", "glibc"]:
                self.format_dry_run(f"Would bootstrap build {pkg_name} (special repo)", [
                    "git pull",
                    "Update chroot package database",
                    "Force install toolchain dependencies", 
                    "Clear pacman cache",
                    "Build with makechrootpkg",
                    "Upload to core-testing"
                ])
            else:
                self.format_dry_run(f"Would bootstrap build {pkg_name} (Arch repo)", [
                    "pkgctl repo clone/update",
                    "Update chroot package database",
                    "Force install toolchain dependencies", 
                    "Clear pacman cache",
                    "Build with makechrootpkg",
                    "Upload to core-testing"
                ])
            return True
        
        print(f"Bootstrap building {pkg_name}...")
        
        # Update chroot package database
        try:
            self.run_command([
                "arch-nspawn", str(self.chroot_path / "root"),
                "pacman", "-Sy", "--noconfirm"
            ])
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to update chroot package database: {e}")
            sys.exit(1)
        
        # Force install all toolchain dependencies in chroot
        all_toolchain = self.TOOLCHAIN_PACKAGES + ["gcc-libs"]
        try:
            self.run_command([
                "arch-nspawn", str(self.chroot_path / "root"),
                "pacman", "-S", "--noconfirm"
            ] + all_toolchain)
        except subprocess.CalledProcessError:
            # Ignore failures - some packages might not exist yet
            pass
        
        # Clear pacman cache
        try:
            for item in self.cache_path.iterdir():
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
        except Exception as e:
            print(f"ERROR: Failed to clear cache: {e}")
            sys.exit(1)
        
        # Build package
        try:
            env = os.environ.copy()
            env['SOURCE_DATE_EPOCH'] = str(int(subprocess.run(['date', '+%s'], capture_output=True, text=True).stdout.strip()))
            
            process = subprocess.Popen([
                "makechrootpkg",
                "-r", str(self.chroot_path),
                "-d", str(self.cache_path),  # Use custom cache directory
                "-c",  # Clean chroot
                "-u",  # Update chroot before building
                "--", "--ignorearch",
            ], cwd=pkg_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, errors='replace', env=env)
            
            output_lines = []
            for line in process.stdout:
                print(line, end='')
                output_lines.append(line)
            
            process.wait()
            
            if process.returncode != 0:
                self.logs_dir.mkdir(exist_ok=True)
                timestamp = subprocess.run(['date', '+%Y%m%d-%H%M%S'], capture_output=True, text=True).stdout.strip()
                log_file = self.logs_dir / f"{pkg_name}-{timestamp}-build.log"
                self.cleanup_old_logs(pkg_name)
                with open(log_file, 'w') as f:
                    f.write(f"Bootstrap build failed for {pkg_name}\n")
                    f.write(f"Return code: {process.returncode}\n\n")
                    f.write("OUTPUT:\n")
                    f.write(''.join(output_lines))
                print(f"ERROR: Bootstrap build failed for {pkg_name} (return code: {process.returncode})")
                print(f"Build log written to {log_file}")
                sys.exit(1)
            
            # Upload to core-testing (toolchain always goes to core)
            self.upload_packages(pkg_dir, "core-testing")
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to bootstrap build {pkg_name}: {e}")
            sys.exit(1)
    
    def run_bootstrap(self):
        """Run complete bootstrap toolchain build"""
        print("=== Bootstrap Toolchain Build ===")
        print(f"Building {len(self.TOOLCHAIN_PACKAGES)} toolchain packages in order")
        
        # Atomic lock file creation with PID
        lock_file = Path("bootstrap.lock")
        try:
            if not self.dry_run:
                # Check if lock file exists and if process is still running
                if lock_file.exists():
                    try:
                        lock_content = lock_file.read_text().strip()
                        if lock_content.startswith("PID:"):
                            old_pid = int(lock_content.split(":")[1])
                            # Check if process is still running
                            try:
                                os.kill(old_pid, 0)  # Signal 0 just checks if process exists
                                print("ERROR: Bootstrap already running (bootstrap.lock exists)")
                                print("If no other bootstrap is running, remove bootstrap.lock and try again")
                                sys.exit(1)
                            except OSError:
                                # Process doesn't exist, remove stale lock file
                                lock_file.unlink()
                                print("Removed stale bootstrap.lock file")
                    except (ValueError, IndexError):
                        # Invalid lock file format, remove it
                        lock_file.unlink()
                        print("Removed invalid bootstrap.lock file")
                
                # Create new lock file with current PID
                current_pid = os.getpid()
                lock_file.write_text(f"PID:{current_pid}\nBootstrap started at {subprocess.run(['date'], capture_output=True, text=True).stdout.strip()}")
        except FileExistsError:
            print("ERROR: Bootstrap already running (bootstrap.lock exists)")
            print("If no other bootstrap is running, remove bootstrap.lock and try again")
            sys.exit(1)
        
        # Set up signal handlers for graceful cleanup
        def signal_handler(signum, frame):
            print(f"\nReceived signal {signum}, cleaning up...")
            if not self.dry_run and lock_file.exists():
                lock_file.unlink()
            sys.exit(1)
        
        if not self.dry_run:
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            # Validate required tools exist
            for tool in self.REQUIRED_TOOLS:
                if not self.dry_run and not shutil.which(tool):
                    print(f"ERROR: Required tool '{tool}' not found in PATH")
                    sys.exit(1)
            
            # Validate gcc/glibc directories exist before starting
            for pkg_name in ["gcc", "glibc"]:
                pkg_dir = self.build_dir / pkg_name
                if not pkg_dir.exists():
                    print(f"ERROR: {pkg_dir} not found - please checkout {pkg_name} from special repo manually")
                    sys.exit(1)
            
            # Ensure all toolchain PKGBUILDs are checked out before starting
            print("Checking out all toolchain PKGBUILDs...")
            for pkg_name in self.TOOLCHAIN_PACKAGES:
                pkg_dir = self.build_dir / pkg_name
                
                if pkg_name in ["gcc", "glibc"]:
                    # Special repos - just git pull
                    if self.dry_run:
                        self.format_dry_run(f"Would update {pkg_name} (special repo)", ["git stash", "git pull", "git stash pop"])
                    else:
                        try:
                            # Stash changes, pull, then restore
                            stash_result = self.run_command(["git", "stash"], cwd=pkg_dir, capture_output=True)
                            has_changes = "No local changes to save" not in stash_result.stdout
                            
                            self.run_command(["git", "pull"], cwd=pkg_dir)
                            
                            if has_changes:
                                self.run_command(["git", "stash", "pop"], cwd=pkg_dir)
                            
                            print(f"✓ Updated {pkg_name}")
                        except subprocess.CalledProcessError as e:
                            print(f"ERROR: git pull failed in {pkg_dir}: {e}")
                            sys.exit(1)
                else:
                    # Regular Arch packages - clone or update
                    if pkg_dir.exists():
                        if self.dry_run:
                            self.format_dry_run(f"Would update {pkg_name}", ["git stash", "git pull", "git stash pop"])
                        else:
                            try:
                                # Stash changes, pull, then restore
                                stash_result = self.run_command(["git", "stash"], cwd=pkg_dir, capture_output=True)
                                has_changes = "No local changes to save" not in stash_result.stdout
                                
                                self.run_command(["git", "pull"], cwd=pkg_dir)
                                
                                if has_changes:
                                    self.run_command(["git", "stash", "pop"], cwd=pkg_dir)
                                
                                print(f"✓ Updated {pkg_name}")
                            except subprocess.CalledProcessError as e:
                                print(f"ERROR: git pull failed in {pkg_dir}: {e}")
                                sys.exit(1)
                    else:
                        if self.dry_run:
                            self.format_dry_run(f"Would clone {pkg_name}", [f"pkgctl repo clone {pkg_name}"])
                        else:
                            try:
                                self.run_command(["pkgctl", "repo", "clone", pkg_name], cwd=self.build_dir)
                                print(f"✓ Cloned {pkg_name}")
                            except subprocess.CalledProcessError as e:
                                print(f"ERROR: Failed to clone {pkg_name}: {e}")
                                sys.exit(1)
            
            self.setup_environment()
            
            # Validate chroot environment after setup
            chroot_root = self.chroot_path / "root"
            if not self.dry_run:
                if not chroot_root.exists():
                    print("ERROR: Chroot root directory not found - environment setup failed")
                    sys.exit(1)
                
                chroot_pacman = chroot_root / "usr" / "bin" / "pacman"
                if not chroot_pacman.exists():
                    print("ERROR: Chroot pacman not found - chroot environment is invalid")
                    sys.exit(1)
            
            # Clear pacman cache before starting bootstrap
            if self.dry_run:
                self.format_dry_run("Would clear pacman cache before bootstrap", [f"rm -rf {self.cache_path}/*"])
            else:
                try:
                    for item in self.cache_path.iterdir():
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item)
                    print(f"Cleared cache directory before bootstrap: {self.cache_path}")
                except Exception as e:
                    print(f"ERROR: Failed to clear cache before bootstrap: {e}")
                    sys.exit(1)
            
            built_count = 0
            
            for i, pkg_name in enumerate(self.TOOLCHAIN_PACKAGES, 1):
                print(f"\n=== Building {pkg_name} ({i}/{len(self.TOOLCHAIN_PACKAGES)}) ===")
                self.bootstrap_build_package(pkg_name)
                built_count += 1
                print(f"✓ {pkg_name} built successfully")
            
            print(f"\n=== Bootstrap Summary ===")
            print(f"Successfully built: {built_count}/{len(self.TOOLCHAIN_PACKAGES)}")
            print("Toolchain bootstrap completed successfully!")
            
        finally:
            # Remove lock file
            if not self.dry_run and lock_file.exists():
                lock_file.unlink()

def main():
    parser = argparse.ArgumentParser(description='Bootstrap build toolchain packages')
    parser.add_argument('--chroot', default=BUILD_ROOT,
                       help=f'Chroot path (default: {BUILD_ROOT})')
    parser.add_argument('--cache', default=CACHE_PATH,
                       help=f'Pacman cache directory (default: {CACHE_PATH})')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be done without actually building')
    
    args = parser.parse_args()
    
    builder = BootstrapBuilder(chroot_path=args.chroot, cache_path=args.cache, dry_run=args.dry_run)
    builder.run_bootstrap()

if __name__ == "__main__":
    main()
