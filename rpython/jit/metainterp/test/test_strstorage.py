import py
import struct
from rpython.rtyper.lltypesystem import lltype, rffi
from rpython.rlib.strstorage import str_storage_getitem
from rpython.rlib.test.test_strstorage import BaseStrStorageTest
from rpython.jit.codewriter import longlong
from rpython.jit.metainterp.history import getkind
from rpython.jit.metainterp.test.support import LLJitMixin

class TestStrStorage(BaseStrStorageTest, LLJitMixin):

    # for the individual tests see
    # ====> ../../../rlib/test/test_strstorage.py

    def str_storage_getitem(self, TYPE, buf, offset):
        def f():
            return str_storage_getitem(TYPE, buf, offset)
        res = self.interp_operations(f, [], supports_singlefloats=True)
        #
        kind = getkind(TYPE)[0] # 'i' or 'f'
        self.check_operations_history({'getarrayitem_gc_%s' % kind: 1,
                                       'finish': 1})
        #
        if TYPE == lltype.SingleFloat:
            # interp_operations returns the int version of r_singlefloat, but
            # our tests expects to receive an r_singlefloat: let's convert it
            # back!
            return longlong.int2singlefloat(res)
        return res


    def test_force_virtual_str_storage(self):
        size = rffi.sizeof(lltype.Signed)
        def f(val):
            x = chr(val) + '\x00'*(size-1)
            return str_storage_getitem(lltype.Signed, x, 0)
        res = self.interp_operations(f, [42], supports_singlefloats=True)
        assert res == 42
        self.check_operations_history({
            'newstr': 1,              # str forcing
            'strsetitem': 1,          # str forcing
            'call_pure_r': 1,         # str forcing (copystrcontent)
            'guard_no_exception': 1,  # str forcing
            'getarrayitem_gc_i': 1,   # str_storage_getitem
            'finish': 1
            })
