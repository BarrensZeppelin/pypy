# PEP 695 Implementation Review

Review of the PEP 695 (Type Parameter Syntax) implementation in PyPy.

## Summary

The implementation provides working support for PEP 695 type parameter syntax including type aliases, generic functions, generic classes, TypeVar with bounds/constraints, ParamSpec, and TypeVarTuple. Basic tests pass.

**However, comparison with CPython PR #103764 revealed several issues:**
- **Critical:** Duplicate TypeVar/ParamSpec/TypeVarTuple classes exist in both `typing.py` and `_pypy_typing.py` - they should be unified
- **Critical:** `infer_variance` is not set to `True` for PEP 695 type parameters
- **Medium:** Missing `__parameters__` property on TypeAliasType and missing methods on ParamSpec/TypeVarTuple
- One known limitation (class namespace access) is documented with a test

See "Issues Found" section below for full details.

---

## Known Limitation

### Class Namespace Access from Annotation Scopes Not Supported

**Severity: Medium** (documented with test)

Type parameter bounds and type alias values cannot access names defined in an enclosing class scope:

```python
class Outer:
    BaseType = int
    type Alias[T: BaseType] = list[T]  # NameError when accessing T.__bound__
```

This works correctly in CPython 3.12+, which uses a `__classdict__` cell and special `LOAD_CLASSDICT_OR_*` opcodes to allow annotation scopes to access the class namespace during class body execution.

**Affected files:**
- `pypy/interpreter/astcompiler/codegen.py`
- `pypy/interpreter/astcompiler/symtable.py`

**To fix:** Would require implementing a mechanism similar to CPython's `__classdict__` cell that gets populated during class body execution and is accessible from nested annotation scopes.

**Test:** `test_class_namespace_access_from_annotation_scope` documents this behavior.

---

## Architecture Comparison with CPython

| Feature | CPython | PyPy |
|---------|---------|------|
| Type param creation | Intrinsic opcodes | Module imports + function calls |
| TypeVar/ParamSpec/TypeVarTuple location | C module (`_typing`) | **Duplicated in typing.py AND _pypy_typing.py** |
| Class dict access | `__classdict__` cell + special opcodes | **Not implemented** |
| Lazy evaluation | Closure functions | Closure functions |
| `__type_params__` setting | `INTRINSIC_SET_FUNCTION_TYPE_PARAMS` | `STORE_ATTR` |
| `infer_variance` for PEP 695 params | Always `True` | **Always `False`** |

PyPy's approach of using module imports (`from _pypy_typing import ...`) rather than intrinsic opcodes is a valid design choice. However, the duplicate class definitions and missing `infer_variance` handling are significant issues that need to be addressed.

---

## What Works Well

1. **Core functionality is correct** - Type aliases, generic functions, generic classes, bounds, constraints, ParamSpec, TypeVarTuple all work.

2. **Proper scope structure** - `AnnotationScope` correctly restricts yield, await, and walrus operator.

3. **Lazy evaluation** - Bounds, constraints, and type alias values are lazily evaluated through closure functions.

4. **Decorator handling** - Decorators are correctly applied after `__type_params__` is set.

5. **`__type_params__` attribute** - Properly implemented on both functions and classes with correct getter/setter behavior.

6. **Forward references** - Type alias values support forward references due to lazy evaluation.

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
- Class namespace access limitation (expected failure)

---

## Files Changed

| File | Changes |
|------|---------|
| `pypy/interpreter/astcompiler/codegen.py` | Type param code generators |
| `pypy/interpreter/astcompiler/symtable.py` | Annotation scope, type param visitors |
| `lib_pypy/_pypy_typing.py` | TypeVar, ParamSpec, TypeVarTuple, TypeAliasType |
| `pypy/interpreter/function.py` | `__type_params__` on functions |
| `pypy/objspace/std/typeobject.py` | `__type_params__` on types |
| `pypy/interpreter/typedef.py` | Function typedef |
| `pypy/interpreter/test/apptest_pep695.py` | Tests |

---

## Issues Found (Comparison with CPython PR #103764)

The following issues were identified by comparing the PyPy implementation with CPython's PEP 695 implementation (PR #103764 and CPython 3.12.12 source).

### Critical: Duplicate TypeVar/ParamSpec/TypeVarTuple Classes

**Severity: Critical**

PyPy has two separate implementations of TypeVar, ParamSpec, and TypeVarTuple:

1. `lib-python/3/typing.py` - Old Python implementations (lines 995, 1063, 1190)
2. `lib_pypy/_pypy_typing.py` - New implementations with PEP 695 support

**CPython's approach:** In CPython 3.12+, these classes are implemented in C (`Objects/typevarobject.c`) and `typing.py` imports them from the `_typing` module. The old Python implementations were deleted.

**PyPy's problem:** `typing.py` does NOT import from `_pypy_typing`, so:

```python
from typing import TypeVar as TypingTypeVar

def foo[T](x: T) -> T:  # T is a _pypy_typing.TypeVar
    return x

# This fails:
isinstance(foo.__type_params__[0], TypingTypeVar)  # False!
```

