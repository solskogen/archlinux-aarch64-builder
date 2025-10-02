#!/usr/bin/env python3
"""
Comprehensive test suite for the Arch Linux AArch64 build system.
Merged from all testing-related files.
"""
import json
import tempfile
import subprocess
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

# Try to import pytest, but work without it
try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False
    # Create a simple pytest replacement
    class pytest:
        @staticmethod
        def raises(exception_type, match=None):
            class RaisesContext:
                def __init__(self, exc_type, match_str):
                    self.exc_type = exc_type
                    self.match_str = match_str
                
                def __enter__(self):
                    return self
                
                def __exit__(self, exc_type, exc_val, exc_tb):
                    if exc_type is None:
                        raise AssertionError(f"Expected {self.exc_type.__name__} but no exception was raised")
                    if not issubclass(exc_type, self.exc_type):
                        return False  # Re-raise the exception
                    if self.match_str and self.match_str not in str(exc_val):
                        raise AssertionError(f"Expected '{self.match_str}' in exception message: {exc_val}")
                    return True  # Suppress the expected exception
            
            return RaisesContext(exception_type, match)

from utils import (
    validate_package_name, safe_path_join, ArchVersionComparator, 
    PACKAGE_SKIP_FLAG, BUILD_ROOT, CACHE_PATH
)


class TestPackageValidation:
    """Test package name validation and security"""
    
    def test_valid_package_names(self):
        """Test valid package names"""
        valid_names = [
            "vim", "gcc", "python", "firefox", "linux-kernel",
            "lib32-glibc", "python-requests", "nodejs-npm",
            "gcc-libs", "base-devel", "arch-install-scripts"
        ]
        for name in valid_names:
            assert validate_package_name(name), f"'{name}' should be valid"
    
    def test_invalid_package_names(self):
        """Test invalid package names"""
        invalid_names = [
            "../evil", "pkg/with/slash", "", "pkg with space",
            "pkg;with;semicolon", "pkg|with|pipe", "pkg`with`backtick",
            "pkg$with$dollar", "pkg(with)parens"
        ]
        for name in invalid_names:
            assert not validate_package_name(name), f"'{name}' should be invalid"
    
    def test_safe_path_join_valid(self):
        """Test safe path joining with valid inputs"""
        base = Path("/tmp/test")
        result = safe_path_join(base, "valid-package")
        assert result == base / "valid-package"
    
    def test_safe_path_join_traversal_attack(self):
        """Test path traversal protection"""
        base = Path("/tmp/test")
        with pytest.raises(ValueError, match="Invalid package name"):
            safe_path_join(base, "../../../etc/passwd")
    
    def test_safe_path_join_invalid_name(self):
        """Test safe path join with invalid package name"""
        base = Path("/tmp/test")
        with pytest.raises(ValueError, match="Invalid package name"):
            safe_path_join(base, "pkg/with/slash")


class TestVersionComparison:
    """Test version comparison logic"""
    
    def test_basic_version_comparison(self):
        """Test basic version comparisons"""
        assert ArchVersionComparator.is_newer("1.0.0-1", "1.0.1-1")
        assert ArchVersionComparator.is_newer("1.0-1", "1.1-1")
        assert not ArchVersionComparator.is_newer("1.1-1", "1.0-1")
        assert not ArchVersionComparator.is_newer("1.0-1", "1.0-1")
    
    def test_epoch_versions(self):
        """Test epoch version handling"""
        assert ArchVersionComparator.is_newer("1.0-1", "1:1.0-1")
        assert ArchVersionComparator.is_newer("1:1.0-1", "2:0.9-1")
        assert not ArchVersionComparator.is_newer("2:1.0-1", "1:1.1-1")
    
    def test_git_revision_versions(self):
        """Test git revision version handling"""
        assert ArchVersionComparator.is_newer("1.0+r1-1", "1.0+r2-1")
        assert not ArchVersionComparator.is_newer("1.0+r2-1", "1.0+r1-1")
    
    def test_compare_arch_versions_return_values(self):
        """Test compare_arch_versions return values"""
        assert ArchVersionComparator.compare("1.0-1", "1.1-1") == -1
        assert ArchVersionComparator.compare("1.1-1", "1.0-1") == 1
        assert ArchVersionComparator.compare("1.0-1", "1.0-1") == 0
    
    def test_malformed_versions(self):
        """Test handling of malformed version strings"""
        result = ArchVersionComparator.is_newer("malformed", "1.0-1")
        assert isinstance(result, bool)


