from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.gateway import WrappedDefault, interp2app, unwrap_spec
from pypy.interpreter.typedef import (
    GetSetProperty, TypeDef, generic_new_descr, interp_attrproperty_w)
from pypy.objspace.descroperation import object_getattribute


class W_Super(W_Root):

    def __init__(self, space):
        self.w_starttype = None
        self.w_objtype = None
        self.w_self = None

    def descr_init(self, space, w_starttype=None, w_obj_or_type=None):
        if space.is_none(w_starttype):
            w_starttype, w_obj_or_type = _super_from_frame(space)
        if space.is_none(w_obj_or_type):
            w_type = None  # unbound super object
            w_obj_or_type = space.w_None
        else:
            w_type = _supercheck(space, w_starttype, w_obj_or_type)
        self.w_starttype = w_starttype
        self.w_objtype = w_type
        self.w_self = w_obj_or_type

    def get(self, space, w_obj, w_type=None):
        if self.w_self is None or space.is_w(w_obj, space.w_None):
            return self
        else:
            # if type(self) is W_Super:
            #     XXX write a fast path for this common case
            w_selftype = space.type(self)
            return space.call_function(w_selftype, self.w_starttype, w_obj)

    def getattribute(self, space, w_name):
        name = space.str_w(w_name)
        # only use a special logic for bound super objects and not for
        # getting the __class__ of the super object itself.
        if self.w_objtype is not None and name != '__class__':
            w_value = space.lookup_in_type_starting_at(self.w_objtype,
                                                       self.w_starttype,
                                                       name)
            if w_value is not None:
                w_get = space.lookup(w_value, '__get__')
                if w_get is None:
                    return w_value
                # Only pass 'obj' param if this is instance-mode super
                # (see CPython sourceforge id #743627)
                if self.w_self is self.w_objtype:
                    w_obj = space.w_None
                else:
                    w_obj = self.w_self
                return space.get_and_call_function(w_get, w_value,
                                                   w_obj, self.w_objtype)
        # fallback to object.__getattribute__()
        return space.call_function(object_getattribute(space), self, w_name)

def _super_from_frame(space):
    """super() without args -- fill in from __class__ and first local
    variable on the stack.
    """
    frame = space.getexecutioncontext().gettopframe()
    code = frame.pycode
    if not code:
        raise oefmt(space.w_RuntimeError, "super(): no code object")
    if code.co_argcount == 0:
        raise oefmt(space.w_RuntimeError, "super(): no arguments")
    w_obj = frame.locals_cells_stack_w[0]
    if not w_obj:
        raise oefmt(space.w_RuntimeError, "super(): arg[0] deleted")
    for index, name in enumerate(code.co_freevars):
        if name == "__class__":
            break
    else:
        raise oefmt(space.w_RuntimeError, "super(): __class__ cell not found")
    # a kind of LOAD_DEREF
    cell = frame._getcell(len(code.co_cellvars) + index)
    try:
        w_starttype = cell.get()
    except ValueError:
        raise oefmt(space.w_RuntimeError, "super(): empty __class__ cell")
    return w_starttype, w_obj

def _supercheck(space, w_starttype, w_obj_or_type):
    """Check that the super() call makes sense. Returns a type"""
    w_objtype = space.type(w_obj_or_type)

    if (space.is_true(space.issubtype(w_objtype, space.w_type)) and
        space.is_true(space.issubtype(w_obj_or_type, w_starttype))):
        # special case for class methods
        return w_obj_or_type

    if space.is_true(space.issubtype(w_objtype, w_starttype)):
        # normal case
        return w_objtype

    try:
        w_type = space.getattr(w_obj_or_type, space.wrap('__class__'))
    except OperationError as e:
        if not e.match(space, space.w_AttributeError):
            raise
        w_type = w_objtype

    if space.is_true(space.issubtype(w_type, w_starttype)):
        return w_type
    raise oefmt(space.w_TypeError,
                "super(type, obj): obj must be an instance or subtype of type")

W_Super.typedef = TypeDef(
    'super',
    __new__          = generic_new_descr(W_Super),
    __init__         = interp2app(W_Super.descr_init),
    __thisclass__    = interp_attrproperty_w("w_starttype", W_Super),
    __getattribute__ = interp2app(W_Super.getattribute),
    __get__          = interp2app(W_Super.get),
    __doc__          =     """\
super(type) -> unbound super object
super(type, obj) -> bound super object; requires isinstance(obj, type)
super(type, type2) -> bound super object; requires issubclass(type2, type)

Typical use to call a cooperative superclass method:

class C(B):
    def meth(self, arg):
        super(C, self).meth(arg)"""
)


