# PEP 695 Implementation Review

Review of the PEP 695 (Type Parameter Syntax) implementation in PyPy.

## Summary

The implementation provides working support for PEP 695 type parameter syntax including type aliases, generic functions, generic classes, TypeVar with bounds/constraints, ParamSpec, and TypeVarTuple. All 32 tests pass. There is one known limitation (class namespace access) which is documented with a test.

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
| Class dict access | `__classdict__` cell + special opcodes | **Not implemented** |
| Lazy evaluation | Closure functions | Closure functions |
| `__type_params__` setting | `INTRINSIC_SET_FUNCTION_TYPE_PARAMS` | `STORE_ATTR` |

PyPy's approach of using module imports (`from _pypy_typing import ...`) rather than intrinsic opcodes is a valid design choice that works well. The missing class dict access is the only functional gap.

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

## Future Work

1. **Implement class namespace access for annotation scopes** - This is the only remaining functional gap compared to CPython 3.12+.
