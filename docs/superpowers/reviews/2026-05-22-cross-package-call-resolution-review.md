# Post-Implementation Review: Cross-Package Call Resolution

**Date:** 2026-05-22
**Branch:** `feature/cross-package-call-resolution`
**Spec:** `docs/specs/2026-05-22-cross-package-call-resolution-design.md`
**Plan:** `docs/superpowers/plans/2026-05-22-cross-package-call-resolution.md`

---

## Summary

Six commits to `src/treeloom/lang/builtin/python.py` (and a small
supporting change to `builder.py`) that fix how treeloom's Python
visitor resolves CALLS edges for imported symbols. Before this work,
`import polars as pl; pl.lit(42)` would falsely resolve to any
internal function named `lit()` via name-based fallback. After this
work, it either resolves to the correct upstream `lit()` (if the
source is in the CPG) or stays unresolved (if it isn't).

**Stats:** +902 lines across 3 files (260 production, 641 test).
28 new tests. 1319 total tests passing, 0 regressions.

---

## What Changed

### 1. Relative Import Parsing (`ed19d94`)

**Problem:** `_visit_import_from_statement` only handled `dotted_name`
children when extracting the module name from `from X import Y`. For
relative imports (`from .sub import X`), tree-sitter emits a
`relative_import` node, which was silently ignored — storing
`module=""` for all relative imports.

**Fix:** Added a `relative_import` node type handler in the
`for child in node.children` loop, extracting the full text (dots +
module name) as `module_name`. Six lines.

**Why this matters:** `__init__.py` re-exports almost universally use
relative imports (`from .dataframe.frame import DataFrame`). Without
this fix, the re-export follower (Change 4) would never fire.

### 2. Qualified Module Names (`8854809`)

**Problem:** Module nodes got `file_path.stem` as their name. So
`polars/expr/string.py` became module `"string"`, not
`"polars.expr.string"`. Import-following resolution (which matches
against module names) could only do fuzzy stem matching, producing
false positives.

**Fix:** Added `_derive_qualified_module_name(file_path)` which walks
up from the file checking each parent directory for `__init__.py` to
find the package root, then constructs a dotted name from the relative
path. Falls back to `file_path.stem` for standalone files and virtual
paths (from `add_source()`).

**Supporting change in `builder.py`:** When `CPGBuilder(relative_root=...)`
is set, the visitor receives a normalized (relative) path that can't be
resolved back to disk for `__init__.py` discovery. The builder now
stashes the original absolute disk path as `_current_original_path` on
itself (it serves as the `NodeEmitter`), and the visitor retrieves it
via `getattr(emitter, "_current_original_path", None)`. This is a
pragmatic coupling — the alternative would be changing the
`NodeEmitter` protocol, which touches every language visitor.

**Mapping:**

| Input Path | Module Name |
|------------|-------------|
| `polars/expr/string.py` | `polars.expr.string` |
| `polars/__init__.py` | `polars` |
| `utils.py` (no `__init__.py` in parent) | `utils` |
| Virtual path via `add_source()` | `file_path.stem` |

### 3. Per-File Import Map + Name-Based Suppression (`acae688`)

**Problem:** The import map was global (one dict for the entire CPG)
and only tracked `from X import Y` patterns. This caused two failures:

1. **Cross-file leakage:** `a.py`'s `import ml` could suppress
   name-based resolution for `ml()` calls in `b.py`, where `ml` might
   be a local function.
2. **Missing `import X as alias`:** `import polars as pl` was silently
   dropped from the import map, so `pl` was never recognized as an
   import alias during resolution.

**Fix:** Replaced the global `import_map: dict` with
`file_import_maps: dict[str, dict[str, tuple[str, str | None]]]`,
keyed by file path. Both `from X import Y` and `import X as alias`
now populate the per-file map. An `is_import_known` guard wraps BOTH
name-based fallback paths:

```python
is_import_known = (
    target in call_import_map                              # from pkg import lit; lit()
    or ("." in target and target.split(".")[0] in call_import_map)  # import pkg as pl; pl.lit()
)

if fn is None and not is_import_known:
    fn = self._resolve_single_call(...)          # primary name-based

if fn is None and not is_import_known and "." in target:
    short_name = target.rsplit(".", 1)[-1]
    fn = self._resolve_single_call(...)          # short-name fallback
```

Guarding both paths is critical — the short-name path strips `pl.`
from `pl.lit` and retries with just `lit`, which would match any
internal `lit()`.

**New indexes built in the same loop:**
- `module_imports: dict[NodeId, list[CpgNode]]` — imports scoped to
  each module (for re-export following)
- `symbols: dict[str, list[CpgNode]]` — functions + classes by name
  (classes needed for re-exported class resolution)
- `module_index: dict[str, CpgNode]` — module name → module node
  (avoids linear scan per lookup)

**Import-following rewrite:** Uses longest-prefix matching against the
per-file import map. For `pl.lit()` with `import_map["pl"] = ("polars", None)`,
the prefix lookup finds `"pl"`, extracts `"lit"` as the method, then
searches for `lit` in module `"polars"`.

For dotted bare imports (`import os.path`), all prefixes are registered:
`fmap["os"] = ("os", None)` and `fmap["os.path"] = ("os.path", None)`.

Multi-level attribute paths (`pkg.sub.func()`) are guarded: if the
longest-prefix match leaves more than one remaining segment, the call
stays unresolved rather than falsely resolving.

### 4. Re-Export Following (`e6c0623`)

**Problem:** Most Python packages re-export their public API through
`__init__.py`. When Changes 1–3 search for a symbol in a module scope
and find no definition (only an import statement), they give up.

**Fix:** Three new methods on `PythonVisitor`:

**`_follow_reexport(cpg, module_name, symbol_name, symbols, module_imports, module_index, _depth=0)`:**
When the direct lookup in a module fails, this method checks whether
that module has a from-import that brings the symbol in from elsewhere.
If so, it follows the chain — searching for the symbol in the source
module, then recursing if needed. Depth-limited to 3 levels to prevent
infinite loops.

**`_resolve_relative_import(importing_module, relative_module, is_package)`:**
Converts relative module references (`.sub`, `..utils`) to absolute
module names. Handles the asymmetry between package modules
(`__init__.py`, where `.sub` means "my child") and non-package modules
(`mod.py`, where `.sibling` means "my parent's child").

**`_is_submodule(name, parent)`:**
Simple prefix check — `polars.expr.string` is a submodule of
`polars.expr`. Used in `_follow_reexport` to match candidates that
live deeper in the package tree than the re-export's source module.

**Relative import resolution in the call loop:** The import-following
path in `resolve_calls` now also resolves relative imports at the call
site level. When `imp_module` starts with `.`, it walks up the scope
chain to find the calling module and resolves the relative reference
against it. This handles `from .sibling import helper; helper()` in
non-`__init__.py` modules.

---

## Deviations from the Spec

### 1. `builder.py` change (not in spec)

The spec assumed `_derive_qualified_module_name` could resolve the
disk path from `file_path` alone. In practice, when `relative_root`
is set, the visitor receives a normalized relative path that can't be
resolved back to disk. The builder change stashes the original path
as a workaround.

**Risk:** Couples the visitor to the builder's internal state via
`getattr`. Acceptable because the fallback (using `file_path` directly)
is correct for all non-`relative_root` cases, and the coupling is
limited to one `getattr` call.

### 2. `imp_mod_parts` fallback matching (spec said exact match only)

The spec's Change 3 said from-imports should use exact module matching
(`scope.name == imp_module`). The implementation also checks
`scope.name in imp_module.rsplit(".", 1)` — matching the last component
of a dotted import module against the CPG module name.

**Why:** `add_source()` with virtual paths creates modules with stem
names (e.g., `"utils"` for `utils.py`). A `from pkg.utils import helper`
has `imp_module="pkg.utils"`, but the CPG module is `"utils"`. Without
the fallback, existing `TestImportFollowingResolution` tests would
break. The fallback preserves backward compatibility for virtual-path
test fixtures.

**Risk:** Could produce false positives if two modules share a stem
(e.g., `pkg.utils` and `other.utils`). Acceptable because: (a) this
only affects `add_source()` virtual paths, which don't have package
structure; (b) the `is_import_known` guard prevents name-based false
positives regardless; (c) real package directories use qualified names
from Change 1, where exact matching works.

### 3. Relative import resolution in call loop (spec didn't include this)

The spec described relative import resolution only inside
`_follow_reexport`. The implementation also resolves relative imports
in the main import-following path (lines 247–267 of the final code).
This is needed because `file_import_maps` stores raw relative module
names from the parser (e.g., `".sibling"`), and the import-following
lookup needs absolute names to match against `module_index`.

### 4. `test_alternative_reexport_path` changed to use `tmp_path`

The plan specified this test using `add_source()` with virtual paths,
but the re-export follower requires real package structure (with
`__init__.py` files and qualified module names) to function. The
implementer correctly switched to `tmp_path` with real directories.

### 5. `test_dotted_bare_import_prefix_registration` fixture renamed

The plan used `path.py` as the unrelated module, but `"path"` matches
`imp_mod_parts` for `import os.path` (since `"os.path".rsplit(".", 1)`
includes `"path"`). The implementer used `myutils.py` instead to avoid
the naming collision.

---

## Test Coverage

| Test Class | Tests | What It Validates |
|------------|-------|-------------------|
| `TestRelativeImportParsing` | 2 | `.sub` and `..utils` stored correctly in import attrs |
| `TestQualifiedModuleNames` | 5 | Dotted names, `__init__.py` → package name, standalone fallback, virtual fallback, `relative_root` |
| `TestPerFileImportMap` | 4 | Alias suppression, per-file isolation, from-import suppression, short-name fallback suppression |
| `TestModuleImportResolution` | 7 | Alias resolution, unaliased import, from-import, mixed imports, cross-resolution protection, dotted bare imports, multi-level guard |
| `TestReexportFollowing` | 9 | Basic re-export, aliased re-export, class re-export, relative re-export, non-init relative, chained (2 levels), depth limit (3), alternative paths, submodule bypass prevention |
| `TestKnownLimitations` | 1 | Post-import shadowing regression marker |

**Adversarial review coverage:** 16 findings from 4 review rounds
are covered by specific tests (see spec's Coverage Matrix).

---

## Known Limitations

1. **Post-import shadowing:** `from pkg import lit; lit = other; lit()`
   resolves to `pkg.lit` (wrong). No per-scope assignment tracking.

2. **Namespace packages (PEP 420):** Packages without `__init__.py`
   get truncated module names.

3. **Star imports:** `from pkg import *` is not modeled.

4. **`module_index` last-write-wins:** If two modules share a name,
   the last one indexed wins. Acceptable for MVP.

5. **Conditional imports:** Both branches of `try/except` imports are
   registered; last-write-wins.

6. **Multi-level attribute paths:** `pkg.sub.func()` where two
   segments remain past the longest import prefix stays unresolved
   (by design — prevents false positives).

---

## Upstream Potential

All changes are general-purpose Python resolution improvements, not
PointBreak-specific. They would benefit any treeloom user working with
Python packages. A PR to `rdwj/treeloom` could be structured as:

1. Relative import parsing fix (clear bug fix)
2. Qualified module names (clear improvement)
3. Per-file import map + guard (accuracy improvement)
4. Re-export following (feature addition)
