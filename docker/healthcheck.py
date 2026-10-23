"""Probe a small database-backed API through the actual HTTP server."""
import sys
from urllib.request import ProxyHandler, build_opener


def main():
    try:
        # Ignore outbound proxy settings for the container's own HTTP server.
        with build_opener(ProxyHandler({})).open(
                'http://127.0.0.1:5000/api/tasks?view=active&limit=1', timeout=5) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == '__main__':
    sys.exit(main())
