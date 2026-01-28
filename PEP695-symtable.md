# PEP 695 Symbol Table Implementation: CPython vs PyPy Comparison

This document compares the CPython and PyPy implementations of PEP 695 (Type Parameter Syntax) in the symbol table, identifying behavioral differences that may need to be addressed.

## Critical Differences

### 1. Missing `DEF_TYPE_PARAM` flag and duplicate detection

**CPython** (symtable.c:1229, 2125-2147):
- Type parameters are marked with `DEF_TYPE_PARAM | DEF_LOCAL`
- Duplicate type params trigger error: `"duplicate type parameter '%U'"`

```c
// symtable.c:1229
if ((flag & DEF_TYPE_PARAM) && (val & DEF_TYPE_PARAM)) {
    PyErr_Format(PyExc_SyntaxError, DUPLICATE_TYPE_PARAM, name);
    ...
}

// symtable.c:2125
if (!symtable_add_def(st, tp->v.TypeVar.name, DEF_TYPE_PARAM | DEF_LOCAL, LOCATION(tp)))
```

**PyPy** (symtable.py:949, 966, 970):
- Uses only `SYM_ASSIGNED` for type params
- **Missing**: No duplicate type parameter detection

```python
# symtable.py:949
self.note_symbol(type_var.name, SYM_ASSIGNED)
```

**Impact**: Code like `def f[T, T](): pass` should raise `SyntaxError` but won't in PyPy.

---

### 2. Missing nonlocal binding restriction for type params

**CPython** (symtable.c:553-558):
```c
if (PySet_Contains(type_params, name)) {
    PyErr_Format(PyExc_SyntaxError,
                 "nonlocal binding not allowed for type parameter '%U'",
                 name);
    return error_at_directive(ste, name);
}
```

**PyPy**: **Missing** - doesn't track type params across scopes during analysis, so this check is absent.

**Impact**: Code like this should raise `SyntaxError` but won't in PyPy:
```python
def outer[T]():
    def inner():
        nonlocal T  # Should error
```

---

### 3. Missing `class_entry` handling in name resolution

**CPython** (symtable.c:579-596):
When resolving names in annotation scopes that can see class scope, if a name is bound in the enclosing class:
- If class has `DEF_GLOBAL` → resolve as `GLOBAL_EXPLICIT`
- If class has `DEF_BOUND` (not nonlocal) → resolve as `GLOBAL_IMPLICIT`

```c
if (class_entry != NULL) {
    long class_flags = _PyST_GetSymbol(class_entry, name);
    if (class_flags & DEF_GLOBAL) {
        SET_SCOPE(scopes, name, GLOBAL_EXPLICIT);
        return 1;
    }
    else if (class_flags & DEF_BOUND && !(class_flags & DEF_NONLOCAL)) {
        SET_SCOPE(scopes, name, GLOBAL_IMPLICIT);
        return 1;
    }
}
```

**PyPy**: `_finalize_name` doesn't have this special `class_entry` logic. Names bound in enclosing class scope may incorrectly resolve as `FREE` instead of `GLOBAL_IMPLICIT`.

**Impact**: Incorrect variable resolution in cases like:
```python
x = "global"
class C:
    x = "class"
    type Alias = x  # Should see global x, not class x via FREE
```

---

### 4. Missing special synthetic variables for generic classes

**CPython** (symtable.c:1326-1347):
For `ClassDef_kind` with type params, creates:
- `.type_params` (DEF_LOCAL + USE) - stores the type params tuple
- `.generic_base` (DEF_LOCAL + USE) - for setting the generic base
- Sets `st_private = name` for name mangling

```c
if (kind == ClassDef_kind) {
    _Py_DECLARE_STR(type_params, ".type_params");
    if (!symtable_add_def(st, &_Py_STR(type_params), DEF_LOCAL, ...))
        return 0;
    if (!symtable_add_def(st, &_Py_STR(type_params), USE, ...))
        return 0;
    st->st_private = name;
    _Py_DECLARE_STR(generic_base, ".generic_base");
    if (!symtable_add_def(st, &_Py_STR(generic_base), DEF_LOCAL, ...))
        return 0;
    if (!symtable_add_def(st, &_Py_STR(generic_base), USE, ...))
        return 0;
}
```

**PyPy**: **Missing** - doesn't add these synthetic variables for generic classes.

**Impact**: Code generation for generic classes may fail or produce incorrect bytecode.

---

### 5. Missing lambda/comprehension restrictions in class annotation scopes

**CPython** (symtable.c:1971-1980, 2431-2437):
```c
// For lambda
if (st->st_cur->ste_can_see_class_scope) {
    // gh-109118
    PyErr_Format(PyExc_SyntaxError,
                 "Cannot use lambda in annotation scope within class scope");
    ...
}

// For comprehensions (in symtable_handle_comprehension)
if (st->st_cur->ste_can_see_class_scope) {
    // gh-109118
    PyErr_Format(PyExc_SyntaxError,
                 "Cannot use comprehension in annotation scope within class scope");
    ...
}
```