class W_Property(W_Root):
    _immutable_fields_ = ["w_fget", "w_fset", "w_fdel"]

    def __init__(self, space):
        pass

    @unwrap_spec(w_fget=WrappedDefault(None),
                 w_fset=WrappedDefault(None),
                 w_fdel=WrappedDefault(None),
                 w_doc=WrappedDefault(None))
    def init(self, space, w_fget=None, w_fset=None, w_fdel=None, w_doc=None):
        self.w_fget = w_fget
        self.w_fset = w_fset
        self.w_fdel = w_fdel
        self.w_doc = w_doc
        self.getter_doc = False
        # our __doc__ comes from the getter if we don't have an explicit one
        if (space.is_w(self.w_doc, space.w_None) and
            not space.is_w(self.w_fget, space.w_None)):
            w_getter_doc = space.findattr(self.w_fget, space.wrap('__doc__'))
            if w_getter_doc is not None:
                if type(self) is W_Property:
                    self.w_doc = w_getter_doc
                else:
                    space.setattr(self, space.wrap('__doc__'), w_getter_doc)
                self.getter_doc = True

    def get(self, space, w_obj, w_objtype=None):
        if space.is_w(w_obj, space.w_None):
            return self
        if space.is_w(self.w_fget, space.w_None):
            raise oefmt(space.w_AttributeError, "unreadable attribute")
        return space.call_function(self.w_fget, w_obj)

    def set(self, space, w_obj, w_value):
        if space.is_w(self.w_fset, space.w_None):
            raise oefmt(space.w_AttributeError, "can't set attribute")
        space.call_function(self.w_fset, w_obj, w_value)
        return space.w_None

    def delete(self, space, w_obj):
        if space.is_w(self.w_fdel, space.w_None):
            raise oefmt(space.w_AttributeError, "can't delete attribute")
        space.call_function(self.w_fdel, w_obj)
        return space.w_None

    def getter(self, space, w_getter):
        return self._copy(space, w_getter=w_getter)

    def setter(self, space, w_setter):
        return self._copy(space, w_setter=w_setter)

    def deleter(self, space, w_deleter):
        return self._copy(space, w_deleter=w_deleter)

    def _copy(self, space, w_getter=None, w_setter=None, w_deleter=None):
        if w_getter is None:
            w_getter = self.w_fget
        if w_setter is None:
            w_setter = self.w_fset
        if w_deleter is None:
            w_deleter = self.w_fdel
        if self.getter_doc and w_getter is not None:
            w_doc = space.w_None
        else:
            w_doc = self.w_doc
        w_type = self.getclass(space)
        return space.call_function(w_type, w_getter, w_setter, w_deleter,
                                   w_doc)

    def descr_isabstract(self, space):
        return space.newbool(space.isabstractmethod_w(self.w_fget) or
                             space.isabstractmethod_w(self.w_fset) or
                             space.isabstractmethod_w(self.w_fdel))

W_Property.typedef = TypeDef(
    'property',
    __doc__ = '''\
property(fget=None, fset=None, fdel=None, doc=None) -> property attribute

fget is a function to be used for getting an attribute value, and likewise
fset is a function for setting, and fdel a function for deleting, an
attribute.  Typical use is to define a managed attribute x:
class C(object):
    def getx(self): return self.__x
    def setx(self, value): self.__x = value
    def delx(self): del self.__x
    x = property(getx, setx, delx, "I am the 'x' property.")''',
    __new__ = generic_new_descr(W_Property),
    __init__ = interp2app(W_Property.init),
    __get__ = interp2app(W_Property.get),
    __set__ = interp2app(W_Property.set),
    __delete__ = interp2app(W_Property.delete),
    __isabstractmethod__ = GetSetProperty(W_Property.descr_isabstract),
    fdel = interp_attrproperty_w('w_fdel', W_Property),
    fget = interp_attrproperty_w('w_fget', W_Property),
    fset = interp_attrproperty_w('w_fset', W_Property),
    getter = interp2app(W_Property.getter),
    setter = interp2app(W_Property.setter),
    deleter = interp2app(W_Property.deleter),
)
# This allows there to be a __doc__ of the property type and a __doc__
# descriptor for the instances.
W_Property.typedef.rawdict['__doc__'] = interp_attrproperty_w('w_doc',
                                                              W_Property)