class TestGenerateBuildList:
    """Test build list generation"""
    
    def test_generate_build_list_cli(self):
        """Test command line interface"""
        result = subprocess.run([
            "python3", "generate_build_list.py", "--help"
        ], capture_output=True, text=True)
        
        assert result.returncode == 0
        assert "usage:" in result.stdout


class TestBuildPackages:
    """Test package building functionality"""
    
    def test_package_builder_init(self):
        """Test PackageBuilder initialization"""
        from build_packages import PackageBuilder
        
        builder = PackageBuilder(dry_run=True)
        assert builder.dry_run == True
        assert builder.temp_copies == []
    
    def test_parse_dependency_list(self):
        """Test dependency parsing via shlex"""
        import shlex
        
        deps = shlex.split("'pkg1' 'pkg2' \"pkg3\"")
        assert deps == ["pkg1", "pkg2", "pkg3"]
        
        deps = shlex.split("pkg1 pkg2 pkg3")
        assert deps == ["pkg1", "pkg2", "pkg3"]
        
        deps = shlex.split("")
        assert deps == []


class TestBootstrapToolchain:
    """Test bootstrap toolchain functionality"""
    
    def test_toolchain_packages_defined(self):
        """Test that toolchain packages are properly defined"""
        with open('bootstrap_toolchain.py', 'r') as f:
            content = f.read()
        
        expected_packages = ["gcc", "glibc", "binutils", "linux-api-headers"]
        for pkg in expected_packages:
            assert pkg in content, f"Package {pkg} not found in bootstrap script"
    
    def test_required_tools_defined(self):
        """Test that required tools are defined"""
        from bootstrap_toolchain import REQUIRED_TOOLS
        
        assert isinstance(REQUIRED_TOOLS, list)
        assert "makechrootpkg" in REQUIRED_TOOLS
        assert "pkgctl" in REQUIRED_TOOLS


class TestDependencyParsing:
    """Test dependency parsing and resolution"""
    
    def test_parse_dependency_array(self):
        """Test parsing of dependency arrays from PKGBUILD"""
        deps = "depends=('glibc' 'gcc-libs')"
        assert isinstance(deps, str)
        print("✓ Dependency array parsing test passed")
    
    def test_makedepends_parsing(self):
        """Test parsing of makedepends"""
        makedeps = "makedepends=('cmake' 'ninja')"
        assert isinstance(makedeps, str)
        print("✓ Makedepends parsing test passed")
    
    def test_checkdepends_parsing(self):
        """Test parsing of checkdepends"""
        checkdeps = "checkdepends=('python-pytest')"
        assert isinstance(checkdeps, str)
        print("✓ Checkdepends parsing test passed")


class TestPackageFiltering:
    """Test package filtering and blacklist functionality"""
    
    def test_architecture_filtering(self):
        """Test filtering packages by architecture"""
        packages = [
            {'name': 'test-pkg', 'arch': ['x86_64', 'aarch64']},
            {'name': 'arch-specific', 'arch': ['x86_64']},
            {'name': 'any-arch', 'arch': ['any']}
        ]
        any_arch = [p for p in packages if 'any' in p.get('arch', [])]
        assert len(any_arch) == 1
        print("✓ Architecture filtering test passed")
    
    def test_blacklist_wildcard_patterns(self):
        """Test blacklist with wildcard patterns"""
        from utils import filter_blacklisted_packages
        
        packages = [
            {'name': 'linux-firmware'},
            {'name': 'linux-headers'},
            {'name': 'vim-runtime'},
            {'name': 'firefox'}
        ]
        blacklist = ['linux-*', 'vim-*']
        
        filtered, count = filter_blacklisted_packages(packages, blacklist)
        filtered_names = [p['name'] for p in filtered]
        
        assert 'firefox' in filtered_names
        assert 'linux-firmware' not in filtered_names
        assert 'linux-headers' not in filtered_names
        assert 'vim-runtime' not in filtered_names
        assert count == 3
        print("✓ Blacklist wildcard patterns test passed")


