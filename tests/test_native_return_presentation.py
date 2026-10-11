from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.provisioning.native_return import native_return_shell, native_return_style


def request(path, host="bridge.localhost:17871", query=b""):
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query,
                    "headers": [(b"host", host.encode())], "scheme": "http", "server": ("bridge.localhost", 17871)})


def test_native_return_preserves_safe_navigation_with_shared_presentation():
    reply = native_return_shell(request("/provisioning/native-return"), "a" * 32)
    html = reply.body.decode()
    assert "Conversation Analytics" in html and 'aria-live="polite"' in html
    assert '/provisioning/native-return.css' in html and '/provisioning/native-tokens.css' in html
    assert 'id="native-return-retry" type="button" hidden' in html
    assert 'id="native-return-focus" type="button" hidden' in html
    policy = reply.headers["content-security-policy"]
    assert "style-src 'self'" in policy and "script-src 'self'" in policy
    assert "unsafe-inline" not in policy and "https:" not in policy
    assert 'set-cookie' not in reply.headers


@pytest.mark.parametrize("path", ["native-return.css", "native-tokens.css"])
def test_native_styles_are_explicit_packaged_public_resources(path):
    reply = native_return_style(request("/provisioning/" + path))
    assert reply.headers["content-type"].startswith("text/css")
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert 'set-cookie' not in reply.headers
    assert b'--dipsy-' in reply.body
    policy = (Path(__file__).resolve().parents[1] / "packaging/runtime-files.json").read_text()
    assert '_internal/app/provisioning/native-return.css' in policy
    assert '_internal/app/provisioning/native-tokens.css' in policy


@pytest.mark.parametrize("path,host,query,code", [
    ("/provisioning/native-tokens.css", "evil.test", b"", 421),
    ("/provisioning/native-return.css", "bridge.localhost:17871", b"path=secret", 400),
    ("/provisioning/other.css", "bridge.localhost:17871", b"", 404),
])
def test_native_styles_do_not_expand_the_asset_boundary(path, host, query, code):
    with pytest.raises(HTTPException) as error:
        native_return_style(request(path, host, query))
    assert error.value.status_code == code


def test_pyinstaller_alias_keeps_generated_tokens_outside_the_source_tree(tmp_path):
    import ast
    from pathlib import PurePosixPath
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "packaging/pyinstaller/brain.spec").read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_source_relative", "_add_file"}]
    namespace = {"PurePosixPath": PurePosixPath, "Path": Path, "_PROJECT_ROOT": root,
                 "_INTERNAL_PREFIX": "_internal/", "CONF": {"workpath": str(tmp_path)}}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "brain.spec", "exec"), namespace)
    entries = []
    namespace["_add_file"](entries, "_internal/app/provisioning/native-tokens.css")
    source, target = entries[0]
    assert PurePosixPath(target.replace("\\", "/")) == PurePosixPath("app/provisioning")
    assert Path(source).name == "native-tokens.css"
    assert Path(source).is_relative_to(tmp_path)
    assert Path(source).read_bytes() == (root / "frontend/src/theme/generated/static-tokens.css").read_bytes()


pytestmark = pytest.mark.ci_tier("fast")
