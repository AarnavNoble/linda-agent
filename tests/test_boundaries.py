"""Static boundary checks over the agent source: the rules the brief and plan promise, enforced.

  * the browser is only ever pointed at a URL once, in PortalSession.start; every other
    navigation is a click on visible UI;
  * no back doors: no __admin endpoints, no portal data dir / manifest, no portal DB, and no
    out-of-browser HTTP client talking to the portal;
  * no hardcoded fax IDs or fault-mode names in runtime code (the agent must detect state
    from the UI, not from knowing the sandbox).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "agent"
FAULT_MODES = ("download_503_once", "session_expire_on_route_once", "route_commit_then_503_once")
# chaos.py (if/when it exists) is the test-harness driver that injects faults on purpose.
HARNESS_FILES = {"chaos.py"}


def _files(include_harness: bool = True) -> list[Path]:
    fs = [p for p in AGENT.rglob("*") if p.is_file() and "__pycache__" not in p.parts
          and p.suffix in (".py", ".yaml", ".yml", ".json", ".txt", ".md")]
    return [p for p in fs if include_harness or p.name not in HARNESS_FILES]


def _py(include_harness: bool = False) -> list[Path]:
    return [p for p in _files(include_harness) if p.suffix == ".py"]


def _rel(p: Path) -> str:
    return str(p.relative_to(AGENT.parent))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing(node, parents, kind):
    while node in parents:
        node = parents[node]
        if isinstance(node, kind):
            return node
    return None


def test_agent_sources_exist():
    assert (AGENT / "browser.py").exists() and len(_py()) >= 10


def test_goto_only_in_portal_session_start():
    hits = []
    for p in _py(include_harness=True):
        tree = ast.parse(p.read_text())
        parents = _parents(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "goto":
                fn = _enclosing(node, parents, (ast.FunctionDef, ast.AsyncFunctionDef))
                cls = _enclosing(node, parents, ast.ClassDef)
                hits.append((_rel(p), cls.name if cls else None, fn.name if fn else None))
    assert hits == [("agent/browser.py", "PortalSession", "start")], hits


def test_goto_target_is_the_base_url_entry_point():
    src = (AGENT / "browser.py").read_text()
    calls = re.findall(r"\.goto\((.*)\)", src)
    assert len(calls) == 1 and "cfg.base_url" in calls[0], calls


@pytest.mark.parametrize("pattern", [
    r"\bgo_back\b", r"\bgo_forward\b", r"\.reload\(", r"\.set_content\(", r"\.route\(",
    r"\.request\.(get|post|fetch)\b", r"\bnew_cdp_session\b", r"\bstorage_state\b", r"add_cookies",
])
def test_no_url_or_session_shortcuts(pattern):
    """No history replay (stale CSRF forms), no network interception, no cookie/session reuse."""
    rx = re.compile(pattern)
    hits = [f"{_rel(p)}:{i}" for p in _py(True) for i, line in enumerate(p.read_text().splitlines(), 1)
            if rx.search(line)]
    assert not hits, hits


def test_no_out_of_browser_http_clients():
    banned = {"requests", "httpx", "urllib.request", "http.client", "aiohttp", "urllib3"}
    hits = []
    for p in _py(True):
        for node in ast.walk(ast.parse(p.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            hits += [f"{_rel(p)}: {n}" for n in names if n in banned]
    assert not hits, hits


def test_no_admin_endpoints():
    hits = [_rel(p) for p in _files() if "__admin" in p.read_text() or "admin_token" in p.read_text().lower()]
    assert not hits, hits


@pytest.mark.parametrize("needle", ["data/faxes", "manifest", "linda-mini", "reset_sandbox", "LINDA_ADMIN"])
def test_no_portal_internals_referenced(needle):
    hits = [_rel(p) for p in _files() if needle.lower() in p.read_text().lower()]
    assert not hits, hits


def _string_constants(p: Path):
    tree = ast.parse(p.read_text())
    parents = _parents(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, parents


def _is_schema_field_description(p: Path, node, parents) -> bool:
    """A pydantic Field(description=...) in the vision schema: model-facing prompt text, not logic."""
    if p.name != "schema.py" or p.parent.name != "extract":
        return False
    par = parents.get(node)
    return isinstance(par, ast.keyword) and par.arg == "description"


def test_no_hardcoded_fax_ids_in_runtime_code():
    rx = re.compile(r"\bFX-\d+\b", re.I)
    hits, allowed = [], []
    for p in _py():
        for node, parents in _string_constants(p):
            if rx.search(node.value):
                (allowed if _is_schema_field_description(p, node, parents) else hits).append(
                    f"{_rel(p)}:{node.lineno}: {node.value[:80]!r}")
    assert not hits, hits
    # The only tolerated occurrence is a format example in the vision schema's field description.
    assert len(allowed) <= 1, allowed


def test_no_fault_mode_names_in_runtime_code():
    hits = [f"{_rel(p)}: {m}" for p in _files(include_harness=False) for m in FAULT_MODES
            if m in p.read_text() or m.replace("_", "-") in p.read_text()]
    assert not hits, hits
    assert not [p for p in _py() if "LINDA_FAULT_MODE" in p.read_text()]


def test_sqlite_only_used_for_the_agents_own_checkpoint():
    users = sorted(_rel(p) for p in _py(True) if re.search(r"^\s*(import|from)\s+sqlite3", p.read_text(), re.M))
    assert users == ["agent/state.py"], users
    for p in _py(True):
        txt = p.read_text()
        assert "portal.sqlite3" not in txt and "LINDA_DB_PATH" not in txt, _rel(p)
        for m in re.finditer(r"\bStore\(([^)]*)\)", txt):
            assert "state_dir" in m.group(1) and "checkpoint.sqlite3" in m.group(1), f"{_rel(p)}: {m.group(0)}"
    connects = re.findall(r"sqlite3\.connect\(([^,)]*)", (AGENT / "state.py").read_text())
    assert connects == ["path"], connects


def test_extraction_reads_only_downloaded_files_not_previews():
    hits = [f"{_rel(p)}:{i}" for p in _py(True) for i, line in enumerate(p.read_text().splitlines(), 1)
            if "/preview" in line or "Open preview" in line]
    assert not hits, hits
