"""
Standalone subprocess entry point for the archive scraper.
Called by app.py via subprocess.run — prints a JSON dict to stdout.
Usage: python _scrape_worker.py <archive_url>
"""
import json
import sys

from scrape_archive import scrape_archive_timeline

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    result = scrape_archive_timeline(url) if url else {}
    print(json.dumps(result))