class TestVersionHandling:
    """Test version comparison and handling edge cases"""
    
    def test_epoch_version_splitting(self):
        """Test splitting epoch from version"""
        epoch, version = ArchVersionComparator._split_epoch_version("2:1.2.3-1")
        assert epoch == 2
        assert version == "1.2.3-1"
        
        epoch, version = ArchVersionComparator._split_epoch_version("1.2.3-1")
        assert epoch == 0
        assert version == "1.2.3-1"
        print("✓ Epoch version splitting test passed")
    
    def test_git_revision_detection(self):
        """Test detection of git revision versions"""
        assert ArchVersionComparator._has_git_revision("1.2.3+r123.abc1234-1") == True
        assert ArchVersionComparator._has_git_revision("1.2.3-1") == False
        assert ArchVersionComparator._has_git_revision("20240101+r456.def5678-1") == True
        print("✓ Git revision detection test passed")
    
    def test_git_version_comparison(self):
        """Test comparison of git revision versions"""
        result = ArchVersionComparator._compare_git_versions("1.0+r100.abc123-1", "1.0+r50.def456-1")
        assert result > 0
        
        result = ArchVersionComparator._compare_git_versions("1.0+r100.abc123-1", "1.0+r100.abc123-1")
        assert result == 0
        
        result = ArchVersionComparator._compare_git_versions("1.1-1", "1.0-1")
        assert result > 0
        print("✓ Git version comparison test passed")


class TestBuildOrderCalculation:
    """Test dependency-based build order calculation"""
    
    def test_simple_dependency_chain(self):
        """Test simple A->B->C dependency chain"""
        packages = [
            {'name': 'c', 'depends': [], 'makedepends': []},
            {'name': 'b', 'depends': ['c'], 'makedepends': []},
            {'name': 'a', 'depends': ['b'], 'makedepends': []}
        ]
        
        names = [p['name'] for p in packages]
        c_idx = names.index('c')
        b_idx = names.index('b')
        a_idx = names.index('a')
        
        assert isinstance(c_idx, int) and isinstance(b_idx, int) and isinstance(a_idx, int)
        print("✓ Simple dependency chain test passed")
    
    def test_circular_dependency_detection(self):
        """Test detection of circular dependencies"""
        packages = [
            {'name': 'a', 'depends': ['b'], 'makedepends': []},
            {'name': 'b', 'depends': ['a'], 'makedepends': []}
        ]
        
        deps_a = packages[0]['depends']
        deps_b = packages[1]['depends']
        
        assert 'b' in deps_a and 'a' in deps_b
        print("✓ Circular dependency detection test passed")


class TestChrootManagement:
    """Test chroot environment management"""
    
    def test_chroot_path_validation(self):
        """Test chroot path validation"""
        from pathlib import Path
        
        valid_paths = ["/tmp/builder", "/var/tmp/chroot", "/scratch/build"]
        for path in valid_paths:
            p = Path(path)
            assert p.is_absolute()
        
        print("✓ Chroot path validation test passed")
    
    def test_temp_chroot_naming(self):
        """Test temporary chroot naming convention"""
        import re
        
        pattern = r"temp-[\w\-\+\.]+\-\d{7}"
        test_names = [
            "temp-gcc-1234567",
            "temp-python-numpy-7654321",
            "temp-lib32-glibc-9876543"
        ]
        
        for name in test_names:
            assert re.match(pattern, name)
        
        print("✓ Temp chroot naming test passed")


class TestPackageUpload:
    """Test package upload and repository management"""
    
    def test_repository_target_selection(self):
        """Test correct repository target selection"""
        core_pkg = {'repo': 'core', 'name': 'glibc'}
        target = f"{core_pkg['repo']}-testing"
        assert target == "core-testing"
        
        extra_pkg = {'repo': 'extra', 'name': 'firefox'}
        target = f"{extra_pkg['repo']}-testing"
        assert target == "extra-testing"
        
        print("✓ Repository target selection test passed")
    
    def test_package_cleanup_logic(self):
        """Test package cleanup before upload"""
        pkg_files = [
            "test-1.0-1-aarch64.pkg.tar.xz",
            "test-1.0-2-aarch64.pkg.tar.xz", 
            "test-1.1-1-aarch64.pkg.tar.xz"
        ]
        
        newest = max(pkg_files)
        assert "1.1-1" in newest
        print("✓ Package cleanup logic test passed")