**PyPy**: **Missing** - no check for lambda/comprehension in `can_see_class_scope` contexts.

**Impact**: Code that should error will be accepted:
```python
class C:
    type Alias = lambda: C  # Should error (gh-109118)
    type Alias2 = [x for x in C]  # Should error
```

---

### 6. Different error messages for annotation scopes

**CPython** (symtable.c:2535-2555) provides scope-specific messages:
```c
if (type == AnnotationBlock)
    PyErr_Format(PyExc_SyntaxError, ANNOTATION_NOT_ALLOWED, name);
else if (type == TypeVarBoundBlock)
    PyErr_Format(PyExc_SyntaxError, TYPEVAR_BOUND_NOT_ALLOWED, name);
else if (type == TypeAliasBlock)
    PyErr_Format(PyExc_SyntaxError, TYPEALIAS_NOT_ALLOWED, name);
else if (type == TypeParamBlock)
    PyErr_Format(PyExc_SyntaxError, TYPEPARAM_NOT_ALLOWED, name);
```

Messages:
- TypeVarBound: `"'%s' cannot be used within a TypeVar bound"`
- TypeAlias: `"'%s' cannot be used within a type alias"`
- TypeParam: `"'%s' cannot be used within the definition of a generic"`

**PyPy** (symtable.py:370-377) uses generic messages:
```python
def note_yield(self, yield_node):
    self.error("'yield' not allowed in annotation scope", yield_node)
```

**Impact**: Less specific error messages. Not a behavioral difference but affects user experience.

---

### 7. Missing `.defaults` and `.kwdefaults` parameters

**CPython** (symtable.c:1349-1362):
For functions with defaults, creates `.defaults` and `.kwdefaults` as `DEF_PARAM`:

```c
if (has_defaults) {
    _Py_DECLARE_STR(defaults, ".defaults");
    if (!symtable_add_def(st, &_Py_STR(defaults), DEF_PARAM, ...))
        return 0;
}
if (has_kwdefaults) {
    _Py_DECLARE_STR(kwdefaults, ".kwdefaults");
    if (!symtable_add_def(st, &_Py_STR(kwdefaults), DEF_PARAM, ...))
        return 0;
}
```

**PyPy**: **Missing** - doesn't track these special parameters for type param scopes.

**Impact**: May affect how defaults are passed to the type param scope during code generation.

---

## Minor Differences

### 8. `ste_can_see_class_scope` propagation

**CPython**: Explicitly sets and propagates `ste_can_see_class_scope` flag, passing `class_entry` during the analysis phase via `analyze_block` and `analyze_child_block`.

**PyPy**: Uses `_find_enclosing_class_scope()` at scope creation time and sets `needs_classdict`, but doesn't propagate `class_entry` during the finalization/analysis phase, which contributes to issue #3.

---

### 9. Walrus operator error messages in comprehensions

**CPython** provides different errors for walrus in comprehensions within different annotation scope types:
- `NAMED_EXPR_COMP_IN_CLASS`
- `NAMED_EXPR_COMP_IN_TYPEVAR_BOUND`
- `NAMED_EXPR_COMP_IN_TYPEALIAS`
- `NAMED_EXPR_COMP_IN_TYPEPARAM`

**PyPy** uses a single generic message.

---

## Summary of Required Changes

### High Priority (Behavioral correctness)

1. **Add `SYM_TYPE_PARAM` flag** (or equivalent) and implement:
   - Duplicate type parameter detection in `note_symbol`
   - Track type params during scope finalization
   - Add nonlocal binding restriction check
   - *Test: `test_pep695_duplicate_type_param`*

2. **Implement `class_entry` handling** in `_finalize_name`:
   - Pass enclosing class scope through finalization
   - Check if name is bound/global in class scope
   - Resolve to `GLOBAL_IMPLICIT`/`GLOBAL_EXPLICIT` instead of `FREE`
   - *Test: `test_pep695_class_scope_name_resolution`*

3. **Add lambda/comprehension restrictions** when `needs_classdict` is True:
   - Check in `visit_Lambda`
   - Check in `_visit_comprehension`
   - *Test: `test_pep695_lambda_comprehension_in_class_annotation_scope`*

4. **Add synthetic variables for generic classes**:
   - `.type_params` (LOCAL + USE)
   - `.generic_base` (LOCAL + USE)
   - *No test coverage*

5. **Add `.defaults` and `.kwdefaults` parameters** for type param scopes when functions have default arguments.
   - *No test coverage*

6. **Add nonlocal restriction for type parameters**:
   - *Test: `test_pep695_nonlocal_type_param`*

### Low Priority (Error message quality)

7. **Improve error messages** to be scope-type specific (TypeVarBound vs TypeAlias vs TypeParam).
   - *No test coverage (cosmetic)*

---
