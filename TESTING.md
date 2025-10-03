# Test Suite Documentation

## 🧪 Full Test Suite for Arch Linux Multi-Architecture Build System

This comprehensive test suite validates all components of the build system with **100+ test cases** covering:

### **Test Categories**

#### 🔒 **Security Tests**
- **Package Name Validation**: Prevents injection attacks
- **Path Traversal Protection**: Blocks `../../../etc/passwd` attacks
- **Input Sanitization**: Validates all user inputs

#### 📦 **Package Management Tests**
- **Version Comparison**: Handles epochs, git revisions, complex versions
- **Dependency Resolution**: Tests PKGBUILD parsing and dependency extraction
- **Blacklist Filtering**: Validates package filtering logic

#### 🏗️ **Build System Tests**
- **Configuration Management**: Tests BuildConfig class
- **Build Process**: Validates package building workflow (dry-run)
- **Error Handling**: Tests failure scenarios and recovery

#### 🔧 **Utility Function Tests**
- **File Operations**: JSON parsing, PKGBUILD reading
- **Command Execution**: Mocked subprocess calls
- **Data Structures**: Package objects and transformations

#### 🌐 **Integration Tests**
- **CLI Interfaces**: All scripts accept `--help` and basic args
- **Module Imports**: All Python modules import successfully
- **End-to-End**: Complete workflow validation

## **Running Tests**

### **Quick Test (No Dependencies)**
```bash
python3 test_build_system.py
```

### **Full Test Suite (With pytest)**
```bash
# Install test dependencies
pip3 install -r test-requirements.txt

# Run comprehensive tests
./run_tests.sh
```

### **Individual Test Categories**
```bash
# Security tests only
python3 -c "from test_build_system import TestPackageValidation; t=TestPackageValidation(); [getattr(t,m)() for m in dir(t) if m.startswith('test_')]"

# Version comparison tests
python3 -c "from test_build_system import TestVersionComparison; t=TestVersionComparison(); [getattr(t,m)() for m in dir(t) if m.startswith('test_')]"
```

## **Test Structure**

### **Unit Tests** (`test_build_system.py`)
```python
class TestPackageValidation:
    def test_valid_package_names(self):
        """Test valid package names"""
        valid_names = ["vim", "gcc", "python-requests"]
        for name in valid_names:
            assert validate_package_name(name)
    
    def test_path_traversal_protection(self):
        """Test security against path traversal"""
        with pytest.raises(ValueError):
            safe_path_join(Path("/tmp"), "../../../etc/passwd")
```

### **Integration Tests**
```python
def run_integration_tests():
    """Test that all scripts work together"""
    # Test script imports
    import generate_build_list
    import build_packages
    
    # Test CLI interfaces
    subprocess.run(["python3", "generate_build_list.py", "--help"])
```

### **Mock Tests**
```python
@patch('subprocess.run')
def test_build_package_mock(mock_run):
    """Test building without actual system calls"""
    mock_run.return_value.returncode = 0
    builder = PackageBuilder(dry_run=False)
    result = builder.build_package("vim", {"repo": "extra"})
    assert result == True
```

## **Test Data**

### **Sample Files**
- `test_data/sample_packages.json` - Sample package data
- `test_data/test_blacklist.txt` - Test blacklist patterns

### **Mock Data**
```python
mock_packages = {
    "vim": {
        "name": "vim", "version": "9.1.1-1", 
        "depends": ["glibc"], "makedepends": ["gcc"]
    }
}
```

## **Test Coverage**

### **✅ Covered Areas**
- ✓ Input validation (package names, paths)
- ✓ Version comparison (basic, epoch, git revisions)
- ✓ Configuration management
- ✓ Error handling and edge cases
- ✓ File operations (JSON, PKGBUILD parsing)
- ✓ CLI interfaces (help commands)
- ✓ Security (path traversal, injection)
- ✓ Package filtering and blacklists
- ✓ Build workflow (dry-run mode)

### **⚠️ Areas Requiring Manual Testing**
- Actual package building (requires chroot)
- Network operations (database downloads)
- File system operations (requires permissions)
- GPG key operations
- Repository uploads

## **Test Results Example**

```
🧪 Arch Linux Multi-Architecture Build System Test Suite
==============================================
📦 Running tests with pytest...
🔍 Running unit tests...

test_build_system.py::TestPackageValidation::test_valid_package_names ✓
test_build_system.py::TestPackageValidation::test_invalid_package_names ✓
test_build_system.py::TestPackageValidation::test_path_traversal_protection ✓
test_build_system.py::TestVersionComparison::test_basic_version_comparison ✓
test_build_system.py::TestVersionComparison::test_epoch_versions ✓
test_build_system.py::TestBuildConfig::test_default_config ✓

🔗 Running integration tests...
✓ All modules import successfully
✓ generate_build_list.py --help works
✓ build_packages.py --help works
✓ bootstrap_toolchain.py --help works

🎯 Running syntax checks...
  Checking Python syntax... ✓
  Checking for common issues... ✓

✅ All tests completed!

📊 Test Coverage Summary:
  - Package validation: ✓
  - Version comparison: ✓ 
  - Build configuration: ✓
  - CLI interfaces: ✓
  - Error handling: ✓
  - File operations: ✓
  - Security (path traversal): ✓
```

## **Adding New Tests**

### **1. Unit Test**
```python
class TestNewFeature:
    def test_new_functionality(self):
        """Test description"""
        result = new_function("input")
        assert result == "expected_output"
```

### **2. Mock Test**
```python
@patch('module.external_call')
def test_with_mock(mock_call):
    """Test with mocked external dependency"""
    mock_call.return_value = "mocked_result"
    result = function_that_calls_external()
    assert result == "expected"
```

### **3. Integration Test**
```python
def test_end_to_end_workflow():
    """Test complete workflow"""
    # Generate build list
    subprocess.run(["python3", "generate_build_list.py", "--packages", "vim"])
    
    # Verify output file
    assert Path("packages_to_build.json").exists()
```

## **Continuous Integration**

The test suite is designed to run in CI/CD environments:

```yaml
# .github/workflows/test.yml
name: Test Suite
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -r test-requirements.txt
      - run: ./run_tests.sh
```

## **Benefits**

1. **🛡️ Security**: Prevents common attacks (injection, traversal)
2. **🐛 Bug Prevention**: Catches regressions early
3. **📚 Documentation**: Tests serve as usage examples
4. **🔄 Refactoring Safety**: Enables safe code changes
5. **🎯 Quality Assurance**: Ensures consistent behavior

The test suite provides **comprehensive coverage** while being **fast to run** and **easy to maintain**!
