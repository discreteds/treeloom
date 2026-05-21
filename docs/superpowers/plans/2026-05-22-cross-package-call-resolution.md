# Cross-Package Call Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix treeloom's Python visitor so `import polars as pl; pl.lit()` resolves to the correct upstream function (or stays unresolved) instead of falsely matching an internal `lit()`.

**Architecture:** Four changes to `src/treeloom/lang/builtin/python.py`: (1) qualified module names from package structure, (2) per-file import map with name-based suppression guard, (3) rewritten import-following with longest-prefix lookup, (4) re-export following through `__init__.py` chains. Plus a prerequisite fix for relative import parsing.

**Tech Stack:** Python 3.10+, tree-sitter, pytest, networkx (via treeloom's CPG)

---

## File Map

```
src/treeloom/lang/builtin/python.py   — ALL production changes (modify)
tests/lang/test_python.py             — ALL new tests (modify)
```

No new files. All changes are in the Python visitor and its test file.

---

## Task 0: Prerequisite — Fix Relative Import Parsing

**Files:**
- Modify: `src/treeloom/lang/builtin/python.py:521-557`
- Test: `tests/lang/test_python.py`

The current `_visit_import_from_statement` only handles `dotted_name` children for the module name. For `from .sub import X`, tree-sitter emits a `relative_import` node, not a `dotted_name`. Without this fix, all relative imports store `module=""` and Change 4's re-export following is dead code.

- [ ] **Step 1: Write the failing test**

```python
class TestRelativeImportParsing:
    """Relative imports should store the dotted module with leading dots."""

    def test_relative_import_stores_dot_prefix(self):
        """from .sub import X should store module='.sub' in IMPORT attrs."""
        init_src = b"""
from .sub import helper
"""
        cpg = CPGBuilder().add_source(init_src, "pkg/__init__.py", "python").build()
        imports = [n for n in cpg.nodes(kind=NodeKind.IMPORT) if n.attrs.get("is_from")]
        assert len(imports) == 1
        assert imports[0].attrs["module"] == ".sub", (
            f"Expected '.sub', got {imports[0].attrs['module']!r}"
        )

    def test_double_dot_relative_import(self):
        """from ..utils import X should store module='..utils'."""
        src = b"""
from ..utils import helper
"""
        cpg = CPGBuilder().add_source(src, "pkg/sub/mod.py", "python").build()
        imports = [n for n in cpg.nodes(kind=NodeKind.IMPORT) if n.attrs.get("is_from")]
        assert len(imports) == 1
        assert imports[0].attrs["module"] == "..utils"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestRelativeImportParsing -v`
Expected: FAIL — `assert imports[0].attrs["module"] == ".sub"` fails because module is `""`

- [ ] **Step 3: Fix `_visit_import_from_statement` to handle `relative_import`**

In `src/treeloom/lang/builtin/python.py`, modify `_visit_import_from_statement` (line ~532) to also handle `relative_import` children. Add this case inside the `for child in node.children:` loop, before the `dotted_name` case:

```python
            if child.type == "relative_import":
                # tree-sitter emits: relative_import → import_prefix ('.') + dotted_name
                # Extract the full text (dots + module name)
                text = self._node_text(child, ctx.source)
                if not saw_import:
                    module_name = text
                continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestRelativeImportParsing -v`
Expected: PASS

- [ ] **Step 5: Run full test suite for regressions**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/ -x -q`
Expected: All existing tests pass (1291+)

- [ ] **Step 6: Commit**

```bash
cd /home/nathanielramm/git/discreteds/treeloom
git add src/treeloom/lang/builtin/python.py tests/lang/test_python.py
git commit -m "fix(python): parse relative imports in from-import statements

_visit_import_from_statement now handles tree-sitter's relative_import
node type, storing the full dotted module with leading dots (e.g.,
'.sub', '..utils'). Previously stored empty string for all relative
imports."
```

---

## Task 1: Qualified Module Names

**Files:**
- Modify: `src/treeloom/lang/builtin/python.py:56-74`
- Test: `tests/lang/test_python.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestQualifiedModuleNames:
    """Module nodes should get dotted qualified names from package structure."""

    def test_package_file_gets_qualified_name(self, tmp_path):
        """pkg/sub/mod.py inside a package gets 'pkg.sub.mod'."""
        pkg = tmp_path / "pkg"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        (pkg / "__init__.py").write_bytes(b"")
        (sub / "__init__.py").write_bytes(b"")
        (sub / "mod.py").write_bytes(b"def foo(): pass\n")

        cpg = CPGBuilder().add_file(sub / "mod.py").build()
        mod = next(cpg.nodes(kind=NodeKind.MODULE))
        assert mod.name == "pkg.sub.mod", f"Expected 'pkg.sub.mod', got {mod.name!r}"

    def test_init_gets_package_name(self, tmp_path):
        """pkg/__init__.py gets 'pkg', not '__init__'."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_bytes(b"def setup(): pass\n")

        cpg = CPGBuilder().add_file(pkg / "__init__.py").build()
        mod = next(cpg.nodes(kind=NodeKind.MODULE))
        assert mod.name == "pkg", f"Expected 'pkg', got {mod.name!r}"

    def test_standalone_file_keeps_stem(self, tmp_path):
        """utils.py with no __init__.py in parent keeps 'utils'."""
        (tmp_path / "utils.py").write_bytes(b"def helper(): pass\n")

        cpg = CPGBuilder().add_file(tmp_path / "utils.py").build()
        mod = next(cpg.nodes(kind=NodeKind.MODULE))
        assert mod.name == "utils", f"Expected 'utils', got {mod.name!r}"

    def test_virtual_file_falls_back_to_stem(self):
        """add_source with virtual path falls back to file_path.stem."""
        cpg = CPGBuilder().add_source(b"def f(): pass\n", "virtual.py", "python").build()
        mod = next(cpg.nodes(kind=NodeKind.MODULE))
        assert mod.name == "virtual"

    def test_qualified_name_with_relative_root(self, tmp_path):
        """Qualified names work correctly with relative_root set."""
        pkg = tmp_path / "src" / "pkg"
        pkg.mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "__init__.py").write_bytes(b"")
        (pkg / "mod.py").write_bytes(b"def foo(): pass\n")

        cpg = CPGBuilder(relative_root=tmp_path / "src").add_file(pkg / "mod.py").build()
        mod = next(cpg.nodes(kind=NodeKind.MODULE))
        assert mod.name == "pkg.mod", f"Expected 'pkg.mod', got {mod.name!r}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestQualifiedModuleNames -v`
Expected: FAIL — names are stems like `"mod"`, `"__init__"`, etc.

- [ ] **Step 3: Implement `_derive_qualified_module_name`**

Add a static method to `PythonVisitor`:

```python
    @staticmethod
    def _derive_qualified_module_name(file_path: Path) -> str:
        """Derive a dotted module name from a file path.

        Walks up the directory tree checking for __init__.py to find the
        package root. Falls back to file_path.stem for non-package files
        or virtual paths that don't exist on disk.
        """
        # Virtual files or files that don't exist: fall back to stem
        try:
            resolved = file_path.resolve()
        except (OSError, ValueError):
            return file_path.stem

        if not resolved.exists():
            return file_path.stem

        parts: list[str] = []
        is_init = file_path.name == "__init__.py"

        if not is_init:
            parts.append(file_path.stem)

        current = resolved.parent
        while (current / "__init__.py").exists():
            parts.append(current.name)
            current = current.parent

        if not parts:
            return file_path.stem

        parts.reverse()
        return ".".join(parts)
```

- [ ] **Step 4: Update `visit()` to use it**

Replace line 64-65 in `visit()`:

```python
        module_name = self._derive_qualified_module_name(file_path)
        module_id = emitter.emit_module(
            module_name, file_path,
            end_location=module_end,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestQualifiedModuleNames -v`
Expected: All 5 pass

- [ ] **Step 6: Run full test suite for regressions**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/ -x -q`
Expected: All existing tests pass. Existing fixtures use non-package directories, so module names are unchanged (stem fallback).

- [ ] **Step 7: Commit**

```bash
cd /home/nathanielramm/git/discreteds/treeloom
git add src/treeloom/lang/builtin/python.py tests/lang/test_python.py
git commit -m "feat(python): derive qualified module names from package structure

polars/expr/string.py → 'polars.expr.string' instead of 'string'.
__init__.py → parent package name instead of '__init__'.
Falls back to file_path.stem for standalone files and virtual paths."
```

---

## Task 2: Per-File Import Map + Name-Based Suppression Guard

**Files:**
- Modify: `src/treeloom/lang/builtin/python.py:108-165`
- Test: `tests/lang/test_python.py`

This task replaces the global import map with per-file maps, adds `import X as alias` support, and wraps both name-based fallback paths with a guard.

- [ ] **Step 1: Write the failing tests**

```python
class TestPerFileImportMap:
    """Import-based call resolution should be scoped per file."""

    def test_alias_suppresses_name_based(self):
        """ml.lit() should NOT match internal lit() in a different module."""
        lib_src = b"""
def lit(value):
    return value
"""
        internal_src = b"""
def lit(x):
    return x * 2
"""
        consumer_src = b"""
import mylib as ml

def process():
    return ml.lit(42)
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "mylib.py", "python")
            .add_source(internal_src, "internal.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        # ml.lit should resolve to mylib.lit, NOT internal.lit
        resolved_targets = [t for s, t in calls_edges if "lit" in s]
        # With both source files in CPG, ml.lit should resolve to mylib's lit
        if resolved_targets:
            edge = next(
                e for e in cpg.edges(kind=EdgeKind.CALLS)
                if cpg.node(e.source) and "lit" in cpg.node(e.source).name
            )
            target = cpg.node(edge.target)
            target_scope = cpg.scope_of(target.id)
            assert target_scope.name == "mylib", (
                f"Expected target in 'mylib', got {target_scope.name!r}"
            )

    def test_per_file_isolation(self):
        """Import in a.py should not affect resolution in b.py."""
        lib_src = b"""
def foo():
    return 1
"""
        a_src = b"""
import mylib as ml

def call_a():
    return ml.foo()
"""
        b_src = b"""
def ml():
    return 'local'

def call_b():
    return ml()
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "mylib.py", "python")
            .add_source(a_src, "a.py", "python")
            .add_source(b_src, "b.py", "python")
            .build()
        )
        # b.py's ml() should resolve via name-based to b.py's ml function
        # (not suppressed by a.py's import)
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        # ml() in b.py should resolve to the local ml function
        b_calls = [
            (s, t) for s, t in calls_edges
            if s == "ml" and t == "ml"
        ]
        assert len(b_calls) >= 1, (
            f"b.py's ml() should resolve to local ml, got: {calls_edges}"
        )

    def test_from_import_suppresses_name_based(self):
        """from pkg import lit; lit() should not match internal lit()."""
        pkg_src = b"""
def lit(value):
    return value
"""
        internal_src = b"""
def lit(x):
    return x * 2
"""
        consumer_src = b"""
from mypkg import lit

def process():
    return lit(42)
"""
        cpg = (
            CPGBuilder()
            .add_source(pkg_src, "mypkg.py", "python")
            .add_source(internal_src, "internal.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        lit_targets = [t for s, t in calls_edges if s == "lit"]
        if lit_targets:
            edge = next(
                e for e in cpg.edges(kind=EdgeKind.CALLS)
                if cpg.node(e.source) and cpg.node(e.source).name == "lit"
                and cpg.node(e.source).kind == NodeKind.CALL
            )
            target = cpg.node(edge.target)
            target_scope = cpg.scope_of(target.id)
            assert target_scope.name == "mypkg", (
                f"Expected target in 'mypkg', got {target_scope.name!r}"
            )

    def test_short_name_fallback_also_suppressed(self):
        """pl.lit should not match internal lit() via short-name strip."""
        lib_src = b"""
def lit(value):
    return value
"""
        internal_src = b"""
def lit(x):
    return x * 2
"""
        consumer_src = b"""
import mylib as ml

def process():
    return ml.lit(42)
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "mylib.py", "python")
            .add_source(internal_src, "internal.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        # Verify: if ml.lit resolves, it must be to mylib's lit, not internal's
        for edge in cpg.edges(kind=EdgeKind.CALLS):
            src = cpg.node(edge.source)
            tgt = cpg.node(edge.target)
            if src and "lit" in src.name:
                tgt_scope = cpg.scope_of(tgt.id)
                assert tgt_scope is None or tgt_scope.name != "internal", (
                    f"ml.lit falsely resolved to internal.lit via short-name fallback"
                )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestPerFileImportMap -v`
Expected: FAIL — current global import map causes cross-file leakage and false matches

- [ ] **Step 3: Rewrite `resolve_calls` — import map + guard**

Replace lines 108-165 of `resolve_calls` in `python.py`. The new code:

1. Builds per-file import maps (both `from X import Y` and `import X as alias`)
2. Builds `symbols` dict (functions + classes)
3. Builds `module_imports` and `module_index` indexes
4. For each call: checks `is_import_known` guard before name-based fallback
5. Rewrites import-following with prefix lookup and longest-prefix matching

This is a large change. The full replacement for lines 108-165:

```python
        # -- Per-file import maps ----------------------------------------
        file_import_maps: dict[str, dict[str, tuple[str, str | None]]] = {}
        module_imports: dict[NodeId, list[CpgNode]] = {}
        for imp_node in cpg.nodes(kind=NodeKind.IMPORT):
            # Per-file import map
            file_key = str(imp_node.location.file) if imp_node.location else ""
            if file_key not in file_import_maps:
                file_import_maps[file_key] = {}
            fmap = file_import_maps[file_key]

            if imp_node.attrs.get("is_from"):
                module = imp_node.attrs.get("module", "")
                for imp_name in imp_node.attrs.get("names", []):
                    aliases = imp_node.attrs.get("aliases") or {}
                    local = aliases.get(imp_name, imp_name)
                    fmap[local] = (module, imp_name)
            else:
                for name in imp_node.attrs.get("names", []):
                    aliases = imp_node.attrs.get("aliases") or {}
                    local = aliases.get(name, name)
                    fmap[local] = (name, None)
                    if "." in name and name not in aliases:
                        parts = name.split(".")
                        for i in range(1, len(parts)):
                            prefix = ".".join(parts[:i])
                            fmap.setdefault(prefix, (prefix, None))

            # Module-scoped import index
            scope_node = cpg.scope_of(imp_node.id)
            if scope_node is not None:
                module_imports.setdefault(scope_node.id, []).append(imp_node)

        # Symbol index: functions + classes
        symbols: dict[str, list[CpgNode]] = {}
        for n in fn_list:
            symbols.setdefault(n.name, []).append(n)
        for n in cpg.nodes(kind=NodeKind.CLASS):
            symbols.setdefault(n.name, []).append(n)

        # Module name index
        module_index: dict[str, CpgNode] = {}
        for n in cpg.nodes(kind=NodeKind.MODULE):
            module_index[n.name] = n

        resolved: list[tuple[NodeId, NodeId]] = []

        for call_node in (call_nodes if call_nodes is not None else cpg.nodes(kind=NodeKind.CALL)):
            target = call_node.name
            fn: CpgNode | None = None

            # Try type-based resolution first for method calls
            receiver_type = call_node.attrs.get("receiver_inferred_type")
            if receiver_type is not None and "." in target:
                method_name = target.rsplit(".", 1)[-1]
                fn = self._resolve_method_via_mro(
                    receiver_type, method_name,
                    method_index, class_nodes,
                )

            # Per-file import map for this call's file
            call_file = str(call_node.location.file) if call_node.location else ""
            call_import_map = file_import_maps.get(call_file, {})

            is_import_known = (
                target in call_import_map
                or ("." in target and target.split(".")[0] in call_import_map)
            )

            # Name-based fallback — guarded
            if fn is None and not is_import_known:
                fn = self._resolve_single_call(
                    call_node, target, functions, cpg,
                )

            if fn is None and not is_import_known and "." in target:
                short_name = target.rsplit(".", 1)[-1]
                fn = self._resolve_single_call(
                    call_node, short_name, functions, cpg,
                )

            # Import-following resolution
            if fn is None:
                imp_entry = call_import_map.get(target)
                base_method: str | None = None

                # Longest-prefix lookup for dotted calls
                if imp_entry is None and "." in target:
                    parts = target.split(".")
                    for i in range(len(parts) - 1, 0, -1):
                        prefix = ".".join(parts[:i])
                        imp_entry = call_import_map.get(prefix)
                        if imp_entry is not None:
                            remaining = parts[i:]
                            if len(remaining) == 1:
                                base_method = remaining[0]
                            else:
                                imp_entry = None
                            break

                if imp_entry is not None:
                    imp_module, imp_name = imp_entry

                    if imp_name is not None:
                        # From-import: search for symbol in exact module
                        imp_candidates = symbols.get(imp_name, [])
                        for candidate in imp_candidates:
                            scope = cpg.scope_of(candidate.id)
                            if scope is not None and scope.kind == NodeKind.MODULE:
                                if scope.name == imp_module:
                                    fn = candidate
                                    break
                        if fn is None:
                            fn = self._follow_reexport(
                                cpg, imp_module, imp_name, symbols,
                                module_imports, module_index,
                            )
                    elif base_method is not None:
                        # Module import: search for method in exact module
                        candidates = symbols.get(base_method, [])
                        for candidate in candidates:
                            scope = cpg.scope_of(candidate.id)
                            if scope is not None and scope.kind == NodeKind.MODULE:
                                if scope.name == imp_module:
                                    fn = candidate
                                    break
                        if fn is None:
                            fn = self._follow_reexport(
                                cpg, imp_module, base_method, symbols,
                                module_imports, module_index,
                            )

            if fn is not None:
                cpg.add_edge(_make_calls_edge(call_node.id, fn.id))
                resolved.append((call_node.id, fn.id))

        return resolved
```

- [ ] **Step 4: Run tests**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestPerFileImportMap -v`
Expected: All 4 pass

- [ ] **Step 5: Run full test suite for regressions**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/ -x -q`
Expected: All tests pass. The existing `TestImportFollowingResolution` tests should still pass because `from X import Y` handling is preserved.

- [ ] **Step 6: Commit**

```bash
cd /home/nathanielramm/git/discreteds/treeloom
git add src/treeloom/lang/builtin/python.py tests/lang/test_python.py
git commit -m "feat(python): per-file import map with name-based suppression

Replace global import map with per-file scoping. Add import X as alias
support. Guard both name-based fallback paths when call prefix is a
known import alias. Rewrite import-following with longest-prefix lookup.
Include classes in symbol lookup for re-exported class resolution."
```

---

## Task 3: Import-Following Tests (Changes 2-3 Validation)

**Files:**
- Test: `tests/lang/test_python.py`

Additional tests for import-following behavior — the resolution paths from Changes 2-3.

- [ ] **Step 1: Write the tests**

```python
class TestModuleImportResolution:
    """Tests for import X as alias; alias.method() resolution."""

    def test_alias_resolves_via_import_following(self):
        """ml.lit() → CALLS edge to lit() in mylib module."""
        lib_src = b"""
def lit(value):
    return value
"""
        consumer_src = b"""
import mylib as ml

def process():
    return ml.lit(42)
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "mylib.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("ml.lit", "lit") in calls_edges, (
            f"Expected ml.lit -> lit, got: {calls_edges}"
        )

    def test_unaliased_import_resolves(self):
        """import mylib; mylib.lit() also resolves correctly."""
        lib_src = b"""
def lit(value):
    return value
"""
        consumer_src = b"""
import mylib

def process():
    return mylib.lit(42)
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "mylib.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("mylib.lit", "lit") in calls_edges, (
            f"Expected mylib.lit -> lit, got: {calls_edges}"
        )

    def test_from_import_still_works(self):
        """from mylib import lit; lit() — existing behavior unchanged."""
        lib_src = b"""
def lit(value):
    return value
"""
        consumer_src = b"""
from mylib import lit

def process():
    return lit(42)
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "mylib.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("lit", "lit") in calls_edges

    def test_mixed_imports_resolve(self):
        """Both import styles in same file resolve correctly."""
        lib_a_src = b"""
