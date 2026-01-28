# PEP 695 Implementation Review

Review of the PEP 695 (Type Parameter Syntax) implementation in PyPy.

## Summary

The implementation provides **complete** support for PEP 695 type parameter syntax including type aliases, generic functions, generic classes, TypeVar with bounds/constraints, ParamSpec, and TypeVarTuple.

**All issues identified by comparison with CPython PR #103764 have been resolved.** The implementation now:
- Uses unified TypeVar/ParamSpec/TypeVarTuple/Generic classes imported into `typing.py` from `_pypy_typing.py`
- Correctly sets `infer_variance=True` for PEP 695 type parameters
- Implements all required helper methods (`__typing_subst__`, `__typing_prepare_subst__`, `__mro_entries__`, `__parameters__`)
- **Supports class namespace access from annotation scopes via `__classdict__` mechanism**

---

## Architecture Comparison with CPython

| Feature | CPython | PyPy |
|---------|---------|------|
| Type param creation | Intrinsic opcodes | Module imports + function calls |
| TypeVar/ParamSpec/TypeVarTuple location | C module (`_typing`) | `_pypy_typing.py` (imported by `typing.py`) |
| Class dict access | `__classdict__` cell + special opcodes | `__classdict__` cell + `LOAD_FROM_DICT_OR_*` opcodes |
| Lazy evaluation | Closure functions | Closure functions |
| `__type_params__` setting | `INTRINSIC_SET_FUNCTION_TYPE_PARAMS` | `STORE_ATTR` |
| `infer_variance` for PEP 695 params | Always `True` | Always `True` |

PyPy's approach of using module imports (`from _pypy_typing import ...`) rather than intrinsic opcodes is a valid design choice that mirrors how CPython's `typing.py` imports from the C `_typing` module.

---

## What Works Well

1. **Core functionality is correct** - Type aliases, generic functions, generic classes, bounds, constraints, ParamSpec, TypeVarTuple all work.

2. **Proper scope structure** - `AnnotationScope` correctly restricts yield, await, and walrus operator.

3. **Lazy evaluation** - Bounds, constraints, and type alias values are lazily evaluated through closure functions.

4. **Decorator handling** - Decorators are correctly applied after `__type_params__` is set.

5. **`__type_params__` attribute** - Properly implemented on both functions and classes with correct getter/setter behavior.

6. **Forward references** - Type alias values support forward references due to lazy evaluation.

7. **Unified type classes** - `typing.py` imports TypeVar, ParamSpec, TypeVarTuple, TypeAliasType, and Generic from `_pypy_typing.py`, ensuring type introspection works correctly.

---

## Test Coverage

The test suite (`apptest_pep695.py`) covers:
- Basic and generic type aliases
- TypeVar with bounds and constraints
- ParamSpec and TypeVarTuple
- Generic functions and classes
- Lazy evaluation (bounds, values, constraints)
- Invalid expressions in annotation scope (walrus, yield, await)
- TypeAliasType direct creation and subscripting
- `__type_params__` attribute behavior
- Stacked decorators on generic functions/classes
- Generic methods inside generic classes
- Forward references in type aliases
- Nested generic classes
- Class namespace access from annotation scopes (via `__classdict__`)

---

## Files Changed

| File | Changes |
|------|---------|
| `pypy/interpreter/astcompiler/codegen.py` | Type param code generators, `__classdict__` lookups |
| `pypy/interpreter/astcompiler/symtable.py` | Annotation scope, type param visitors, `__classdict__` tracking |
| `pypy/interpreter/astcompiler/assemble.py` | Stack effects for `LOAD_FROM_DICT_OR_*` opcodes |
| `lib_pypy/_pypy_typing.py` | TypeVar, ParamSpec, TypeVarTuple, TypeAliasType, Generic |
| `lib-python/3/typing.py` | Import from `_pypy_typing`, helper functions |
| `pypy/interpreter/function.py` | `__type_params__` on functions |
| `pypy/objspace/std/typeobject.py` | `__type_params__` on types |
| `pypy/interpreter/typedef.py` | Function typedef |
| `pypy/interpreter/test/apptest_pep695.py` | Tests |

---

## Issues Resolved (Comparison with CPython PR #103764)

The following issues were identified and **resolved** by comparing the PyPy implementation with CPython's PEP 695 implementation (PR #103764 and CPython 3.12 source).

### ✅ Critical: Duplicate TypeVar/ParamSpec/TypeVarTuple Classes

**Status: RESOLVED**

`typing.py` now imports TypeVar, ParamSpec, TypeVarTuple, ParamSpecArgs, ParamSpecKwargs, TypeAliasType, and Generic from `_pypy_typing.py`. The old Python implementations in `typing.py` were removed.

---

### ✅ Critical: `infer_variance` Not Set for PEP 695 Type Parameters

**Status: RESOLVED**

The factory functions (`_make_typevar`, `_make_typevar_with_bound`, `_make_typevar_with_constraints`, `_make_paramspec`) now set `infer_variance=True` for PEP 695 type parameters. TypeVars created with PEP 695 syntax now correctly show `T` in repr instead of `~T`.

---

### ✅ Medium: TypeAliasType Missing `__parameters__` Property

**Status: RESOLVED**

TypeAliasType now has a `__parameters__` property that returns the type parameters with TypeVarTuples unpacked.

---

### ✅ Medium: TypeVarTuple Missing `__typing_subst__` and `__typing_prepare_subst__`

**Status: RESOLVED**

TypeVarTuple now implements:
- `__typing_subst__`: raises `TypeError("Substitution of bare TypeVarTuple is not supported")`
- `__typing_prepare_subst__`: calls `typing._typevartuple_prepare_subst`

---

### ✅ Medium: ParamSpec Missing `__typing_prepare_subst__`

**Status: RESOLVED**

ParamSpec now implements `__typing_prepare_subst__` which calls `typing._paramspec_prepare_subst`.

---

### ✅ Low: TypeAliasType Allows Subscripting Non-Generic Aliases

**Status: RESOLVED**

TypeAliasType now raises `TypeError("Only generic type aliases are subscriptable")` when attempting to subscript a non-generic type alias.

---

### ✅ Low: Missing `__mro_entries__` Methods

**Status: RESOLVED**

TypeVar, ParamSpec, TypeVarTuple, and TypeAliasType now implement `__mro_entries__` to raise appropriate `TypeError` when used as a base class.

---

### ✅ Low: TypeVar.__typing_subst__ Behavior Differs

**Status: RESOLVED**

TypeVar now calls `typing._typevar_subst(self, arg)` matching CPython's behavior.

---

## Summary of Issues

| Issue | Severity | Status |
|-------|----------|--------|
| Duplicate TypeVar/ParamSpec/TypeVarTuple classes | Critical | ✅ Resolved |
| `infer_variance` not set | Critical | ✅ Resolved |
| Missing `__parameters__` | Medium | ✅ Resolved |
| Missing TypeVarTuple methods | Medium | ✅ Resolved |
| Missing ParamSpec method | Medium | ✅ Resolved |
| Non-generic subscript allowed | Low | ✅ Resolved |
| Missing `__mro_entries__` | Low | ✅ Resolved |
| TypeVar.__typing_subst__ | Low | ✅ Resolved |

---

## Completion Status

The PEP 695 implementation is **feature-complete** and matches CPython 3.12+ behavior. All major functionality is implemented including class namespace access from annotation scopes.
