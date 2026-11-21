"""Putting the app together: what it refuses to start without, and who may call what."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import make_client
from promin.app import create_app
from promin.config import Settings


def test_app_refuses_to_start_without_token(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(data_dir=tmp_path, token=""))
    with pytest.raises(RuntimeError):
        create_app(Settings(data_dir=tmp_path, token="short"))


def test_every_route_names_who_may_call_it():
    """Routes are spread over modules, so one added without an access level would be open."""
    from fastapi.routing import APIRoute

    from promin.routes import ROUTERS
    from promin.routes.deps import require_auth

    def guarded(dependant) -> bool:
        return dependant.call is require_auth or any(guarded(d) for d in dependant.dependencies)

    # Read from the routers themselves: how an app keeps included routers differs between
    # FastAPI versions.
    routes = [r for router in ROUTERS for r in router.routes if isinstance(r, APIRoute)]
    assert len(routes) > 30
    open_routes = {r.path for r in routes if not guarded(r.dependant)}
    assert open_routes == {
        "/login", "/logout", "/tv", "/api/pair/start", "/api/pair/status",
        "/c/{token}", "/api/links/redeem",
    }


def test_pages_find_their_styles_and_scripts(tmp_path):
    """The styles and scripts live in static/, and every page names them with a version."""
    from promin.config import TEMPLATES_DIR

    def check(client, template: str, path: str) -> None:
        source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
        assert "<style" not in source and "<script>" not in source, template
        text = client.get(path).text
        assert "{{" not in text, template
        assets = re.findall(r'(?:href|src)="(/static/[^"]+\.(?:css|js))"', text)
        assert len(assets) == source.count("/static/{{v}}/") > 0, template
        for asset in assets:
            assert re.match(r"/static/v-[0-9a-f]{12}/", asset), asset
            assert client.get(asset).status_code == 200, asset

    with make_client(tmp_path) as client:
        check(client, "index.html", "/")
        check(client, "settings.html", "/settings")
        check(client, "users.html", "/users")
        check(client, "tv.html", "/tv")
        check(client, "tv.html", "/c/" + "a" * 32)
        check(client, "pair.html", "/pair?t=" + "a" * 24)
    with make_client(tmp_path, login=False) as anon:
        check(anon, "login.html", "/login")


def test_script_modules_import_what_is_exported():
    """Every import in the page scripts names a file that exists and something it exports."""
    from promin.config import STATIC_DIR

    scripts = list((STATIC_DIR / "js").rglob("*.js"))
    exports = {
        path: set(re.findall(r"^export (?:function|var) (\w+)", path.read_text(encoding="utf-8"), re.M))
        for path in scripts
    }
    imports = 0
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        # An import may be wrapped over several lines.
        found = re.findall(r"^import \{([^}]+)\} from '(\.{1,2}/[\w./]+)';", text, re.M)
        assert len(found) == len(re.findall(r"^import ", text, re.M)), "%s has an import of another shape" % path.name
        for names, source in found:
            target = (path.parent / source).resolve()
            assert target in exports, "%s imports from a missing %s" % (path.name, source)
            missing = set(re.findall(r"\w+", names)) - exports[target]
            assert not missing, "%s imports %s that %s does not export" % (path.name, missing, source)
            imports += 1
    assert imports > 20


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
@pytest.mark.parametrize("script", ["index/main.js", "settings.js", "users.js"])
def test_scripts_load(script):
    """The module scripts load in a real JavaScript engine: the syntax is right, the imports
    link, and no module uses another one before it has run."""
    from promin.config import STATIC_DIR

    result = subprocess.run(
        ["node", str(Path(__file__).with_name("load_scripts.mjs")), str(STATIC_DIR / "js" / script)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0 and result.stdout.strip() == "loaded", result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
@pytest.mark.parametrize("script", ["tv.js", "pair.js", "nomodule.js"])
def test_plain_scripts_parse(script):
    from promin.config import STATIC_DIR

    result = subprocess.run(
        ["node", "--check", str(STATIC_DIR / "js" / script)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_projector_page_behaves():
    """What tv.js does (the remote, errors, reconnecting, pairing, the projector check),
    checked in a stand-in browser: see tv_page.test.mjs."""
    result = subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("tv_page.test.mjs"))],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^. pass [1-9]", result.stdout, re.M), result.stdout


def test_projector_script_uses_no_newer_language_features():
    """tv.js has to run in the old browser of a projector, which stops at the first piece
    of syntax it does not know."""
    from promin.config import STATIC_DIR

    source = (STATIC_DIR / "js" / "tv.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    code = re.sub(r"'(?:\\.|[^'\\])*'", "''", code)  # what is inside a string is not syntax
    for newer in (r"=>", r"`", r"\b(?:let|const|class|async|await)\s", r"\.\.\.", r"\?\.", r"\?\?"):
        found = re.search(newer, code)
        assert not found, "tv.js uses %r" % (found.group(0) if found else "")