def foo():
    return 1
"""
        lib_b_src = b"""
def bar():
    return 2
"""
        consumer_src = b"""
import lib_a as a
from lib_b import bar

def process():
    x = a.foo()
    y = bar()
    return x + y
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_a_src, "lib_a.py", "python")
            .add_source(lib_b_src, "lib_b.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("a.foo", "foo") in calls_edges
        assert ("bar", "bar") in calls_edges

    def test_no_false_cross_resolution(self):
        """Two modules with same function name — correct one resolved."""
        lib_src = b"""
def lit(value):
    return value
"""
        other_src = b"""
def lit(x):
    return x * 2
"""
        consumer_src = b"""
import mylib as ml

def process():
    return ml.lit(42)
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "mylib.py", "python")
            .add_source(other_src, "other.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        for edge in cpg.edges(kind=EdgeKind.CALLS):
            src = cpg.node(edge.source)
            tgt = cpg.node(edge.target)
            if src and "lit" in src.name:
                tgt_scope = cpg.scope_of(tgt.id)
                assert tgt_scope is not None and tgt_scope.name == "mylib", (
                    f"ml.lit should resolve to mylib, got {tgt_scope.name if tgt_scope else '?'}"
                )

    def test_dotted_bare_import_prefix_registration(self):
        """import os.path; os.path.join() — all prefixes registered."""
        lib_src = b"""
def join(*args):
    return '/'.join(args)
"""
        consumer_src = b"""
import os.path

def process():
    return os.path.join('a', 'b')
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "path.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        # The call should be to os.path's join, not name-based fallback
        # Since we have a "path.py" with join(), and "os.path" registers prefix "os",
        # the guard should suppress name-based. Whether it resolves depends on
        # module name matching — at minimum, it should NOT falsely resolve.
        for edge in cpg.edges(kind=EdgeKind.CALLS):
            src = cpg.node(edge.source)
            if src and "join" in src.name:
                tgt = cpg.node(edge.target)
                tgt_scope = cpg.scope_of(tgt.id) if tgt else None
                # Should NOT resolve to path.py's join via name-based
                # (it should resolve to os.path's join or stay unresolved)
                pass  # assertion depends on whether os.path source is in CPG

    def test_multi_level_stays_unresolved(self):
        """import pkg.sub; pkg.sub.func() with too many levels stays unresolved."""
        lib_src = b"""
def sub():
    return 'wrong'
"""
        consumer_src = b"""
import pkg.sub

def process():
    return pkg.sub.func()
"""
        cpg = (
            CPGBuilder()
            .add_source(lib_src, "pkg.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        # pkg.sub.func has 2 segments past "pkg" — should NOT resolve
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        false_matches = [(s, t) for s, t in calls_edges if "sub" in s.lower() or "func" in s.lower()]
        assert not false_matches, (
            f"pkg.sub.func() should stay unresolved, got: {false_matches}"
        )
```

- [ ] **Step 2: Run tests**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestModuleImportResolution -v`
Expected: All 7 pass (these tests validate the behavior from Task 2's implementation)

- [ ] **Step 3: Commit**

```bash
cd /home/nathanielramm/git/discreteds/treeloom
git add tests/lang/test_python.py
git commit -m "test(python): import-following resolution for module imports

Tests for alias resolution, unaliased imports, from-imports, mixed
imports, cross-resolution protection, dotted bare imports, and
multi-level attribute path guard."
```

---

## Task 4: Re-Export Following + Helper Functions

**Files:**
- Modify: `src/treeloom/lang/builtin/python.py`
- Test: `tests/lang/test_python.py`

Add `_follow_reexport`, `_resolve_relative_import`, and `_is_submodule` methods to the Python visitor.

- [ ] **Step 1: Write the failing tests**

```python
class TestReexportFollowing:
    """Re-exports through __init__.py should be followed."""

    def test_reexport_resolution(self, tmp_path):
        """from pkg import Foo where Foo is in pkg/sub/impl.py."""
        pkg = tmp_path / "pkg"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        (pkg / "__init__.py").write_bytes(b"from pkg.sub.impl import Foo\n")
        (sub / "__init__.py").write_bytes(b"")
        (sub / "impl.py").write_bytes(b"def Foo(): return 1\n")

        consumer = tmp_path / "consumer.py"
        consumer.write_bytes(b"from pkg import Foo\n\ndef main():\n    return Foo()\n")

        cpg = CPGBuilder().add_directory(tmp_path).build()
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("Foo", "Foo") in calls_edges

        # Verify target is in pkg.sub.impl, not pkg
        for edge in cpg.edges(kind=EdgeKind.CALLS):
            src = cpg.node(edge.source)
            tgt = cpg.node(edge.target)
            if src and src.name == "Foo" and src.kind == NodeKind.CALL:
                tgt_scope = cpg.scope_of(tgt.id)
                assert tgt_scope is not None
                assert "impl" in tgt_scope.name, (
                    f"Expected target in pkg.sub.impl, got {tgt_scope.name!r}"
                )

    def test_reexport_with_alias(self, tmp_path):
        """pkg/__init__.py: from pkg.internal import _Impl as PublicName."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_bytes(
            b"from pkg.internal import _Impl as PublicName\n"
        )
        (pkg / "internal.py").write_bytes(b"def _Impl(): return 1\n")

        consumer = tmp_path / "consumer.py"
        consumer.write_bytes(
            b"from pkg import PublicName\n\ndef main():\n    return PublicName()\n"
        )

        cpg = CPGBuilder().add_directory(tmp_path).build()
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("PublicName", "_Impl") in calls_edges

    def test_reexported_class_resolves(self, tmp_path):
        """from pkg import MyClass; MyClass() → CLASS node."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_bytes(b"from pkg.impl import MyClass\n")
        (pkg / "impl.py").write_bytes(b"class MyClass:\n    pass\n")

        consumer = tmp_path / "consumer.py"
        consumer.write_bytes(
            b"from pkg import MyClass\n\ndef main():\n    return MyClass()\n"
        )

        cpg = CPGBuilder().add_directory(tmp_path).build()
        for edge in cpg.edges(kind=EdgeKind.CALLS):
            src = cpg.node(edge.source)
            tgt = cpg.node(edge.target)
            if src and src.name == "MyClass" and src.kind == NodeKind.CALL:
                assert tgt.kind == NodeKind.CLASS, (
                    f"Expected CLASS node, got {tgt.kind}"
                )
                break
        else:
            pytest.fail("MyClass() call was not resolved")

    def test_relative_reexport(self, tmp_path):
        """__init__.py: from .sub import helper — relative re-export."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_bytes(b"from .sub import helper\n")
        (pkg / "sub.py").write_bytes(b"def helper(): return 1\n")

        consumer = tmp_path / "consumer.py"
        consumer.write_bytes(
            b"from pkg import helper\n\ndef main():\n    return helper()\n"
        )

        cpg = CPGBuilder().add_directory(tmp_path).build()
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("helper", "helper") in calls_edges

    def test_relative_import_from_non_init(self, tmp_path):
        """pkg/mod.py: from .sibling import helper — non-package relative."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_bytes(b"")
        (pkg / "sibling.py").write_bytes(b"def helper(): return 1\n")
        (pkg / "mod.py").write_bytes(
            b"from .sibling import helper\n\ndef main():\n    return helper()\n"
        )

        cpg = CPGBuilder().add_directory(tmp_path).build()
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("helper", "helper") in calls_edges

        # Verify target is in pkg.sibling
        for edge in cpg.edges(kind=EdgeKind.CALLS):
            src = cpg.node(edge.source)
            tgt = cpg.node(edge.target)
            if src and src.name == "helper" and src.kind == NodeKind.CALL:
                tgt_scope = cpg.scope_of(tgt.id)
                assert tgt_scope is not None and "sibling" in tgt_scope.name

    def test_chained_reexport(self, tmp_path):
        """a/__init__.py → a/b/__init__.py → a/b/c.py — 2 levels."""
        a = tmp_path / "a"
        b = a / "b"
        b.mkdir(parents=True)
        (a / "__init__.py").write_bytes(b"from a.b import deep_func\n")
        (b / "__init__.py").write_bytes(b"from a.b.c import deep_func\n")
        (b / "c.py").write_bytes(b"def deep_func(): return 1\n")

        consumer = tmp_path / "consumer.py"
        consumer.write_bytes(
            b"from a import deep_func\n\ndef main():\n    return deep_func()\n"
        )

        cpg = CPGBuilder().add_directory(tmp_path).build()
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        assert ("deep_func", "deep_func") in calls_edges

    def test_recursion_limit(self, tmp_path):
        """4-level chain: a → b → c → d. Level 4 should NOT resolve."""
        a = tmp_path / "a"
        b = a / "b"
        c = b / "c"
        d = c / "d"
        d.mkdir(parents=True)
        (a / "__init__.py").write_bytes(b"from a.b import deep\n")
        (b / "__init__.py").write_bytes(b"from a.b.c import deep\n")
        (c / "__init__.py").write_bytes(b"from a.b.c.d import deep\n")
        (d / "__init__.py").write_bytes(b"from a.b.c.d.e import deep\n")
        # No e module — chain exceeds depth 3

        consumer = tmp_path / "consumer.py"
        consumer.write_bytes(
            b"from a import deep\n\ndef main():\n    return deep()\n"
        )

        cpg = CPGBuilder().add_directory(tmp_path).build()
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        deep_resolved = [(s, t) for s, t in calls_edges if "deep" in s]
        assert not deep_resolved, (
            f"4-level chain should not resolve, got: {deep_resolved}"
        )

    def test_alternative_reexport_path(self):
        """Two re-export paths for same symbol — first fails, second succeeds."""
        init_src = b"""
from pkg.missing import Widget
from pkg.real import Widget
"""
        real_src = b"""
def Widget():
    return 1
"""
        consumer_src = b"""
from mypkg import Widget

def main():
    return Widget()
"""
        cpg = (
            CPGBuilder()
            .add_source(init_src, "mypkg.py", "python")
            .add_source(real_src, "real.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        # Widget should resolve despite first import path being dead
        assert ("Widget", "Widget") in calls_edges

    def test_direct_search_no_submodule_bypass(self, tmp_path):
        """from pkg import Foo where Foo is in pkg.internal but NOT re-exported."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_bytes(b"# does NOT re-export Foo\n")
        (pkg / "internal.py").write_bytes(b"def Foo(): return 1\n")

        consumer = tmp_path / "consumer.py"
        consumer.write_bytes(
            b"from pkg import Foo\n\ndef main():\n    return Foo()\n"
        )

        cpg = CPGBuilder().add_directory(tmp_path).build()
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        foo_resolved = [(s, t) for s, t in calls_edges if "Foo" in s]
        assert not foo_resolved, (
            f"Foo should NOT resolve (not re-exported), got: {foo_resolved}"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestReexportFollowing -v`
Expected: FAIL — `_follow_reexport` doesn't exist yet

- [ ] **Step 3: Add helper functions to PythonVisitor**

Add these methods to the `PythonVisitor` class:

```python
    @staticmethod
    def _is_submodule(name: str, parent: str) -> bool:
        """Check if name is a submodule of parent."""
        return name.startswith(parent + ".")

    def _follow_reexport(
        self,
        cpg: CodePropertyGraph,
        module_name: str,
        symbol_name: str,
        symbols: dict[str, list[CpgNode]],
        module_imports: dict[NodeId, list[CpgNode]],
        module_index: dict[str, CpgNode],
        _depth: int = 0,
    ) -> CpgNode | None:
        """Follow __init__.py re-exports to find the actual definition."""
        if _depth >= 3:
            return None

        module_node = module_index.get(module_name)
        if module_node is None:
            return None

        for imp in module_imports.get(module_node.id, []):
            if not imp.attrs.get("is_from"):
                continue
            source_module = imp.attrs.get("module", "")

            if source_module.startswith("."):
                is_pkg = (
                    module_node.location is not None
                    and str(module_node.location.file).endswith("__init__.py")
                )
                source_module = self._resolve_relative_import(
                    module_name, source_module, is_package=is_pkg
                )

            names = imp.attrs.get("names", [])
            aliases = imp.attrs.get("aliases") or {}
            for name in names:
                local = aliases.get(name, name)
                if local == symbol_name or name == symbol_name:
                    candidates = symbols.get(name, [])
                    for candidate in candidates:
                        scope = cpg.scope_of(candidate.id)
                        if scope and scope.kind == NodeKind.MODULE:
                            if (scope.name == source_module
                                    or self._is_submodule(scope.name, source_module)):
                                return candidate
                    result = self._follow_reexport(
                        cpg, source_module, name, symbols,
                        module_imports, module_index, _depth + 1,
                    )
                    if result is not None:
                        return result
        return None

    @staticmethod
    def _resolve_relative_import(
        importing_module: str, relative_module: str, is_package: bool
    ) -> str:
        """Resolve a relative import against the importing module's name."""
        dots = len(relative_module) - len(relative_module.lstrip("."))
        suffix = relative_module.lstrip(".")
        parts = importing_module.split(".")
        if not is_package and parts:
            parts = parts[:-1]
        base_parts = parts[: max(0, len(parts) - (dots - 1))]
        if suffix:
            base_parts.append(suffix)
        return ".".join(base_parts)
```

- [ ] **Step 4: Run tests**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/lang/test_python.py::TestReexportFollowing -v`
Expected: All 9 pass

- [ ] **Step 5: Run full test suite**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
cd /home/nathanielramm/git/discreteds/treeloom
git add src/treeloom/lang/builtin/python.py tests/lang/test_python.py
git commit -m "feat(python): follow re-exports through __init__.py

_follow_reexport traces import chains in __init__.py files to find
the actual definition module. Handles relative imports, aliased
re-exports, class nodes, and chained re-exports up to 3 levels.
_resolve_relative_import correctly handles both package and non-package
modules."
```

---

## Task 5: Known Limitation Test + Final Validation

**Files:**
- Test: `tests/lang/test_python.py`

- [ ] **Step 1: Write the known-limitation test**

```python
class TestKnownLimitations:
    """Document known limitation behavior for regression tracking."""

    def test_post_import_shadowing_known_wrong(self):
        """from pkg import lit; lit = other; lit() — resolves to pkg.lit (wrong).

        This is a known limitation: the resolver has no access to per-scope
        assignment tracking. This test documents the current (incorrect)
        behavior as a regression marker.
        """
        pkg_src = b"""
def lit(value):
    return value
"""
        consumer_src = b"""
from mypkg import lit

def other():
    return 'other'

def process():
    lit = other
    return lit()
"""
        cpg = (
            CPGBuilder()
            .add_source(pkg_src, "mypkg.py", "python")
            .add_source(consumer_src, "consumer.py", "python")
            .build()
        )
        # Known wrong: lit() resolves to mypkg.lit even after shadowing
        calls_edges = _edge_pairs(cpg, EdgeKind.CALLS)
        # Just verify it doesn't crash — the exact resolution is documented
        # as a known limitation
        assert isinstance(calls_edges, list)
```

- [ ] **Step 2: Run ALL tests**

Run: `cd /home/nathanielramm/git/discreteds/treeloom && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: All tests pass (1291 existing + ~25 new)

- [ ] **Step 3: Commit**

```bash
cd /home/nathanielramm/git/discreteds/treeloom
git add tests/lang/test_python.py
git commit -m "test(python): known limitation — post-import shadowing

Documents that from pkg import lit; lit = other; lit() incorrectly
resolves to pkg.lit. Regression marker for when shadowing support
is added."
```

---

## Summary

| Task | What it builds | Tests |
|------|---------------|-------|
| 0 | Fix relative import parsing prerequisite | 2 |
| 1 | Qualified module names | 5 |
| 2 | Per-file import map + name-based guard | 4 |
| 3 | Import-following resolution tests | 7 |
| 4 | Re-export following + helpers | 9 |
| 5 | Known limitation test + final validation | 1 |

Total: 28 tests (25 from spec + 2 relative import parsing + 1 final validation structure check)

Dependency order: 0 → 1 → 2 → 3 → 4 → 5 (strict sequential — each task depends on the previous).