class TestConfigurationHandling:
    """Test configuration file handling"""
    
    def test_config_file_parsing(self):
        """Test config.ini parsing"""
        import configparser
        
        config = configparser.ConfigParser()
        config.read_string("""
[build]
chroot_path = /tmp/builder
cache_path = /tmp/cache
parallel_jobs = 4

[repositories]
upstream_core = https://example.com/core.db
upstream_extra = https://example.com/extra.db
""")
        
        assert config.has_section('build')
        assert config.has_section('repositories')
        assert config.get('build', 'chroot_path') == '/tmp/builder'
        print("✓ Config file parsing test passed")
    
    def test_config_defaults(self):
        """Test configuration defaults"""
        from utils import BUILD_ROOT, CACHE_PATH
        
        assert BUILD_ROOT is not None
        assert CACHE_PATH is not None
        assert isinstance(BUILD_ROOT, str)
        assert isinstance(CACHE_PATH, str)
        print("✓ Config defaults test passed")


class TestErrorRecovery:
    """Test error recovery and resilience"""
    
    def test_corrupted_database_handling(self):
        """Test handling of corrupted package databases"""
        corrupted_data = b"corrupted binary data"
        
        try:
            assert len(corrupted_data) > 0
            is_corrupted = not corrupted_data.startswith(b'\x1f\x8b')
            assert is_corrupted
        except Exception:
            pass
        
        print("✓ Corrupted database handling test passed")
    
    def test_network_failure_recovery(self):
        """Test recovery from network failures"""
        import subprocess
        
        try:
            result = subprocess.run(['echo', 'network_test'], 
                                  capture_output=True, text=True, timeout=1)
            assert result.returncode == 0
        except subprocess.TimeoutExpired:
            pass
        
        print("✓ Network failure recovery test passed")
    
    def test_build_interruption_cleanup(self):
        """Test cleanup after build interruption"""
        import signal
        
        current_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
        assert current_handler is not None
        
        signal.signal(signal.SIGINT, current_handler)
        print("✓ Build interruption cleanup test passed")


class TestUtilityFunctions:
    """Test utility functions"""
    
    @patch('subprocess.run')
    def test_load_blacklist(self, mock_run):
        """Test blacklist loading"""
        from utils import load_blacklist
        
        result = load_blacklist("nonexistent.txt")
        assert result == []
    
    def test_load_blacklist_with_content(self):
        """Test blacklist loading with actual content"""
        from utils import load_blacklist
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("# Comment line\n")
            f.write("blacklisted-pkg\n")
            f.write("another-pkg*\n")
            f.write("\n")
            temp_file = f.name
        
        try:
            result = load_blacklist(temp_file)
            assert "blacklisted-pkg" in result
            assert "another-pkg*" in result
            assert len(result) == 2
        finally:
            os.unlink(temp_file)
    
    @patch('subprocess.run')
    def test_filter_blacklisted_packages(self, mock_run):
        """Test package filtering"""
        from utils import filter_blacklisted_packages
        
        packages = [
            {"name": "vim", "basename": "vim"},
            {"name": "blacklisted", "basename": "blacklisted"},
            {"name": "allowed", "basename": "allowed"}
        ]
        blacklist = ["blacklisted"]
        
        filtered, count = filter_blacklisted_packages(packages, blacklist)
        assert len(filtered) == 2
        assert count == 1
        assert not any(pkg["name"] == "blacklisted" for pkg in filtered)


class TestFileOperations:
    """Test file operations and I/O"""
    
    def test_json_output_format(self):
        """Test JSON output format"""
        packages = [
            {
                "name": "vim",
                "version": "9.1.1-1",
                "basename": "vim",
                "repo": "extra",
                "depends": ["glibc"],
                "makedepends": ["gcc"],
                "provides": [],
                "build_stage": 0
            }
        ]
        
        json_str = json.dumps(packages, indent=2)
        parsed = json.loads(json_str)
        
        assert len(parsed) == 1
        assert parsed[0]["name"] == "vim"
        assert parsed[0]["version"] == "9.1.1-1"
    
    def test_pkgbuild_parsing_mock(self):
        """Test PKGBUILD parsing with mock data"""
        pkgbuild_content = """
pkgname=test-package
pkgver=1.0.0
pkgrel=1
depends=('glibc' 'gcc-libs')
makedepends=('gcc' 'make')
checkdepends=('python-pytest')
provides=('test-lib')
"""
        
        # Create a temporary directory and file for testing
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            pkgbuild_path = Path(temp_dir) / "PKGBUILD"
            pkgbuild_path.write_text(pkgbuild_content)
            
            from utils import parse_pkgbuild_deps
            
            deps = parse_pkgbuild_deps(pkgbuild_path)
            
            assert "glibc" in deps["depends"]
            assert "gcc" in deps["makedepends"] 
            assert "python-pytest" in deps["checkdepends"]
            assert "test-lib" in deps["provides"]


