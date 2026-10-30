"""Test all module imports to catch circular dependencies."""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_import(module_name):
    """Try importing a module and report status."""
    try:
        __import__(module_name)
        print(f"✓ {module_name}")
        return True
    except Exception as e:
        print(f"✗ {module_name}: {e}")
        return False

if __name__ == '__main__':
    modules = [
        # Core
        'app',
        'web_worker',

        # Extensions
        'app.extensions',
        'app.helpers',
        'app.routes',

        # Store
        'webapp.store',
        'webapp.store_utils',
        'webapp.database',
        'webapp.tasks',

        # Routes
        'webapp.clash_routes',
        'webapp.fleet_routes',
        'webapp.statistics_routes',

        # Auth
        'webapp.console_auth',
        'webapp.youtube_auth',

        # Services
        'webapp.catalog',
        'webapp.clash',
        'webapp.fleet',
        'webapp.execution',
        'webapp.startup',

        # Utils
        'webapp.settings',
        'webapp.media',
        'webapp.disk_budget',
        'webapp.diagnostics',
        'webapp.proxy_health',
    ]

    print("Testing module imports...")
    print("=" * 60)

    results = [test_import(mod) for mod in modules]

    print("=" * 60)
    success = sum(results)
    total = len(results)
    print(f"\nResult: {success}/{total} modules imported successfully")

    if success < total:
        sys.exit(1)
