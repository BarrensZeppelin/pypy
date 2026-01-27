# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PyPy is a Python interpreter written in RPython (a restricted subset of Python) that uses the RPython translation toolchain to compile into efficient C code. The codebase has clear layer separation between the translation framework and the interpreter.

## Common Commands

### Running Tests

PyPy uses test-driven development extensively. Tests run on Python 2.7 (since RPython is a Python 2 dialect):

```bash
# Run a specific test file (untranslated, runs on CPython/PyPy2)
python2 pytest.py pypy/interpreter/test/test_pyframe.py

# Run all tests in a directory
python2 pytest.py pypy/interpreter/

# (uncommon) Run app-level tests directly on host Python (Python 3)
python3 -m pytest -D pypy/interpreter/test/apptest_pyframe.py

# Run mixed-level tests on host interpreter
python2 pytest.py -A pypy/module/cpyext/test --python=path/to/pypy3
```

Key pytest flags:
- `-D` / `--direct-apptest`: Run `apptest_*.py` files directly on host Python
- `-A` / `--runappdirect`: Run mixed tests on host interpreter
- When running on Python 3, the `-D` flag is mandatory

Test naming conventions:
- `test_*.py`: Untranslated tests (run before translation on CPython/PyPy2)
- `apptest_*.py`: Application-level tests (run on translated PyPy or (more common) interpreted PyPy on top of host Python)

### Building PyPy

Translation requires Python 2.7 with CFFI installed. Using PyPy2.7 is 2-3x faster than CPython:

```bash
# From pypy/goal directory, translate with JIT (recommended)
cd pypy/goal
pypy2.7 ../../rpython/bin/rpython --opt=jit

# Translate without JIT
pypy2.7 ../../rpython/bin/rpython --opt=2

# Debug build (after initial translation)
make lldebug    # with debug info
make lldebug0   # debug info + no optimization
```

Translation requires ~6GB RAM on 64-bit systems and takes ~20 minutes with PyPy2.7.

After translation, build CFFI modules:
```bash
cd pypy/goal
PYTHONPATH=../.. ./pypy3-c ../../lib_pypy/pypy_tools/build_cffi_imports.py
```

### Interactive Development

```bash
# Run untranslated PyPy interpreter (for debugging)
python2 pypy/bin/pyinteractive.py

# Enable bytecode tracing in the interpreter
>>>> __pytrace__ = 1
```

## Architecture

### Directory Structure

```
rpython/          # Translation toolchain (compiles RPython → C)
├── annotator/    # Type inference
├── rtyper/       # Low-level type conversion
├── flowspace/    # Control flow graph building
├── jit/          # JIT compiler framework
│   ├── metainterp/   # Tracing JIT core
│   └── backend/      # Machine code backends (x86, ARM, etc.)
├── memory/       # Garbage collection (incminimark is default)
├── rlib/         # Runtime library utilities
└── translator/   # Translation driver

pypy/             # Python interpreter (written in RPython)
├── interpreter/  # Bytecode interpreter
│   ├── pyframe.py      # Frame/execution context
│   ├── pyopcode.py     # Bytecode operations
│   ├── gateway.py      # Interp/app level bridge
│   ├── pyparser/       # Python parser
│   └── astcompiler/    # AST → bytecode compiler
├── objspace/std/ # Standard object space (type implementations)
├── module/       # Built-in modules
└── goal/         # Translation targets (targetpypystandalone.py)

lib_pypy/         # Pure Python modules (PyPy-specific)
lib-python/       # CPython standard library (patched for PyPy)
```

### Key Concepts

**Interpreter-level vs Application-level**: Interpreter-level code manipulates wrapped objects (`w_` prefix) through the object space. Application-level is regular Python code.

```python
# Application-level
a + b

# Interpreter-level equivalent
space.add(w_a, w_b)
```

**Object Space**: All Python object operations go through the object space (`pypy/objspace/std/`). The space provides operations like `add`, `getattr`, `call`, and object creation (`newint`, `newlist`).

**Wrapped Objects**: Python objects at interpreter-level are "wrapped" and prefixed with `w_`. Use `space.int_w(w_x)` to unwrap to interpreter-level values.

**RPython**: A restricted Python subset that can be statically analyzed. Code is normal Python during import, but must be "static enough" for translation. Full Python is allowed during initialization; restrictions apply at runtime.

**Translation Pipeline**: Source → Flow Graphs → Type Annotation → RTyping → Optimization → C Generation → Compilation

### Key Files

- `pypy/goal/targetpypystandalone.py`: Translation target definition
- `pypy/interpreter/pyframe.py`: Frame class (execution context)
- `pypy/interpreter/pyopcode.py`: Bytecode operation implementations
- `pypy/interpreter/gateway.py`: Bridge between interp/app levels (`interp2app`, `applevel`)
- `pypy/objspace/std/objspace.py`: Main object space
- `rpython/bin/rpython`: Translation entry point

## Coding Conventions

- Variables holding wrapped objects use `w_` prefix
- Follow PEP 8; when in doubt, match existing code style
- Application-level code is preferred when possible (simpler, more maintainable)
- Application exceptions are wrapped in `OperationError`
