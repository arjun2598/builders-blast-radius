"""Build step: unpack sqlglot at the pinned commit into /opt/sqlglot."""
import io, os, shutil, sys, tarfile, urllib.request

sha = sys.argv[1]
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=180).read()
tarfile.open(fileobj=io.BytesIO(data)).extractall("/opt", filter="data")
shutil.move(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
for junk in ("posts", "docs", ".github"):
    shutil.rmtree(f"/opt/sqlglot/{junk}", ignore_errors=True)
if os.path.exists("/opt/sqlglot/CHANGELOG.md"):
    os.remove("/opt/sqlglot/CHANGELOG.md")
