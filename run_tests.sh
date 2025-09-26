#!/bin/bash
set -e

echo "🧪 Arch Linux AArch64 Build System Test Suite"
echo "=============================================="

# Check if pytest is available
if command -v pytest &> /dev/null; then
    echo "📦 Running tests with pytest..."
    
    # Run tests with coverage if available
    echo "🔍 Running unit tests..."
    if command -v pytest-cov &> /dev/null; then
        pytest test_build_system.py -v --cov=. --cov-report=term-missing
    else
        pytest test_build_system.py -v
    fi
    
    echo ""
    echo "🔗 Running integration tests..."
    python3 test_build_system.py
    
else
    echo "⚠️  pytest not found, running basic tests..."
    python3 test_build_system.py
fi

echo ""
echo "🎯 Running syntax checks..."

# Check Python syntax
echo "  Checking Python syntax..."
python3 -m py_compile *.py

# Check for common issues
echo "  Checking for common issues..."
if command -v flake8 &> /dev/null; then
    flake8 --select=E9,F63,F7,F82 *.py
else
    echo "    (flake8 not available, skipping style checks)"
fi

echo ""
echo "✅ All tests completed!"
echo ""
echo "📊 Test Coverage Summary:"
echo "  - Package validation: ✓"
echo "  - Version comparison: ✓" 
echo "  - Build configuration: ✓"
echo "  - CLI interfaces: ✓"
echo "  - Error handling: ✓"
echo "  - File operations: ✓"
echo "  - Security (path traversal): ✓"