**Impact:**
- Type introspection breaks - `isinstance(x, typing.TypeVar)` fails for PEP 695 TypeVars
- typing module internals don't recognize PEP 695 type params
- Third-party tools (mypy, pyright runtime helpers) may break

**Fix:** Modify `lib-python/3/typing.py` to import from `_pypy_typing` and delete the duplicate class definitions:
```python
from _pypy_typing import (
    TypeVar,
    ParamSpec,
    TypeVarTuple,
    ParamSpecArgs,
    ParamSpecKwargs,
    TypeAliasType,
)
```

---

### Critical: `infer_variance` Not Set for PEP 695 Type Parameters

**Severity: Critical**

**Location:** `pypy/interpreter/astcompiler/codegen.py` lines 2735-2790, `lib_pypy/_pypy_typing.py` lines 381-418

When creating TypeVars and ParamSpecs via PEP 695 syntax (`def foo[T](x: T)`), they should have `infer_variance=True`.

- **CPython** (`Objects/typevarobject.c:1245-1246`): `_Py_make_typevar` **always** sets `infer_variance=true`
- **PyPy**: `_make_typevar` defaults `infer_variance=False`, and `codegen.py` never passes `infer_variance=True`

**Impact:**
- TypeVars show `~T` in repr instead of just `T`
- Type checkers won't correctly infer variance

**CPython test that would fail:**
```python
def func1[A: str]():
    return A

a = func1()
assert a.__infer_variance__ == True  # Fails in PyPy
```

**Fix:** Modify codegen to pass `infer_variance=True` when calling `_make_typevar`, `_make_typevar_with_bound`, `_make_typevar_with_constraints`, and `_make_paramspec`.

---

### Medium: TypeAliasType Missing `__parameters__` Property

**Severity: Medium**

**Location:** `lib_pypy/_pypy_typing.py` lines 298-375

CPython's `TypeAliasType` has a `__parameters__` property (`Objects/typevarobject.c:1312-1319`) that returns the type parameters with TypeVarTuples unpacked. PyPy's implementation is missing this property.

**CPython test that would fail:**
```python
type TA1 = int
assert TA1.__parameters__ == ()  # AttributeError in PyPy

type Generic[T, *Ts] = tuple[T, *Ts]
assert len(Generic.__parameters__) == 2  # AttributeError in PyPy
```

---

### Medium: TypeVarTuple Missing `__typing_subst__` and `__typing_prepare_subst__`

**Severity: Medium**

**Location:** `lib_pypy/_pypy_typing.py` TypeVarTuple class

CPython implements:
- `__typing_subst__`: raises `TypeError("Substitution of bare TypeVarTuple is not supported")`
- `__typing_prepare_subst__`: calls `typing._typevartuple_prepare_subst`

PyPy's TypeVarTuple is missing both methods.

---

### Medium: ParamSpec Missing `__typing_prepare_subst__`

**Severity: Medium**

**Location:** `lib_pypy/_pypy_typing.py` ParamSpec class

CPython's ParamSpec (`Objects/typevarobject.c:902-917`) implements `__typing_prepare_subst__` which calls `typing._paramspec_prepare_subst`. PyPy doesn't have this method.

---

### Low: TypeAliasType Allows Subscripting Non-Generic Aliases

**Severity: Low**

**Location:** `lib_pypy/_pypy_typing.py` lines 356-361

CPython (`Objects/typevarobject.c:1416-1425`) raises `TypeError` when subscripting a non-generic type alias:

```python
type NonGeneric = int
NonGeneric[int]  # CPython: TypeError: Only generic type aliases are subscriptable
                 # PyPy: Returns a _GenericAlias (no error)
```

---

### Low: Missing `__mro_entries__` Methods

**Severity: Low**

**Location:** `lib_pypy/_pypy_typing.py`

CPython's TypeVar, ParamSpec, and TypeVarTuple implement `__mro_entries__` to raise `TypeError("Cannot subclass an instance of TypeVar")` when used as a base class. PyPy doesn't implement these methods.

---

### Low: TypeVar.__typing_subst__ Behavior Differs

**Severity: Low**

**Location:** `lib_pypy/_pypy_typing.py` line 143-145

- **CPython** (`Objects/typevarobject.c:410-416`): calls `typing._typevar_subst(self, arg)`
- **PyPy**: simply returns `arg`

This may affect generic type substitution behavior in edge cases.

---

## Summary of Issues

| Issue | Severity | Files Affected |
|-------|----------|----------------|
| Duplicate TypeVar/ParamSpec/TypeVarTuple classes | **Critical** | lib-python/3/typing.py, lib_pypy/_pypy_typing.py |
| `infer_variance` not set | **Critical** | codegen.py, _pypy_typing.py |
| Missing `__parameters__` | Medium | _pypy_typing.py |
| Missing TypeVarTuple methods | Medium | _pypy_typing.py |
| Missing ParamSpec method | Medium | _pypy_typing.py |
| Non-generic subscript allowed | Low | _pypy_typing.py |
| Missing `__mro_entries__` | Low | _pypy_typing.py |
| TypeVar.__typing_subst__ | Low | _pypy_typing.py |

---

## Future Work

1. **Implement class namespace access for annotation scopes** - This is the only remaining functional gap compared to CPython 3.12+.