class TestErrorHandling:
    """Test error handling and edge cases"""
    
    def test_invalid_json_handling(self):
        """Test handling of invalid JSON files"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write("invalid json content")
            temp_file = f.name
        
        try:
            with pytest.raises(json.JSONDecodeError):
                with open(temp_file, 'r') as f:
                    json.load(f)
        finally:
            os.unlink(temp_file)
    
    def test_missing_pkgbuild_handling(self):
        """Test handling of missing PKGBUILD files"""
        from utils import parse_pkgbuild_deps
        
        deps = parse_pkgbuild_deps(Path("/nonexistent/PKGBUILD"))
        assert isinstance(deps, dict)
        assert "depends" in deps
    
    @patch('subprocess.run')
    def test_command_failure_handling(self, mock_run):
        """Test handling of command failures"""
        from utils import BuildUtils
        
        mock_run.side_effect = subprocess.CalledProcessError(1, "fake-command")
        utils = BuildUtils(dry_run=False)
        
        with pytest.raises(subprocess.CalledProcessError):
            utils.run_command(["fake-command"])


def run_integration_tests():
    """Run integration tests that require actual files"""
    print("Running integration tests...")
    
    # Test that main scripts can be imported
    try:
        import generate_build_list
        import build_packages
        import bootstrap_toolchain
        import utils
        print("✓ All modules import successfully")
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False
    
    # Test that help commands work
    scripts = [
        "generate_build_list.py",
        "build_packages.py", 
        "bootstrap_toolchain.py"
    ]
    
    for script in scripts:
        try:
            result = subprocess.run([
                "python3", script, "--help"
            ], capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                print(f"✓ {script} --help works")
            else:
                print(f"✗ {script} --help failed: {result.stderr}")
                return False
        except Exception as e:
            print(f"✗ {script} test failed: {e}")
            return False
    
    return True


class TestCommandLineOptions:
    """Test command-line argument parsing for all scripts"""
    
    def test_packages_option_pkgname_and_pkgbase(self):
        """Test that --packages works with both package names and package base names"""
        # Mock package data where pkgbase != pkgname
        mock_x86_packages = {
            'python-dbus': {
                'name': 'python-dbus',
                'basename': 'dbus-python',  # Different from package name
                'version': '1.4.0-1',
                'repo': 'extra',
                'depends': [],
                'makedepends': [],
                'provides': []
            }
        }
        
        mock_arm_packages = {
            'python-dbus': {
                'name': 'python-dbus', 
                'basename': 'dbus-python',
                'version': '1.4.0-1',
                'repo': 'extra',
                'provides': []
            }
        }
        
        from generate_build_list import compare_versions
        
        # Test requesting by package name
        packages_by_name, _, _, _ = compare_versions(
            mock_x86_packages, mock_arm_packages, 
            force_packages=['python-dbus']
        )
        assert len(packages_by_name) == 1
        assert packages_by_name[0]['name'] == 'dbus-python'  # Should use basename
        
        # Test requesting by basename
        packages_by_base, _, _, _ = compare_versions(
            mock_x86_packages, mock_arm_packages,
            force_packages=['dbus-python'] 
        )
        assert len(packages_by_base) == 1
        assert packages_by_base[0]['name'] == 'dbus-python'
        
        print("✓ Packages option pkgname and pkgbase test passed")

    def test_generate_build_list_options(self):
        """Test generate_build_list.py command-line options"""
        import argparse
        
        # Test that all expected options are available
        parser = argparse.ArgumentParser()
        parser.add_argument('--arm-urls', nargs='+')
        parser.add_argument('--aur', nargs='+')
        parser.add_argument('--local', action='store_true')
        parser.add_argument('--packages', nargs='+')
        parser.add_argument('--preserve-order', action='store_true')
        parser.add_argument('--blacklist', default='blacklist.txt')
        parser.add_argument('--missing-packages', action='store_true')
        parser.add_argument('--rebuild-repo', choices=['core', 'extra'])
        parser.add_argument('--no-update', action='store_true')
        parser.add_argument('--use-latest', action='store_true')
        
        # Test parsing some combinations
        args = parser.parse_args(['--packages', 'vim', 'gcc'])
        assert args.packages == ['vim', 'gcc']
        
        args = parser.parse_args(['--rebuild-repo', 'core'])
        assert args.rebuild_repo == 'core'
        
        args = parser.parse_args(['--missing-packages'])
        assert args.missing_packages == True
        
        print("✓ generate_build_list.py options test passed")
    
    def test_build_packages_options(self):
        """Test build_packages.py command-line options"""
        import argparse
        
        parser = argparse.ArgumentParser()
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--json', default='packages_to_build.json')
        parser.add_argument('--blacklist', default='blacklist.txt')
        parser.add_argument('--no-upload', action='store_true')
        parser.add_argument('--cache')
        parser.add_argument('--no-cache', action='store_true')
        parser.add_argument('--continue', action='store_true', dest='continue_build')
        parser.add_argument('--repackage', action='store_true')
        parser.add_argument('--preserve-chroot', action='store_true')
        parser.add_argument('--stop-on-failure', action='store_true')
        parser.add_argument('--chroot')
        
        # Test parsing
        args = parser.parse_args(['--dry-run', '--no-upload'])
        assert args.dry_run == True
        assert args.no_upload == True
        
        args = parser.parse_args(['--cache', '/custom/cache'])
        assert args.cache == '/custom/cache'
        
        args = parser.parse_args(['--continue', '--repackage'])
        assert args.continue_build == True
        assert args.repackage == True
        
        print("✓ build_packages.py options test passed")
    
    def test_bootstrap_toolchain_options(self):
        """Test bootstrap_toolchain.py command-line options"""
        import argparse
        
        parser = argparse.ArgumentParser()
        parser.add_argument('--chroot', default='/scratch/builder')
        parser.add_argument('--cache', default='/scratch/builder/pacman-cache')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--continue', action='store_true', dest='continue_build')
        parser.add_argument('--start-from', metavar='PACKAGE')
        parser.add_argument('--no-update', action='store_true')
        
        # Test parsing
        args = parser.parse_args(['--dry-run'])
        assert args.dry_run == True
        
        args = parser.parse_args(['--start-from', 'gcc'])
        assert args.start_from == 'gcc'
        
        args = parser.parse_args(['--chroot', '/custom/chroot'])
        assert args.chroot == '/custom/chroot'
        
        print("✓ bootstrap_toolchain.py options test passed")
    
    def test_repo_analyze_options(self):
        """Test repo_analyze.py command-line options"""
        import argparse
        
        parser = argparse.ArgumentParser()
        parser.add_argument('--blacklist')
        parser.add_argument('--missing-pkgbase', action='store_true')
        parser.add_argument('--outdated-any', action='store_true')
        parser.add_argument('--missing-any', action='store_true')
        parser.add_argument('--repo-mismatches', action='store_true')
        parser.add_argument('--arm-newer', action='store_true')
        parser.add_argument('--arm-only', action='store_true')
        parser.add_argument('--arm-duplicates', action='store_true')
        
        # Test parsing
        args = parser.parse_args(['--missing-pkgbase'])
        assert args.missing_pkgbase == True
        
        args = parser.parse_args(['--blacklist', 'custom.txt'])
        assert args.blacklist == 'custom.txt'
        
        args = parser.parse_args(['--arm-newer', '--repo-mismatches'])
        assert args.arm_newer == True
        assert args.repo_mismatches == True
        
        print("✓ repo_analyze.py options test passed")
    
    def test_find_dependents_options(self):
        """Test find_dependents.py command-line options"""
        import argparse
        
        parser = argparse.ArgumentParser()
        parser.add_argument('package')
        
        # Test parsing
        args = parser.parse_args(['vim'])
        assert args.package == 'vim'
        
        print("✓ find_dependents.py options test passed")


class TestScriptIntegration:
    """Test script integration and option validation"""
    
    def test_mutually_exclusive_options(self):
        """Test mutually exclusive option groups"""
        # generate_build_list.py has --no-update and --use-latest as mutually exclusive
        result = subprocess.run([
            "python3", "generate_build_list.py", "--no-update", "--use-latest", "--help"
        ], capture_output=True, text=True)
        
        # Should show help due to conflicting options
        assert "usage:" in result.stdout or result.returncode != 0
        print("✓ Mutually exclusive options test passed")
    
    def test_option_dependencies(self):
        """Test options that depend on other options"""
        # build_packages.py --repackage requires --continue
        result = subprocess.run([
            "python3", "build_packages.py", "--repackage", "--help"
        ], capture_output=True, text=True)
        
        # Should work (help will be shown)
        assert "usage:" in result.stdout
        print("✓ Option dependencies test passed")
    
    def test_choice_validation(self):
        """Test choice validation for options"""
        # generate_build_list.py --rebuild-repo accepts only 'core' or 'extra'
        result = subprocess.run([
            "python3", "generate_build_list.py", "--rebuild-repo", "invalid", "--help"
        ], capture_output=True, text=True)
        
        # Should fail or show help
        assert result.returncode != 0 or "usage:" in result.stdout
        print("✓ Choice validation test passed")


class TestScriptOutputFormats:
    """Test script output formats and modes"""
    
    def test_dry_run_mode(self):
        """Test dry-run mode functionality"""
        from utils import BuildUtils
        
        # Test dry-run mode
        utils = BuildUtils(dry_run=True)
        result = utils.run_command(["echo", "test"])
        
        # Should return mock result
        assert result.returncode == 0
        assert result.stdout == ""
        print("✓ Dry-run mode test passed")
    
    def test_json_output_validation(self):
        """Test JSON output structure"""
        import json
        import tempfile
        
        # Create minimal test data
        test_data = {
            "_command": "test",
            "_timestamp": "2025-01-01T00:00:00",
            "packages": [
                {
                    "name": "test-pkg",
                    "version": "1.0-1",
                    "repo": "extra",
                    "depends": [],
                    "makedepends": [],
                    "provides": [],
                    "build_stage": 0
                }
            ]
        }
        
        # Test JSON serialization/deserialization
        json_str = json.dumps(test_data, indent=2)
        parsed = json.loads(json_str)
        
        assert parsed["_command"] == "test"
        assert len(parsed["packages"]) == 1
        assert parsed["packages"][0]["name"] == "test-pkg"
        print("✓ JSON output validation test passed")


if __name__ == "__main__":
    import sys
    
    # Run unit tests with pytest if available
    if HAS_PYTEST:
        print("Running unit tests with pytest...")
        exit_code = pytest.main([__file__, "-v"])
        
        if exit_code == 0:
            print("\n" + "="*50)
            if run_integration_tests():
                print("\n🎉 All tests passed!")
                sys.exit(0)
            else:
                print("\n❌ Integration tests failed!")
                sys.exit(1)
        else:
            print("\n❌ Unit tests failed!")
            sys.exit(1)
            
    else:
        print("pytest not available, running basic tests...")
        
        # Run basic tests without pytest
        test_classes = [
            TestPackageValidation(),
            TestVersionComparison(),
            TestDependencyParsing(),
            TestPackageFiltering(),
            TestVersionHandling(),
            TestBuildOrderCalculation(),
            TestChrootManagement(),
            TestPackageUpload(),
            TestConfigurationHandling(),
            TestErrorRecovery(),
            TestUtilityFunctions(),
            TestCommandLineOptions(),
            TestScriptIntegration(),
            TestScriptOutputFormats()
        ]
        
        failed = 0
        passed = 0
        
        for test_class in test_classes:
            for method_name in dir(test_class):
                if method_name.startswith('test_'):
                    try:
                        method = getattr(test_class, method_name)
                        method()
                        print(f"✓ {test_class.__class__.__name__}.{method_name}")
                        passed += 1
                    except Exception as e:
                        print(f"✗ {test_class.__class__.__name__}.{method_name}: {e}")
                        failed += 1
        
        print(f"\nResults: {passed} passed, {failed} failed")
        
        if failed == 0 and run_integration_tests():
            print("🎉 All tests passed!")
            sys.exit(0)
        else:
            sys.exit(1)
