# Cross-Package Call Resolution for Aliased Imports

**Status:** APPROVED (post-adversarial-review)
**Date:** 2026-05-22

---

## Problem

treeloom's Python call resolution produces false positives when consumer
code calls functions in external packages via aliased imports.

Given:
```python
# consumer.py
import polars as pl

def pipeline():
    return pl.lit(42)
```

And an internal function also named `lit` elsewhere in the CPG, the
resolver matches `pl.lit` → internal `lit()` via name-based fallback.
The correct resolution would be `pl.lit` → `polars.lit` (if polars
source is in the CPG) or unresolved (if it isn't).

### Root Causes

1. **Module names are file stems, not qualified.** `polars/__init__.py`
   gets module name `"__init__"`, not `"polars"`. Import-following can't
   match `"polars"` against `"__init__"`.

2. **Name-based resolution runs before import-following for aliased
   calls.** `pl.lit` hits the name-based resolver first (both the
   primary attempt AND the short-name fallback), which strips the prefix
   and finds any function named `lit` — ignoring that `pl` is an import
   alias.

3. **Import map ignores `import X as alias` patterns.** Only
   `from X import Y` enters the import map. `import polars as pl`
   is silently dropped, so `pl` is never recognized as an import alias
   during resolution.

4. **Import map is global, not per-file.** An import in `a.py` can
   suppress name-based resolution for calls in `b.py` that don't have
   that import.

---

## Design

Four changes to `src/treeloom/lang/builtin/python.py`, all in the
Python visitor's `resolve_calls` method and `visit` method.

### Change 1: Qualified Module Names

**Where:** `visit()` method, line 65.

**Current:** `file_path.stem` → `"string"` for `polars/expr/string.py`

**New:** Walk up from the file, checking each parent for `__init__.py`.
Stop at the first parent without one. Construct a dotted name from the
relative path.

```
polars/expr/string.py     → "polars.expr.string"
polars/expr/__init__.py   → "polars.expr"
polars/__init__.py        → "polars"
utils.py (standalone)     → "utils"  (unchanged)
```

Add a `_derive_qualified_module_name(file_path: Path) -> str` static
method to the Python visitor.

**Two resolution strategies:**

1. **Real files (`add_file` / `add_directory`):** Walk up from the
   **absolute disk path** (before `relative_root` normalization),
   checking each directory for `__init__.py` on disk. Stop at the
   first parent without one. The builder passes normalized paths to
   `visit()`, so the method must resolve back to the absolute path
   for the filesystem walk (via `self._relative_root / file_path`
   if `relative_root` is set, otherwise `file_path.resolve()`).

2. **Virtual files (`add_source`):** The file path may not exist on
   disk. Fall back to `file_path.stem`. This means `add_source()` tests
   get simple module names (`"utils"`, `"caller"`), which is correct
   for the non-package test fixtures. Tests that need qualified names
   must use real temporary directories (via `tmp_path` fixtures) with
   actual `__init__.py` files.

**Edge cases:**
- Files outside packages (no `__init__.py` in parent) → `file_path.stem`
  as before. Existing tests pass unchanged.
- `__init__.py` itself → module name is the parent directory chain,
  not `"__init__"`.
- Namespace packages (PEP 420, no `__init__.py`) → get truncated names.
  Known limitation — noted in Out of Scope.

### Change 2: Per-File Import Map + Suppress Name-Based Fallback

**Where:** `resolve_calls()` method. The import map changes from a
global dict to a **per-file dict**, and a guard wraps BOTH name-based
fallback paths (primary at line ~136 AND short-name at lines ~141-145).

**Why per-file:** Python imports are file-scoped. `a.py`'s
`import mylib as ml` should not affect resolution of `ml.something()`
in `b.py` where `ml` might be a local variable. A global import map
causes cross-file alias leakage.

```python
# Import map is now per-file: {file_path_str: {local_alias: (module, name)}}
file_import_maps: dict[str, dict[str, tuple[str, str | None]]] = {}

for imp_node in cpg.nodes(kind=NodeKind.IMPORT):
    file_key = str(imp_node.location.file) if imp_node.location else ""
    if file_key not in file_import_maps:
        file_import_maps[file_key] = {}
    fmap = file_import_maps[file_key]
    # ... populate fmap (see Change 3 Part A for both import styles)
```

**Guard logic — wraps BOTH name-based fallback paths:**

```python
# Look up the import map for THIS call's file
call_file = str(call_node.location.file) if call_node.location else ""
call_import_map = file_import_maps.get(call_file, {})

is_import_known = (
    target in call_import_map                              # from pkg import lit; lit()
    or ("." in target and target.split(".")[0] in call_import_map)  # import pkg as pl; pl.lit()
)

# PRIMARY name-based — guarded
if fn is None and not is_import_known:
    fn = self._resolve_single_call(call_node, target, functions, cpg)

# SHORT-NAME fallback — also guarded (strips prefix, retries)
if fn is None and not is_import_known and "." in target:
    short_name = target.rsplit(".", 1)[-1]
    fn = self._resolve_single_call(call_node, short_name, functions, cpg)
```

Both paths must be guarded. Without this, the short-name path strips
`pl.` from `pl.lit`, finds internal `lit()`, and creates a false edge.

### Change 3: Populate Import Map + Rewrite Import-Following

**Where:** `resolve_calls()` method, both the per-file import map
population AND the import-following resolution (lines ~149-160).

**Part A — Import map population** (inside the per-file loop from
Change 2). Handle both `from X import Y` and `import X as alias`:

```python
if imp_node.attrs.get("is_from"):
    module = imp_node.attrs.get("module", "")
    for imp_name in imp_node.attrs.get("names", []):
        aliases = imp_node.attrs.get("aliases") or {}
        local = aliases.get(imp_name, imp_name)
        fmap[local] = (module, imp_name)
else:
    # import polars as pl → fmap["pl"] = ("polars", None)
    # import os.path     → fmap["os"] = ("os", None), fmap["os.path"] = ("os.path", None)
    for name in imp_node.attrs.get("names", []):
        aliases = imp_node.attrs.get("aliases") or {}
        local = aliases.get(name, name)
        fmap[local] = (name, None)  # None = module-level import
        # For dotted bare imports (import os.path), also register
        # each prefix so prefix lookup works for os.path.join()
        if "." in name and name not in aliases:
            parts = name.split(".")
            for i in range(1, len(parts)):
                prefix = ".".join(parts[:i])
                fmap.setdefault(prefix, (prefix, None))
```

**Part B — Rewrite import-following resolution.** Uses the per-file
`call_import_map` from Change 2. The current code at line 149 does
`if target in import_map` — this only matches exact keys. For module
imports, the key is the alias (`"pl"`) but the target is `"pl.lit"`.
The lookup must also check the prefix:

```python
# New import-following — uses per-file call_import_map
imp_entry = call_import_map.get(target)

# For dotted calls, also try prefix lookup (module imports)
if imp_entry is None and "." in target:
    prefix = target.split(".")[0]
    imp_entry = call_import_map.get(prefix)

if imp_entry is not None:
    imp_module, imp_name = imp_entry

    if imp_name is not None:
        # From-import: from X import Y → search for Y EXACTLY in module X
        # NOTE: direct search uses exact module match only. _is_submodule
        # check belongs in _follow_reexport — without this restriction,
        # symbols in submodules bypass the re-export gate.
        imp_candidates = symbols.get(imp_name, [])
        for candidate in imp_candidates:
            scope = cpg.scope_of(candidate.id)
            if scope is not None and scope.kind == NodeKind.MODULE:
                if scope.name == imp_module:
                    fn = candidate
                    break
        # If not found in exact module, try re-export following (Change 4)
        if fn is None:
            fn = self._follow_reexport(
                cpg, imp_module, imp_name, symbols,
                module_imports, module_index,
            )
    else:
        # Module import: import X as alias → call is alias.method
        method_part = target.split(".", 1)[1] if "." in target else target
        base_method = method_part.split(".")[0]  # "when" from "pl.when.then"
        candidates = symbols.get(base_method, [])
        for candidate in candidates:
            scope = cpg.scope_of(candidate.id)
            if scope is not None and scope.kind == NodeKind.MODULE:
                if scope.name == imp_module:
                    fn = candidate
                    break
        # If not found in exact module, try re-export following (Change 4)
        if fn is None:
            fn = self._follow_reexport(
                cpg, imp_module, base_method, symbols,
                module_imports, module_index,
            )
```

**`symbols` replaces `functions` for lookup:** The lookup dict must
include both `NodeKind.FUNCTION` and `NodeKind.CLASS` nodes, since
re-exported names are often classes (`DataFrame`, `Expr`):

```python
symbols: dict[str, list[CpgNode]] = {}
for n in fn_list:
    symbols.setdefault(n.name, []).append(n)
for n in cpg.nodes(kind=NodeKind.CLASS):
    symbols.setdefault(n.name, []).append(n)
```

Note: uses `dict.setdefault` instead of `defaultdict` to avoid adding
an import to `python.py`.

**Multi-level attribute paths:** For `import pkg.sub; pkg.sub.func()`,
the prefix lookup should use longest-prefix matching: try `"pkg.sub"`
before `"pkg"` in the import map. If the longest prefix matches and
leaves exactly one remaining segment (`"func"`), resolve that as the
method. If more than one segment remains (`"sub.func"` from a `"pkg"`
match), leave the call unresolved rather than falsely resolving to a
symbol named `"sub"`.

```python
# Longest-prefix import lookup for dotted calls
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
                imp_entry = None  # too many levels, stay unresolved
            break
```

**Helper functions:**

```python
def _is_submodule(name: str, parent: str) -> bool:
    """Check if name is a submodule of parent."""
    return name.startswith(parent + ".")
```

### Change 4: Follow Re-Exports Through `__init__.py`

**Prerequisite — fix relative import parsing.** The current
`_visit_import_from_statement` only captures `dotted_name` children
for the module name. For `from .sub import X`, tree-sitter emits a
`relative_import` node (containing `import_prefix` + `dotted_name`),
not a bare `dotted_name`. The visitor must be updated to handle
`relative_import` children by extracting the full text (dots + name)
as the module attr. Without this fix, all relative imports store
`module=""` and the re-export resolver never fires. Since
`__init__.py` re-exports almost universally use relative imports
(`from .dataframe.frame import DataFrame`), this is a hard prerequisite.

**Problem:** Most Python packages re-export their public API through
`__init__.py`. Changes 1-3 search for symbols scoped under the target
module but find no definition (only an import statement) and give up.

**New behavior:** When the resolver searches for a symbol in a module
scope and finds no definition, check if that module has an IMPORT node
that brings the symbol in from a submodule. If so, follow the chain.

**Pre-built indexes** (constructed in the same loop as the per-file
import map, to avoid per-call O(N) scans):

```python
# Module-scoped import index: module_node.id → [import_nodes_in_that_module]
module_imports: dict[NodeId, list[CpgNode]] = {}
for imp_node in cpg.nodes(kind=NodeKind.IMPORT):
    scope_node = cpg.scope_of(imp_node.id)
    if scope_node is not None:
        module_imports.setdefault(scope_node.id, []).append(imp_node)

# Module name index: module_name → module_node (avoids linear scan per lookup)
module_index: dict[str, CpgNode] = {}
for n in cpg.nodes(kind=NodeKind.MODULE):
    module_index[n.name] = n
```

**NOTE:** `module_imports` is keyed by `NodeId` (from `scope_node.id`),
not by `CpgNode`. The lookup in `_follow_reexport` uses
`module_imports.get(module_node.id, [])` — both are `NodeId`, so the
types match.

```python
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
    """Follow __init__.py re-exports to find the actual definition.

    Recursion limited to 3 levels to prevent infinite loops.
    """
    if _depth >= 3:
        return None

    module_node = module_index.get(module_name)
    if module_node is None:
        return None

    # Check imports scoped to this module — O(imports_in_module)
    for imp in module_imports.get(module_node.id, []):
        if not imp.attrs.get("is_from"):
            continue
        source_module = imp.attrs.get("module", "")

        # Resolve relative imports against the importing module's name
        if source_module.startswith("."):
            is_pkg = (
                module_node.location is not None
                and str(module_node.location.file).endswith("__init__.py")
            )
            source_module = _resolve_relative_import(
                module_name, source_module, is_package=is_pkg
            )

        names = imp.attrs.get("names", [])
        aliases = imp.attrs.get("aliases") or {}
        for name in names:
            local = aliases.get(name, name)
            if local == symbol_name or name == symbol_name:
                # Search for the symbol in the source module
                candidates = symbols.get(name, [])
                for candidate in candidates:
                    scope = cpg.scope_of(candidate.id)
                    if scope and scope.kind == NodeKind.MODULE:
                        if (scope.name == source_module
                                or _is_submodule(scope.name, source_module)):
                            return candidate
                # Not found here — recurse (source module may re-export too)
                result = self._follow_reexport(
                    cpg, source_module, name, symbols,
                    module_imports, module_index, _depth + 1,
                )
                if result is not None:
                    return result
                # Recursion returned None — continue checking other
                # imports in this module (e.g., same name re-exported
                # from multiple paths)
    return None


def _resolve_relative_import(
    importing_module: str, relative_module: str, is_package: bool
) -> str:
    """Resolve a relative import against the importing module's qualified name.

    Args:
        importing_module: Qualified name of the module containing the import.
        relative_module: The relative module string (e.g., ".sub", "..utils").
        is_package: True if importing_module is a package (__init__.py).
            Non-package modules must strip their leaf name first, because
            `from .sibling import X` in `pkg.mod` resolves relative to `pkg`,
            not `pkg.mod`.

    Examples:
        _resolve_relative_import("polars", ".dataframe.frame", is_package=True)
            → "polars.dataframe.frame"
        _resolve_relative_import("polars.expr", "..utils", is_package=True)
            → "polars.utils"
        _resolve_relative_import("pkg.mod", ".sibling", is_package=False)
            → "pkg.sibling"  (resolves relative to pkg, not pkg.mod)
    """
    dots = len(relative_module) - len(relative_module.lstrip("."))
    suffix = relative_module.lstrip(".")
    parts = importing_module.split(".")
    # Non-package modules: strip the leaf (mod.py is not a package)
    if not is_package and parts:
        parts = parts[:-1]
    # Go up (dots - 1) levels from the package base
    base_parts = parts[: max(0, len(parts) - (dots - 1))]
    if suffix:
        base_parts.append(suffix)
    return ".".join(base_parts)
```

**This only fires when upstream source is in the CPG.** If polars isn't
in the CPG, there's no module node to inspect and the call stays
unresolved — correct for Tier 1 (consumer-only).

### How the Four Changes Interact

```
Consumer code:  from polars import DataFrame; DataFrame()

Change 2: per-file import_map["DataFrame"] = ("polars", "DataFrame")
Change 2: guard — "DataFrame" in call_import_map → skip name-based (both paths)
Change 1: polars/__init__.py has module name "polars"
Change 3: search symbols for "DataFrame" in module "polars" → no definition
Change 4: polars/__init__.py has `from polars.dataframe.frame import DataFrame`
          → follow chain → find DataFrame (class) in "polars.dataframe.frame"
          → CALLS edge
```

For `import polars as pl; pl.lit(42)`:
```
Change 2: per-file import_map["pl"] = ("polars", None)
Change 2: guard — "pl" prefix in call_import_map → skip name-based (both paths)
Change 3: prefix lookup → imp_entry = ("polars", None), extract method "lit"
Change 3: search symbols for "lit" in module "polars" → no definition
Change 4: polars/__init__.py re-exports lit from polars.utils
          → follow chain → find lit in "polars.utils" → CALLS edge
```

---

## Testing Strategy

Follow treeloom's existing patterns: inline source via `add_source()`,
assert CALLS edge pairs. Tests needing package structure use `tmp_path`
with real `__init__.py` files.

### Test Cases

Tests are grouped by the change they primarily exercise. Each test
lists the adversarial finding(s) it covers.

#### Change 1: Qualified Module Names

1. **Qualified module name for package file** — file inside a package
   directory (with `__init__.py`) gets dotted module name.
   Uses `tmp_path` with real directory structure.
2. **Qualified module name for `__init__.py`** — gets parent dir name,
   not `"__init__"`.
3. **Qualified module name for standalone file** — no `__init__.py` in
   parent → unchanged `file_path.stem`.
24. **Qualified module names with `relative_root`** — build with
    `CPGBuilder(relative_root=tmp_path)`, verify module names are still
    qualified correctly despite path normalization. *(R4-2)*

#### Change 2: Per-File Import Map + Name-Based Suppression

4. **Import alias suppresses name-based fallback** — `ml.lit()` does
   NOT match an internal `lit()` in a different module. Assert no
   false CALLS edge. *(R1-1)*
14. **Per-file import isolation** — `a.py` has `import ml`; `b.py`
    does NOT have `import ml`. `ml.foo()` in `b.py` is not affected
    by `a.py`'s import map. Assert `ml.foo` in `b.py` resolves via
    name-based (not suppressed). *(C2)*
21. **Non-dotted from-import suppresses name-based** — `from pkg import
    lit; lit()` with an internal `lit()` in a different module. The
    guard catches `target in call_import_map` (no dot) and suppresses
    name-based, allowing import-following to resolve correctly. *(R3-5)*
25. **Short-name fallback also suppressed** — explicit test that
    `pl.lit` does NOT resolve to internal `lit()` via the *second*
    name-based path (strip prefix, retry with short name). Two
    modules: one with `import mylib as ml` and internal `lit()`, one
    defining `lit()` in `mylib`. Assert resolved target is in
    `mylib`, not the other module. *(R1-5)*

#### Change 3: Import-Following for Module Imports

5. **Import alias resolves via import-following** — `ml.lit()` →
   CALLS edge to `lit()` in `mylib` module (when source is in CPG).
6. **Unaliased import still works** — `import mylib; mylib.lit()` (no
   alias) also resolves correctly.
7. **From-import still works** — `from mylib import lit; lit()` —
   existing behavior unchanged.
8. **Mixed imports** — both `import X as alias` and `from X import Y`
   in the same file resolve correctly.
9. **No false cross-resolution** — two modules define `lit()`;
   `ml.lit()` resolves to the correct module's `lit()`, not the
   other. **Must assert target scope, not just name.** *(R4-6)*
19. **Dotted bare import `import os.path; os.path.join()`** — verifies
    all prefixes registered in import map (`"os"` and `"os.path"`).
    `os.path.join()` resolves, `os.path.join` guard suppresses
    name-based fallback. *(R3-2)*
23. **Multi-level dotted import stays unresolved** — `import pkg.sub;
    pkg.sub.func()` where `func` is two segments past the longest
    import prefix. Single-remaining-segment guard prevents false
    resolution. Assert no CALLS edge. *(R4-4, C6)*

#### Change 4: Re-Export Following

10. **Re-export resolution** — `from pkg import Foo` where `Foo` is
    defined in `pkg/sub/impl.py` and re-exported via
    `pkg/__init__.py` using `from pkg.sub.impl import Foo`.
    **Must assert target scope is `pkg.sub.impl`.** Uses `tmp_path`.
11. **Chained re-export** — `a/__init__.py` re-exports from
    `a/b/__init__.py` which re-exports from `a/b/c.py` → follows
    chain. Uses `tmp_path`. *(R1-9 partial)*
12. **Re-export with alias** — `pkg/__init__.py` does
    `from pkg.internal import _Impl as PublicName` → consumer's
    `from pkg import PublicName` resolves to `_Impl` in
    `pkg.internal`. **Assert target name is `_Impl`.** *(R4-6)*
13. **Re-exported class resolves** — `from pkg import MyClass;
    MyClass()` → CALLS edge to the CLASS node, not just functions.
    **Assert target kind is `NodeKind.CLASS`.** *(C3)*
16. **Relative re-export via `from .sub import X`** — `__init__.py`
    uses relative import to re-export. Uses `tmp_path` with real
    `__init__.py`. Verifies `_follow_reexport` fires for the most
    common re-export pattern. *(R1-8, R3-1, C5)*
17. **Relative import from non-`__init__.py` module** — `pkg/mod.py`
    does `from .sibling import helper; helper()`. Verifies
    `_resolve_relative_import` strips the leaf module name correctly
    (`pkg.mod` → resolve relative to `pkg`). Uses `tmp_path`. *(R4-3)*
18. **Recursion depth limit at 3 levels** — chain of 4 re-export
    levels: `a/__init__.py` → `a/b/__init__.py` →
    `a/b/c/__init__.py` → `a/b/c/d.py`. The 4th level should NOT
    resolve. Uses `tmp_path`. *(R1-9)*
20. **Alternative re-export path after first fails** — `pkg/__init__.py`
    has two from-imports for the same symbol name; first source module
    doesn't define it, second does. Verifies the `continue` after
    failed recursion. *(R3-3)*
22. **Direct search does NOT match submodule symbols** —
    `from pkg import Foo` where `Foo` is defined in `pkg.internal`
    but NOT re-exported by `pkg/__init__.py`. Must stay unresolved.
    Assert no CALLS edge. *(R4-1)*

#### Known Limitations (negative tests)

15. **Post-import shadowing** — `from pkg import lit; lit = local_fn;
    lit()` — resolves to `pkg.lit` (wrong). Known limitation — the
    resolver has no access to per-scope assignment tracking. Document
    expected (incorrect) behavior so the test serves as a regression
    marker if shadowing support is added later.

### Assertion Strategy

**Simple tests (1-8, 19, 24):** Use `(source_name, target_name)` pairs
via treeloom's existing `_edge_pairs` helper.

**Ambiguous-name tests (9, 12, 21, 25):** Assert the resolved target's
enclosing module scope — not just the name. Follow the existing pattern
from `test_resolved_targets_are_in_lib_module`.

**Negative tests (22, 23, 15):** Assert the call has NO outgoing CALLS
edge, or assert the specific (known-wrong) resolution for limitation
tests.

**Kind assertions (13):** Assert `target.kind == NodeKind.CLASS`.

**Re-export tests (10-12, 16-18, 20):** Assert target scope is the
final definition module (not the re-exporting `__init__.py`). Use
`tmp_path` fixtures with real directory structures.

### Coverage Matrix

| Finding | Test(s) | What it verifies |
|---------|---------|-----------------|
| R1-1 (prefix lookup) | 5 | `alias.method()` resolves via import-following |
| R1-5 (second name-based bypass) | 25 | Short-name strip path also suppressed |
| R1-8 (relative imports in re-exports) | 16 | `from .sub import X` in `__init__.py` works |
| R1-9 (recursion limit) | 18 | 4-level chain stops at 3 |
| C2 (cross-file leakage) | 14 | Import in `a.py` doesn't affect `b.py` |
| C3 (classes missing) | 13 | Re-exported class resolves |
| C6 (multi-level path) | 23 | `pkg.sub.func()` stays unresolved |
| R3-1/C5 (relative import parsing) | 16 | Prerequisite verified end-to-end |
| R3-2 (dotted bare import) | 19 | `import os.path` prefix registration |
| R3-3 (early return in re-export) | 20 | Alternative path checked after first fails |
| R3-5 (non-dotted from-import guard) | 21 | `from pkg import lit; lit()` suppressed |
| R4-1 (submodule bypass) | 22 | Direct search rejects submodule symbols |
| R4-2 (relative_root) | 24 | Qualified names correct with relative_root |
| R4-3 (non-init relative import) | 17 | `pkg/mod.py` relative import resolves correctly |
| R4-4 (dotted bare wrong symbol) | 23 | Single-remaining-segment guard |
| R4-6 (scope assertions) | 9, 12, 22 | Target identity verified, not just name |

### Regression Protection

All existing `TestImportFollowingResolution` tests must continue to
pass. The existing `import_resolution_lib.py` / `import_resolution_caller.py`
fixtures use `from` imports in a non-package directory, so they exercise
the unchanged code path.

---

## Files Changed

| File | Change |
|------|--------|
| `src/treeloom/lang/builtin/python.py` | Add `_derive_qualified_module_name()`, `_follow_reexport()`, `_resolve_relative_import()`, `_is_submodule()`. Modify `visit()` and `resolve_calls()`. |
| `tests/lang/test_python.py` | Add 25 test cases in new test classes |
| `tests/fixtures/python/` | Add package-structure fixtures with `__init__.py` for re-export tests (via `tmp_path`) |

Estimated: ~200-250 lines added/modified in source, ~500 lines of tests.

---

## Adversarial Review Findings

### Round 1 (Opus 4.7)

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| 1 | Import-following can't match dotted calls against module-import keys | CRITICAL | Fixed — Change 3 Part B adds prefix lookup |
| 5 | Second name-based fallback (lines 141-145) bypasses Change 2 guard | SIGNIFICANT | Fixed — Change 2 now wraps both paths |
| 2 | `_derive_qualified_module_name` fails for `add_source()` virtual paths | SIGNIFICANT | Fixed — two-strategy approach |
| 3 | `_follow_reexport` scans all imports — O(calls * imports) | SIGNIFICANT | Fixed — pre-built `module_imports` index |
| 8 | Relative imports in re-export chains fail silently | MINOR | Fixed — `_resolve_relative_import` helper |
| 9 | Recursion limit described but not in pseudocode | MINOR | Fixed — `_depth` parameter |
| 4 | Spec says "Three changes" but describes four | MINOR | Fixed |
| 10 | `_is_submodule` used but not defined | MINOR | Fixed |

### Round 2 (Codex)

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| C1 | Re-export index key type mismatch (CpgNode vs NodeId) | CRITICAL | Fixed — use `scope_node.id` as key, explicit NOTE added |
| C2 | Import map global, not per-file — cross-file leakage | CRITICAL | Fixed — Change 2 rewritten with per-file maps |
| C3 | Re-export only searches functions, misses classes | SIGNIFICANT | Fixed — `symbols` dict includes CLASS nodes |
| C6 | `pkg.sub.func()` extracts wrong level | SIGNIFICANT | Documented as known limitation |
| C10 | Linear scan of module nodes per lookup | SIGNIFICANT | Fixed — `module_index` dict |
| C8+C9 | Missing tests for leakage and re-exported classes | SIGNIFICANT | Fixed — test cases 13-14 added |
| C5 | Relative imports may lose dot prefix in parser | MINOR | NOTE added — verify during implementation |
| C7 | Type-based can override import-prefix guard | MINOR | Acceptable edge case |
| C-star | Star imports unmodeled | MINOR | Out of scope |

### Round 3 (Opus 4.7, second pass)

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| R3-1 | Relative imports produce empty `module` attr — Change 4 dead code | SIGNIFICANT | Fixed — prerequisite added to Change 4 to handle `relative_import` nodes |
| R3-2 | `import os.path` registers as `"os.path"` but prefix lookup uses `"os"` | SIGNIFICANT | Fixed — register all prefixes for dotted bare imports |
| R3-3 | `_follow_reexport` early return skips alternative import paths | SIGNIFICANT | Fixed — `continue` instead of `return` after failed recursion |
| R3-5 | Guard doesn't cover non-dotted from-imports | SIGNIFICANT | Fixed — guard also checks `target in call_import_map` |
| R3-4 | `module_index` last-write-wins for duplicates | MINOR | Documented — acceptable for MVP |
| R3-6 | Try/except conditional imports both registered | MINOR | Out of scope — inherent static analysis limitation |

### Round 4 (Codex, second pass)

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| R4-1 | `_is_submodule` in direct lookup bypasses re-export gate | CRITICAL | Fixed — direct search uses `scope.name == imp_module` only |
| R4-3 | `_resolve_relative_import` wrong for non-`__init__.py` modules | SIGNIFICANT | Fixed — added `is_package` param, strips leaf for non-packages |
| R4-2 | `_derive_qualified_module_name` breaks under `relative_root` | SIGNIFICANT | Fixed — noted must use absolute disk path |
| R4-4 | Dotted bare import can match wrong symbol | SIGNIFICANT | Fixed — longest-prefix lookup, single-segment-remaining guard |
| R4-5 | `defaultdict` not imported in python.py | SIGNIFICANT | Fixed — replaced with `dict.setdefault` |
| R4-6 | Tests assert name pairs only, not target identity | MINOR | Fixed — scope assertion strategy added |

---

## Out of Scope

- Import-derived type inference for method calls (`df = pl.DataFrame();
  df.filter()`)
- Call name cleanup for chained expressions
- Multi-level attribute paths (`pkg.sub.func()`) — single-level
  `alias.method()` only
- Namespace packages (PEP 420, no `__init__.py`)
- Star imports (`from pkg import *`)
- Post-import shadowing (`from pkg import f; f = other; f()`)
- Conditional imports in try/except blocks (both branches register;
  last-write-wins is acceptable for static analysis)
- Changes to any language visitor other than Python
- Changes to the builder, CPG, or analysis layers
