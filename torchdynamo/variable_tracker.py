import collections
import copy
import functools
import inspect
import itertools
import math
import operator
import re
import types
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Set

import torch.fx
import torch.nn.modules.utils

from . import config
from .allowed_functions import _allowed_function_ids
from .allowed_functions import is_allowed
from .bytecode_transformation import create_instruction
from .guards import GuardBuilder
from .utils import identity
from .utils import is_namedtuple_cls
from .utils import istype
from .utils import make_cell
from .utils import namedtuple_fields
from .utils import proxy_args_kwargs
from .utils import unimplemented
from .variable_source import AttrSource
from .variable_source import GetItemSource
from .variable_source import NNModuleSource
from .variable_source import Source

dict_values = type(dict().values())
odict_values = type(collections.OrderedDict().values())

product = functools.partial(functools.reduce, operator.mul)


def check_constant_args(args, kwargs):
    return all(x.is_python_constant() for x in itertools.chain(args, kwargs.values()))


class MutableLocal:
    """
    Marker used to indicate this (list, iter, etc) was constructed
    in local scope and can be mutated safely in analysis without leaking
    state.
    """

    pass


class VariableTracker:
    """
    Base class for tracked locals and stack values

    VariableTracker instances are immutable and should be copied in
    order to change them.
    """

    @staticmethod
    def propagate(*vars: List[List["VariableTracker"]]):
        guards = set()

        def visit(var):
            if type(var) in (list, tuple, dict_values, odict_values):
                for i in var:
                    visit(i)
            else:
                assert isinstance(var, VariableTracker), typestr(var)
                guards.update(var.guards)

        visit(vars)
        return {
            "guards": guards,
        }

    def clone(self, **kwargs):
        """Shallow copy with some (optional) changes"""
        args = dict(self.__dict__)
        args.update(kwargs)
        return self.__class__(**args)

    @classmethod
    def copy(cls, value):
        """Deeper (but not full) copy, leaving FX and user objects alone"""
        return cls.apply(identity, value)

    @classmethod
    def apply(
        cls, fn: Callable[["VariableTracker"], "VariableTracker"], value, cache=None
    ):
        """
        Walk this object and call fn on all the VariableTracker
        instances to produce a new VariableTracker with the results.
        """
        if cache is None:
            cache = dict()

        idx = id(value)
        if idx in cache:
            return cache[idx]

        if isinstance(value, VariableTracker):
            result = fn(value.clone(**cls.apply(fn, value.__dict__, cache)))
        elif isinstance(value, list):
            result = [cls.apply(fn, v, cache) for v in value]
        elif isinstance(value, collections.OrderedDict):
            result = collections.OrderedDict(
                cls.apply(fn, v, cache) for v in value.items()
            )
        elif isinstance(value, dict):
            result = {k: cls.apply(fn, v, cache) for k, v in value.items()}
        else:
            result = value

        cache[idx] = result
        return result

    def add_guard(self, guard):
        return self.clone(guards=set.union(self.guards, {guard}))

    def add_guards(self, guards):
        assert isinstance(guards, set)
        return self.clone(guards=set.union(self.guards, guards))

    def __str__(self):
        return f"{self.__class__.__name__}()"

    def __repr__(self):
        return str(self)

    def python_type(self):
        raise NotImplementedError(f"{self} has no type")

    def as_python_constant(self):
        """For constants"""
        raise NotImplementedError(f"{self} is not a constant")

    def is_python_constant(self):
        try:
            self.as_python_constant()
            return True
        except NotImplementedError:
            return False

    def can_create_guard(self):
        try:
            self.create_guard(None)
            return True
        except NotImplementedError:
            return False

    def create_guard(self, fn):
        if self.source:
            return self.source.create_guard(fn)
        raise NotImplementedError()

    def replace_guards(self, guards, *fns):
        name = self.source.name()
        new_guards = {g for g in (guards or []) if g.name != name}
        new_guards.update(self.source.create_guard(fn) for fn in fns)
        return new_guards

    def has_const_attr(self, tx, name):
        try:
            return ConstantVariable.is_literal(self.get_const_attr(tx, name))
        except NotImplementedError:
            return False

    def get_const_attr(self, tx, name):
        raise NotImplementedError()

    def get_var_attr(self, tx, name):
        options = VariableTracker.propagate(self)
        if self.source:
            options["source"] = AttrSource(self.source, name)
        return ConstantVariable(self.get_const_attr(tx, name), **options)

    def is_proxy(self):
        try:
            self.as_proxy()
            return True
        except NotImplementedError:
            return False

    def as_proxy(self):
        raise NotImplementedError(str(self))

    def reconstruct(self, codegen):
        raise NotImplementedError()

    def unpack_var_sequence(self, tx):
        raise NotImplementedError()

    def has_unpack_var_sequence(self, tx):
        try:
            self.unpack_var_sequence(tx)
            return True
        except Exception:
            return False

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        raise unimplemented(f"call_function {self} {args} {kwargs}")

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        raise unimplemented(f"call_method {self} {name} {args} {kwargs}")

    def __init__(
        self,
        guards: Optional[Set] = None,
        source: Source = None,
        mutable_local: MutableLocal = None,
    ):
        super(VariableTracker, self).__init__()
        self.guards = guards or set()
        self.source = source
        self.mutable_local = mutable_local


class TensorVariable(VariableTracker):
    """Points to a tensor"""

    @staticmethod
    def propagate_args_kwargs(node):
        def visit(n: torch.fx.Node):
            return n.meta["example_value"]

        return torch.fx.node.map_arg((node.args, node.kwargs), visit)

    @classmethod
    def create(cls, proxy, example_value=None, nnmodule=None, **options):
        assert "example_value" not in proxy.node.meta
        if not config.dynamic_propagation:
            if isinstance(example_value, torch.Tensor):
                options.update(TensorVariable.specialize(example_value))
            return TensorVariable(proxy, **options)

        if example_value is None:
            op = proxy.node.op
            args, kwargs = cls.propagate_args_kwargs(proxy.node)
            if op == "call_function":
                example_value = proxy.node.target(*args, **kwargs)
            elif op == "call_method":
                example_value = getattr(args[0], proxy.node.target)(*args[1:], **kwargs)
            elif op == "call_module":
                assert nnmodule is not None
                example_value = copy.deepcopy(nnmodule)(*args, **kwargs)
            else:
                assert False, op

        if isinstance(example_value, torch.Tensor):
            proxy.node.meta["example_value"] = example_value.clone()
            options.update(TensorVariable.specialize(example_value))
            return TensorVariable(proxy, **options)
        elif isinstance(example_value, tuple):
            unpacked = []
            for i, val in enumerate(example_value):
                unpacked.append(
                    TensorVariable.create(
                        proxy.tracer.create_proxy(
                            "call_function", operator.getitem, (proxy, i), {}
                        ),
                        example_value=val,
                        **options,
                    )
                )
            if istype(example_value, tuple):
                return TupleVariable(unpacked, **options)
            else:
                assert (
                    example_value.__class__.__module__ == "torch.return_types"
                    or hasattr(example_value, "_fields")
                ), "namedtuple?"
                return NamedTupleVariable(unpacked, example_value.__class__, **options)
        else:
            assert (
                False
            ), f"{typestr(example_value)} {proxy.node.op} {proxy.node.target}"

    def __init__(
        self,
        proxy: torch.fx.Proxy,
        dtype=None,
        device=None,
        ndim=None,
        size=None,
        stride=None,
        requires_grad=None,
        **kwargs,
    ):
        assert dtype is not None or not config.dynamic_propagation
        super(TensorVariable, self).__init__(**kwargs)
        self.proxy = proxy
        self.dtype = dtype
        self.device = device
        self.ndim = ndim
        self.size = size
        self.stride = stride
        self.requires_grad = requires_grad

    def as_proxy(self):
        return self.proxy

    def python_type(self):
        return torch.Tensor

    @staticmethod
    def specialize(value: torch.Tensor):
        props = {
            "dtype": value.dtype,
            "device": value.device,
            "ndim": int(value.ndim),
            "requires_grad": value.requires_grad,
        }
        if not config.dynamic_shapes:
            props["size"] = tuple(value.size())
            props["stride"] = tuple(value.stride())
        return props

    def get_var_attr(self, tx, name):
        result = None
        options = VariableTracker.propagate(self)
        if name == "ndim" and self.ndim is not None:
            result = ConstantVariable(self.ndim, **options)
        elif name == "dtype" and self.dtype is not None:
            result = AllowedFunctionOrModuleVariable(self.dtype, **options)
        elif name == "device" and self.device is not None:
            result = AllowedFunctionOrModuleVariable(self.device, **options)
        elif name == "is_cuda" and self.device is not None:
            result = ConstantVariable(self.device.type == "cuda", **options)
        elif name == "shape" and self.size is not None:
            result = ConstantVariable(self.size, **options)
        elif name == "requires_grad" and self.requires_grad is not None:
            result = ConstantVariable(self.requires_grad, **options)
        elif name == "shape" and self.size is None:
            result = self.call_method(tx, "size", [], {})
        elif name == "ndim" and self.ndim is None:
            result = self.call_method(tx, "dim", [], {})

        if result is None:
            raise NotImplementedError()

        return result

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        options = VariableTracker.propagate(self, args, kwargs.values())

        if name == "stride" and self.stride is not None:
            constant_result = ConstantVariable(self.stride, **options)
        elif name == "size" and self.size is not None:
            constant_result = ConstantVariable(self.size, **options)
        elif name == "numel" and self.size is not None:
            constant_result = ConstantVariable(product(self.size), **options)
        elif name in ("ndimension", "dim") and self.ndim is not None:
            constant_result = ConstantVariable(self.ndim, **options)
        elif name == "is_floating_point" and self.dtype is not None:
            constant_result = ConstantVariable(self.dtype.is_floating_point, **options)
        else:
            constant_result = None

        if constant_result:
            assert not kwargs
            if len(args) == 1:
                return constant_result.getitem_const(args[0])
            elif args:
                return TupleVariable(
                    [constant_result.getitem_const(a) for a in args], **options
                )
            return constant_result

        if (
            name == "repeat"
            and not all(
                x.is_python_constant() for x in itertools.chain(args, kwargs.values())
            )
            and not config.dynamic_shapes
        ):
            unimplemented("dynamic Tensor.repeat")

        if name in ("item", "tolist"):
            unimplemented(f"Tensor.{name}")

        if name == "__len__":
            return BuiltinVariable(len).call_function(tx, [self] + args, kwargs)

        return TensorVariable.create(
            tx.create_proxy(
                "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
            ),
            **options,
        )


class NNModuleVariable(VariableTracker):
    def __init__(self, module_type: type, module_key: str, **kwargs):
        super(NNModuleVariable, self).__init__(**kwargs)
        self.module_type = module_type
        self.module_key = module_key
        assert self.source

    def python_type(self):
        return self.module_type

    def unpack_var_sequence(self, tx):
        # implement list/iter/tuple/etc calls
        key = self.module_key
        base = tx.get_submodule(self.module_key)
        options = VariableTracker.propagate([self])
        assert isinstance(
            base, (torch.nn.ModuleList, torch.nn.ParameterList, torch.nn.Sequential)
        ), typestr(base)
        assert self.source
        return [
            tx.add_submodule(
                submod, key, idx, source=GetItemSource(self.source, idx), **options
            )
            for idx, submod in enumerate(base)
        ]

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        options = VariableTracker.propagate(self, args, kwargs.values())
        mod = tx.get_submodule(self.module_key)
        if (
            isinstance(mod, torch.nn.Sequential)
            and mod.__class__.forward is torch.nn.Sequential.forward
        ):
            # unroll Sequential()
            assert not kwargs
            (arg,) = args
            for idx, submod in enumerate(mod):
                tx.call_function(
                    tx.add_submodule(
                        submod,
                        self.module_key,
                        idx,
                        source=NNModuleSource(GetItemSource(self.source, idx)),
                        **options,
                    ),
                    [arg],
                    {},
                )
                arg = tx.pop()
            return arg
        elif is_allowed(mod.__class__):
            return TensorVariable.create(
                proxy=tx.create_proxy(
                    "call_module",
                    self.module_key,
                    *proxy_args_kwargs(args, kwargs),
                ),
                nnmodule=mod,
                **options,
            )
        else:
            forward = mod.__class__.forward
            assert forward is not torch.nn.Module.forward
            return tx.inline_user_function_return(
                UserFunctionVariable(forward, **options),
                [self] + args,
                kwargs,
            )

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        options = VariableTracker.propagate(self, args, kwargs.values())
        key = self.module_key
        module = tx.get_submodule(key)
        if not all(x.is_python_constant() for x in itertools.chain(args, kwargs)):
            raise unimplemented(f"non-const NNModule method {name}")

        def get_kwargs(*names):
            fn = getattr(module, name)
            bound_args = inspect.signature(fn).bind(
                *([x.as_python_constant() for x in args]),
                **{k: v.as_python_constant() for k, v in kwargs.items()},
            )
            bound_args.apply_defaults()
            bound_args = bound_args.arguments
            return {k: bound_args[k] for k in names}

        def wrap_values(items, getsource=AttrSource):
            result = []
            for name, submod in items:
                # layer.0.foo => layer[0].foo
                name = re.sub(r"[.]([0-9]+)([.]|$)", r"[\1]\2", name)
                src = NNModuleSource(getsource(self.source, name))
                result.append(
                    tx.add_submodule(
                        submod,
                        key,
                        name,
                        source=src,
                        **options,
                    )
                )
            return ListIteratorVariable(result, mutable_local=MutableLocal(), **options)

        if name == "children":
            assert not (args or kwargs)
            return wrap_values(module.named_children())
        elif name == "parameters":
            return wrap_values(module.named_parameters(**get_kwargs("recurse")))
        elif name == "values":
            assert not (args or kwargs)
            return wrap_values(module.items(), GetItemSource)
        elif name == "items":
            assert not (args or kwargs)
            result = []
            for name, submod in module.items():
                result.append(
                    TupleVariable(
                        [
                            ConstantVariable(name, **options),
                            tx.add_submodule(
                                submod,
                                key,
                                name,
                                source=NNModuleSource(GetItemSource(self.source, name)),
                                **options,
                            ),
                        ]
                    )
                )
            return ListIteratorVariable(result, mutable_local=MutableLocal(), **options)
        else:
            return super().call_method(tx, name, args, kwargs)


class ConstantVariable(VariableTracker):
    def __init__(self, value, **kwargs):
        super(ConstantVariable, self).__init__(**kwargs)
        self.value = value

    def as_proxy(self):
        return self.value

    def python_type(self):
        return type(self.value)

    def as_python_constant(self):
        return self.value

    def getitem_const(self, arg: VariableTracker):
        return ConstantVariable(
            self.value[arg.as_python_constant()],
            **VariableTracker.propagate([self, arg]),
        )

    @staticmethod
    def is_literal(obj):
        if type(obj) in (int, float, bool, type(None), str):
            return True
        if type(obj) in (list, tuple, set, frozenset):
            return all(ConstantVariable.is_literal(x) for x in obj)
        return False

    def unpack_var_sequence(self, tx):
        try:
            options = VariableTracker.propagate([self])
            return [ConstantVariable(x, **options) for x in self.as_python_constant()]
        except TypeError:
            raise NotImplementedError()

    def get_var_attr(self, tx, name):
        member = getattr(self.value, name)
        if callable(member):
            raise NotImplementedError()
        return ConstantVariable(member, **VariableTracker.propagate(self))


class LambdaVariable(VariableTracker):
    def __init__(self, fn, **kwargs):
        super(LambdaVariable, self).__init__(**kwargs)
        self.fn = fn

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        return self.fn(*args, **kwargs)


class BuiltinVariable(VariableTracker):
    @staticmethod
    @functools.lru_cache(None)
    def _constant_fold_functions():
        fns = {
            abs,
            all,
            any,
            bool,
            callable,
            chr,
            dict,
            divmod,
            float,
            int,
            len,
            list,
            max,
            min,
            ord,
            pow,
            repr,
            round,
            str,
            sum,
            tuple,
            type,
        }
        fns.update(x for x in math.__dict__.values() if isinstance(x, type(math.sqrt)))
        return fns

    def can_constant_fold_through(self):
        return self.fn in self._constant_fold_functions()

    def __init__(self, fn, **kwargs):
        super(BuiltinVariable, self).__init__(**kwargs)
        self.fn = fn

    def __str__(self):
        return f"{self.__class__.__name__}({self.fn.__name__})"

    def python_type(self):
        return type(self.fn)

    def as_python_constant(self):
        return self.fn

    def reconstruct(self, codegen):
        name = self.fn.__name__
        assert self.fn.__module__ == "builtins"
        assert name not in codegen.tx.f_globals, "shadowed global"
        return [codegen.create_load_global(name, add=True)]

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        constant_args = check_constant_args(args, kwargs)
        options = VariableTracker.propagate(self, args, kwargs.values())
        assert isinstance(args, list)
        assert isinstance(kwargs, dict)

        if self.can_constant_fold_through() and constant_args:
            # constant fold
            return ConstantVariable(
                self.as_python_constant()(
                    *[x.as_python_constant() for x in args],
                    **{k: v.as_python_constant() for k, v in kwargs.items()},
                ),
                **options,
            )
        elif self.fn is range and constant_args:
            return RangeVariable(
                value=range(
                    *[x.value for x in args],
                    **{k: v.value for k, v in kwargs.items()},
                ),
                **options,
            )
        elif self.fn is slice:
            assert not kwargs
            return SliceVariable(args, **options)
        elif self.fn is iter and args and isinstance(args[0], BaseListVariable):
            assert not kwargs and len(args) == 1
            return ListIteratorVariable(
                args[0].items, mutable_local=MutableLocal(), **options
            )
        elif self.fn is iter and args and args[0].has_unpack_var_sequence(tx):
            assert not kwargs and len(args) == 1
            return ListIteratorVariable(
                args[0].unpack_var_sequence(tx),
                mutable_local=MutableLocal(),
                **options,
            )
        elif self.fn is zip and all(x.has_unpack_var_sequence(tx) for x in args):
            assert not kwargs
            items = [
                TupleVariable(list(item), **options)
                for item in zip(*[arg.unpack_var_sequence(tx) for arg in args])
            ]
            return TupleVariable(items, **options)
        elif self.fn is enumerate and all(x.has_unpack_var_sequence(tx) for x in args):
            assert not kwargs and len(args) == 1
            items = [
                TupleVariable([ConstantVariable(idx, **options), var], **options)
                for idx, var in enumerate(args[0].unpack_var_sequence(tx))
            ]
            return TupleVariable(items, **options)
        elif self.fn is len:
            assert not kwargs and len(args) == 1
            arg = args[0]
            if isinstance(arg, TensorVariable):
                if arg.size:
                    assert not config.dynamic_shapes
                    return ConstantVariable(arg.size[0], **options)
                else:
                    return TensorVariable.create(
                        tx.create_proxy("call_function", len, (arg.as_proxy(),), {}),
                        **options,
                    )
            elif isinstance(arg, (BaseListVariable, ConstDictVariable)):
                return ConstantVariable(len(arg.items), **options)
            elif isinstance(arg, NNModuleVariable):
                # assuming constant length of nn.ModuleList, etc
                return ConstantVariable(
                    len(tx.get_submodule(arg.module_key)), **options
                )
            elif arg.has_unpack_var_sequence(tx):
                return ConstantVariable(len(arg.unpack_var_sequence(tx)), **options)
            else:
                unimplemented(f"`len` with arg type {arg}")
        elif self.fn is isinstance:
            assert not kwargs and len(args) == 2
            arg, isinstance_type = args
            arg_type = arg.python_type()
            isinstance_type = isinstance_type.as_python_constant()
            try:
                val = issubclass(arg_type, isinstance_type)
            except TypeError:
                val = arg_type is isinstance_type
            return ConstantVariable(val, **options)
        elif self.fn is super:
            assert not kwargs
            assert len(args) in (1, 2)
            return SuperVariable(*args, **options)
        elif self.fn is next and args and isinstance(args[0], ListIteratorVariable):
            val, next_iter = args[0].next_variables()
            tx.replace_all(args[0], next_iter)
            return val.add_guards(self.guards)
        elif self.fn is hasattr and args and isinstance(args[0], NNModuleVariable):
            obj, attr = args
            mod = tx.get_submodule(obj.module_key)
            name = attr.as_python_constant()
            result = hasattr(mod, name)
            return ConstantVariable(result, **options).add_guard(
                NNModuleSource(AttrSource(obj.source, name)).create_guard(
                    GuardBuilder.HASATTR
                )
            )
        else:
            raise super().call_function(tx, args, kwargs)


class ListIteratorVariable(VariableTracker):
    def __init__(self, items, index: int = 0, **kwargs):
        super(ListIteratorVariable, self).__init__(**kwargs)
        assert isinstance(items, list)
        assert all(isinstance(x, VariableTracker) for x in items)
        self.items = items
        self.index = index

    def next_variables(self):
        assert self.mutable_local
        if self.index >= len(self.items):
            raise StopIteration()
        return self.items[self.index].add_guards(self.guards), ListIteratorVariable(
            self.items,
            self.index + 1,
            mutable_local=MutableLocal(),
            **VariableTracker.propagate([self]),
        )

    def as_python_constant(self):
        if self.index > 0:
            raise NotImplementedError()
        return iter([x.as_python_constant() for x in self.items])

    def unpack_var_sequence(self, tx):
        return [x.add_guards(self.guards) for x in self.items[self.index :]]

    def reconstruct(self, codegen):
        remaining_items = self.items[self.index :]
        codegen.foreach(remaining_items)
        return [
            create_instruction("BUILD_TUPLE", len(remaining_items)),
            create_instruction("GET_ITER"),
        ]


class GetAttrVariable(VariableTracker):
    def __init__(self, obj, name, **kwargs):
        super(GetAttrVariable, self).__init__(**kwargs)
        assert isinstance(obj, VariableTracker)
        assert isinstance(name, str)
        self.obj = obj
        self.name = name

    def __str__(self):
        return f"{self.__class__.__name__}({self.obj}, {self.name})"

    def as_proxy(self):
        return getattr(self.obj.as_proxy(), self.name)

    def get_const_attr(self, tx, name):
        if not isinstance(self.obj, NNModuleVariable):
            raise NotImplementedError()
        step1 = tx.get_submodule(self.obj.module_key)
        if self.name not in step1.__dict__:
            raise NotImplementedError()
        step2 = inspect.getattr_static(step1, self.name)
        if name not in step2.__dict__:
            raise NotImplementedError()
        return inspect.getattr_static(step2, name)

    def reconstruct(self, codegen):
        codegen(self.obj)
        return [codegen.create_load_attr(self.name)]

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        return self.obj.call_method(tx, self.name, args, kwargs).add_guards(self.guards)


class BaseListVariable(VariableTracker):
    def __init__(self, items, **kwargs):
        super(BaseListVariable, self).__init__(**kwargs)
        assert isinstance(items, list)
        assert all(isinstance(x, VariableTracker) for x in items)
        self.items = items
        self.guards = set(self.guards)
        for item in self.items:
            self.guards.update(item.guards)

    def _as_proxy(self):
        return [x.as_proxy() for x in self.items]

    def as_python_constant(self):
        return self.python_type()([x.as_python_constant() for x in self.items])

    def as_proxy(self):
        return self.python_type()(self._as_proxy())

    def getitem_const(self, arg: VariableTracker):
        index = arg.as_python_constant()
        if isinstance(index, slice):
            return self.clone(items=self.items[index]).add_guards(arg.guards)
        else:
            assert isinstance(index, int)
            return self.items[index].add_guards(self.guards).add_guards(arg.guards)

    def unpack_var_sequence(self, tx):
        return list(self.items)


class RangeVariable(BaseListVariable):
    def __init__(self, value, items=None, guards=None, **kwargs):
        if items is None:
            items = [ConstantVariable(x, guards=guards) for x in value]
        super().__init__(items, guards=guards, **kwargs)
        self.value = value

    def python_type(self):
        return range

    def as_python_constant(self):
        return self.value

    def reconstruct(self, codegen):
        assert "range" not in codegen.tx.f_globals
        range_fn = codegen.tx.create_load_global("range", add=True)
        if self.value.step == 1:
            if self.value.start == 0:
                return [
                    range_fn,
                    codegen.tx.create_load_const(self.value.stop),
                    create_instruction("CALL_FUNCTION", 1),
                ]
            return [
                range_fn,
                codegen.tx.create_load_const(self.value.start),
                codegen.tx.create_load_const(self.value.stop),
                create_instruction("CALL_FUNCTION", 2),
            ]
        return [
            range_fn,
            codegen.tx.create_load_const(self.value.start),
            codegen.tx.create_load_const(self.value.stop),
            codegen.tx.create_load_const(self.value.step),
            create_instruction("CALL_FUNCTION", 3),
        ]


class ListVariable(BaseListVariable):
    def python_type(self):
        return list

    def reconstruct(self, codegen):
        codegen.foreach(self.items)
        return [create_instruction("BUILD_LIST", len(self.items))]

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        options = VariableTracker.propagate(self, args, kwargs.values())
        if name == "append" and self.mutable_local:
            assert not kwargs
            (arg,) = args
            tx.replace_all(
                self,
                ListVariable(
                    self.items + [arg], mutable_local=MutableLocal(), **options
                ),
            )
            return ConstantVariable(None, **options)
        elif name == "insert" and self.mutable_local:
            assert not kwargs
            idx, value = args
            items = list(self.items)
            items.insert(idx.as_python_constant(), value)
            tx.replace_all(
                self,
                ListVariable(items, mutable_local=MutableLocal(), **options),
            )
            return ConstantVariable(None, **options)
        elif name == "pop" and self.mutable_local:
            assert not kwargs
            items = list(self.items)
            result = items.pop(*[a.as_python_constant() for a in args])
            tx.replace_all(
                self,
                ListVariable(items, mutable_local=MutableLocal(), **options),
            )
            return result
        else:
            return super().call_method(tx, name, args, kwargs)


class TupleVariable(BaseListVariable):
    def python_type(self):
        return tuple

    def reconstruct(self, codegen):
        codegen.foreach(self.items)
        return [create_instruction("BUILD_TUPLE", len(self.items))]


class NamedTupleVariable(TupleVariable):
    def __init__(self, items, tuple_cls, **kwargs):
        super().__init__(items, **kwargs)
        self.tuple_cls = tuple_cls

    def python_type(self):
        return self.tuple_cls

    def reconstruct(self, codegen):
        create_fn = getattr(self.tuple_cls, "_make", self.tuple_cls)
        codegen.output.append(codegen._create_load_const(create_fn))
        codegen.foreach(self.items)
        return [
            create_instruction("BUILD_TUPLE", len(self.items)),
            create_instruction("CALL_FUNCTION", 1),
        ]

    def get_var_attr(self, tx, name):
        fields = namedtuple_fields(self.tuple_cls)
        if name not in fields:
            unimplemented(f"NamedTupleVariable.{name}")
        return self.items[fields.index(name)].add_guards(self.guards)


class SliceVariable(BaseListVariable):
    def __init__(self, items, **kwargs):
        start, stop, step = [ConstantVariable(None)] * 3
        if len(items) == 1:
            (stop,) = items
        elif len(items) == 2:
            start, stop = items
        elif len(items) == 3:
            start, stop, step = items
        else:
            assert False
        super().__init__([start, stop, step], **kwargs)

    def as_proxy(self):
        return slice(*self._as_proxy())

    def python_type(self):
        return slice

    def as_python_constant(self):
        return slice(*[x.as_python_constant() for x in self.items])

    def reconstruct(self, codegen):
        codegen.foreach(self.items)
        return [create_instruction("BUILD_SLICE", len(self.items))]

    def get_var_attr(self, tx, name):
        fields = ["start", "stop", "step"]
        if name not in fields:
            unimplemented(f"slice.{name}")
        return self.items[fields.index(name)].add_guards(self.guards)


class ConstDictVariable(VariableTracker):
    def __init__(self, items, **kwargs):
        super(ConstDictVariable, self).__init__(**kwargs)
        if not isinstance(items, collections.OrderedDict):
            assert isinstance(items, dict)
            items = collections.OrderedDict((k, items[k]) for k in sorted(items.keys()))
        self.items = items

    def as_proxy(self):
        return {k: v.as_proxy() for k, v in self.items.items()}

    def python_type(self):
        return dict

    def reconstruct(self, codegen):
        if len(self.items) == 0:
            return [create_instruction("BUILD_MAP", 0)]
        keys = tuple(sorted(self.items.keys()))
        for key in keys:
            codegen(self.items[key])
        return [
            codegen.create_load_const(keys),
            create_instruction("BUILD_CONST_KEY_MAP", len(keys)),
        ]

    def getitem_const(self, arg: VariableTracker):
        index = arg.as_python_constant()
        return self.items[index].add_guards(self.guards).add_guards(arg.guards)

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        options = VariableTracker.propagate(self, args, kwargs.values())
        val = self.items
        if name == "items":
            assert not (args or kwargs)
            return TupleVariable(
                [
                    TupleVariable([ConstantVariable(k, **options), v], **options)
                    for k, v in val.items()
                ],
                **options,
            )
        elif name == "keys":
            assert not (args or kwargs)
            return TupleVariable(
                [ConstantVariable(k, **options) for k in val.keys()],
                **options,
            )

        elif name == "values":
            assert not (args or kwargs)
            return TupleVariable(list(val.values()), **options)
        elif (
            name == "__setattr__"
            and args
            and args[0].is_python_constant()
            and self.mutable_local
        ):
            assert not kwargs and len(args) == 2
            newval = collections.OrderedDict(val)
            newval[args[0].as_python_constant()] = args[1]
            tx.replace_all(
                self, ConstDictVariable(newval, mutable_local=MutableLocal(), **options)
            )
        else:
            return super().call_method(tx, name, args, kwargs)


class BaseUserFunctionVariable(VariableTracker):
    def get_filename(self):
        return self.get_code().co_filename

    def get_name(self):
        return self.get_code().co_name

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        return tx.inline_user_function_return(self, self.self_args() + args, kwargs)


class UserFunctionVariable(BaseUserFunctionVariable):
    """Some unsupported user-defined global function"""

    def __init__(self, fn, **kwargs):
        super(UserFunctionVariable, self).__init__(**kwargs)
        assert isinstance(
            fn, types.FunctionType
        ), f"expected FunctionType {typestr(fn)} {fn}"
        self.fn: types.FunctionType = fn

    def self_args(self):
        return []

    def get_function(self):
        return self.fn

    def get_code(self):
        return self.fn.__code__

    def python_type(self):
        return types.FunctionType

    def has_closure(self):
        return getattr(self.fn, "__closure__", None) is not None

    def has_self(self):
        return getattr(self.fn, "__self__", None) is not None

    def get_globals(self):
        return self.fn.__globals__

    def bind_args(self, parent, args, kwargs):
        options = VariableTracker.propagate([self])

        def wrap(val):
            if ConstantVariable.is_literal(val):
                return ConstantVariable(val, **options)
            else:
                return val

        fn: types.FunctionType = self.fn
        fake_func = types.FunctionType(
            fn.__code__,
            fn.__globals__,
            fn.__name__,
            tuple(map(wrap, fn.__defaults__ or [])),
            fn.__closure__,
        )
        if fn.__kwdefaults__:
            fake_func.__kwdefaults__ = {
                k: wrap(v) for k, v in fn.__kwdefaults__.items()
            }

        bound = inspect.signature(fake_func).bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments.items())

    def export_freevars(self, parent, child):
        pass


class UserMethodVariable(UserFunctionVariable):
    """Some unsupported user-defined method"""

    def __init__(self, fn, obj, **kwargs):
        super(UserMethodVariable, self).__init__(fn=fn, **kwargs)
        self.obj = obj

    def __str__(self):
        return f"{self.__class__.__name__}({self.fn}, {self.obj})"

    def self_args(self):
        return [self.obj]

    def python_type(self):
        return types.MethodType

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        if isinstance(self.obj, NNModuleVariable) and getattr(
            self.fn, "__module__", ""
        ).startswith("torch.nn."):
            return self.obj.call_method(tx, self.fn.__name__, args, kwargs).add_guards(
                self.guards
            )
        return super().call_function(tx, args, kwargs)


class NestedUserFunctionVariable(BaseUserFunctionVariable):
    def __init__(
        self,
        fn_name,
        code,
        f_globals,
        defaults,
        kwdefaults,
        annotations,
        closure,
        **kwargs,
    ):
        super(NestedUserFunctionVariable, self).__init__(**kwargs)
        assert isinstance(fn_name.as_python_constant(), str)
        assert isinstance(code.as_python_constant(), types.CodeType)
        assert isinstance(f_globals, dict)
        self.fn_name = fn_name
        self.code = code
        self.f_globals = f_globals
        self.defaults = defaults
        self.kwdefaults = kwdefaults
        self.annotations = annotations
        self.closure = closure

    def self_args(self):
        return []

    def get_code(self):
        return self.code.as_python_constant()

    def get_function(self):
        if self.closure:
            raise NotImplementedError()
        func = types.FunctionType(
            self.code.as_python_constant(),
            self.f_globals,
            self.fn_name.as_python_constant(),
        )
        if self.defaults:
            func.__defaults__ = self.defaults.as_python_constant()
        if self.kwdefaults:
            func.__kwdefaults__ = self.kwdefaults.as_python_constant()
        if self.annotations:
            func.__annotations__ = self.annotations.as_python_constant()
        return func

    def has_closure(self):
        return self.closure is not None

    def has_self(self):
        return False

    def get_globals(self):
        return self.f_globals

    def bind_args(self, parent, args, kwargs):
        closure_items = []
        if self.closure:
            closure_items = [
                parent.symbolic_locals.get(c.name, None) for c in self.closure.items
            ]

        code = self.get_code()
        func = types.FunctionType(
            code,
            self.f_globals,
            self.fn_name.as_python_constant(),
            self.defaults.items if self.defaults else None,
            tuple(map(make_cell, closure_items)),
        )
        if self.kwdefaults:
            func.__kwdefaults__ = self.kwdefaults.items

        bound = inspect.signature(func).bind(*args, **kwargs)
        bound.apply_defaults()
        result = dict(bound.arguments.items())

        for idx, var in enumerate(code.co_freevars):
            assert self.closure.items[idx].name == var
            assert var not in result
            result[var] = closure_items[idx]

        return result

    def export_freevars(self, parent, child):
        code = self.get_code()
        for var in code.co_freevars:
            if var in child.symbolic_locals:
                parent.symbolic_locals[var] = child.symbolic_locals[var]

    def reconstruct(self, codegen):
        flags = 0x00
        if self.defaults:
            flags |= 0x01
            codegen(self.defaults)
        if self.kwdefaults:
            flags |= 0x02
            codegen(self.kwdefaults)
        if self.annotations:
            flags |= 0x04
            codegen(self.annotations)
        if self.closure:
            flags |= 0x08
            codegen(self.closure)
        codegen(self.code)
        codegen(self.fn_name)
        return [create_instruction("MAKE_FUNCTION", flags)]


class UserDefinedClassVariable(VariableTracker):
    def __init__(self, value, **kwargs):
        super(UserDefinedClassVariable, self).__init__(**kwargs)
        self.value = value

    def as_python_constant(self):
        return self.value

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        if is_namedtuple_cls(self.value):
            fields = namedtuple_fields(self.value)
            items = list(args)
            items.extend([None] * (len(fields) - len(items)))
            for name, value in kwargs.items():
                assert name in fields
                items[fields.index(name)] = value
            assert all(x is not None for x in items)
            return NamedTupleVariable(
                items, self.value, **VariableTracker.propagate(self, items)
            )
        return super().call_function(tx, args, kwargs)


class AllowedFunctionOrModuleVariable(VariableTracker):
    """Points to a module or method in torch.*"""

    def __init__(self, value, **kwargs):
        super(AllowedFunctionOrModuleVariable, self).__init__(**kwargs)
        self.value = value

        # the remainder of this is just optional debug checks
        try:
            self_should_be_none = getattr(self.value, "__self__", None)
        except RuntimeError as e:
            assert "No such operator" in str(e), str(e)
            self_should_be_none = None

        # assert "_ntuple.<locals>.parse" not in str(value)

        if self_should_be_none is None:
            pass
        elif isinstance(self_should_be_none, types.ModuleType):
            # weird ones like torch.nn.functional.avg_pool2d have __self__
            name = self_should_be_none.__name__
            assert re.match(r"^(torch|math)([.]|$)", name), f"__self__ set to {name}"
        elif isinstance(
            self_should_be_none, type(torch._C._get_tracing_state.__self__)
        ):
            # some _C functions have __self__ as a null capsule
            pass
        else:
            assert False, f"{value} found with __self__ set"

    def unique_var_name(self):
        name = _allowed_function_ids().get(
            id(self.value), f"allowed_fn_{id(self.value)}"
        )
        return "__" + re.sub(r"[^a-zA-Z0-9_]+", "_", name)

    def reconstruct(self, codegen):
        return codegen.setup_globally_cached(self.unique_var_name(), self.value)

    def as_proxy(self):
        return self.value

    def python_type(self):
        if isinstance(self.value, (torch.Tensor, torch.nn.Module)):
            return type(self.value)
        return super().python_type()

    def as_python_constant(self):
        return self.value

    def can_constant_fold_through(self):
        if self.value in (torch.is_tensor, torch.is_floating_point):
            return True
        return getattr(self.value, "__module__", None) == "math"

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        constant_args = check_constant_args(args, kwargs)
        options = VariableTracker.propagate(self, args, kwargs.values())

        if self.value in config.constant_functions:
            assert not args and not kwargs
            return ConstantVariable(config.constant_functions[self.value], **options)
        elif self.can_constant_fold_through() and constant_args:
            # constant fold
            return ConstantVariable(
                self.as_python_constant()(
                    *[x.as_python_constant() for x in args],
                    **{k: v.as_python_constant() for k, v in kwargs.items()},
                ),
                **options,
            )
        elif istype(self.value, type) and issubclass(self.value, torch.nn.Module):
            if self.value is torch.nn.Softmax:
                return self._call_softmax(tx, args, kwargs, options)
            else:
                unimplemented(f"construct nn.Module: {self.value.__name__}")
        elif (
            self.value in (torch.is_tensor, torch.is_floating_point)
            and isinstance(args[0], TensorVariable)
            and args[0].dtype is not None
        ):
            if self.value is torch.is_tensor:
                return ConstantVariable(True, **options)
            elif self.value is torch.is_floating_point:
                return ConstantVariable(args[0].dtype.is_floating_point, **options)
            else:
                assert False
        elif (
            self.value is torch.numel
            and isinstance(args[0], TensorVariable)
            and args[0].size is not None
        ):
            return ConstantVariable(product(args[0].size), **options)
        elif self.value in (
            torch.nn.modules.utils._single,
            torch.nn.modules.utils._pair,
            torch.nn.modules.utils._triple,
            torch.nn.modules.utils._quadruple,
            torch.nn.modules.utils._ntuple,
        ):
            return self._call_ntuple(tx, args, kwargs, options)
        elif not config.dynamic_shapes and self.is_dynamic_shapes(args, kwargs):
            unimplemented(f"dynamic shapes: {self.value.__name__}")
        else:
            return TensorVariable.create(
                proxy=tx.create_proxy(
                    "call_function", self.value, *proxy_args_kwargs(args, kwargs)
                ),
                **options,
            )

    def is_dynamic_shapes(self, args, kwargs):
        """Check for dynamic shapes when shape specialization is enabled"""
        # TODO(jansel): need to get a complete list
        if self.value in (
            torch.nonzero,
            torch.unique,
            torch.unique_consecutive,
        ):
            return True

        if self.value in (
            torch.arange,
            torch.repeat_interleave,
        ):
            none = ConstantVariable(None)

            def has_non_const(it):
                return not all(x.is_python_constant() for x in it)

            def arange(start=none, end=none, step=none, **kwargs):
                return has_non_const([start, end, step])

            def repeat_interleave(input, repeats, dim=none, **kwargs):
                return has_non_const([repeats])

            return locals()[self.value.__name__](*args, **kwargs)

        return False

    def _call_softmax(self, tx, args, kwargs, options):
        """rewrite the pattern nn.Softmax(dim=-1)(x) to F.softmax(x, -1)"""
        dim = args[0] if args else kwargs.get("dim", ConstantVariable(None))

        def fake_softmax(input):
            return TensorVariable.create(
                proxy=tx.create_proxy(
                    "call_function",
                    torch.nn.functional.softmax,
                    *proxy_args_kwargs([input, dim], {}),
                ),
                **VariableTracker.propagate([self, dim, input]),
            )

        return LambdaVariable(fake_softmax, **options)

    def _call_ntuple(self, tx, args, kwargs, options):
        """inline behavior of torch.nn.modules.utils._ntuple"""
        if self.value is torch.nn.modules.utils._ntuple:
            count = args[0].as_python_constant()
        else:
            count = self.value.__closure__[0].cell_contents
        assert isinstance(count, int)

        def handle_ntuple(value):
            if value.has_unpack_var_sequence(tx):
                return TupleVariable(
                    list(value.unpack_var_sequence(tx)),
                    **VariableTracker.propagate(self, value, args, kwargs.values()),
                )
            else:
                # constant prop through it
                return ConstantVariable(
                    torch.nn.modules.utils._ntuple(count)(value.as_python_constant()),
                    **VariableTracker.propagate(self, value, args, kwargs.values()),
                )

        if self.value is torch.nn.modules.utils._ntuple:
            return LambdaVariable(handle_ntuple, **options)
        else:
            return handle_ntuple(args[0])


class PythonModuleVariable(VariableTracker):
    def __init__(self, value: types.ModuleType, **kwargs):
        super(PythonModuleVariable, self).__init__(**kwargs)
        self.value = value

    def python_type(self):
        return types.ModuleType


class UnsupportedVariable(VariableTracker):
    """
    Mostly objects of defined type.  Catch-all for something where we only know the type.
    """

    def __init__(self, value, value_type=None, **kwargs):
        super(UnsupportedVariable, self).__init__(**kwargs)
        self.value = value
        self.value_type = value_type or type(value)

    def __str__(self):
        return f"{self.__class__.__name__}({self.value_type.__name__})"

    def python_type(self):
        return self.value_type

    """
    def get_const_attr(self, tx, name):
        if name not in getattr(self.value, "__dict__", {}):
            raise NotImplementedError()
        subobj = inspect.getattr_static(self.value, name)
        assert id(subobj) == id(self.value.__dict__[name])
        if not ConstantVariable.is_literal(subobj):
            raise NotImplementedError()
        return subobj
    """

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        if name not in getattr(self.value, "__dict__", {}):
            options = VariableTracker.propagate(self, args, kwargs.values())
            method = inspect.getattr_static(type(self.value), name)
            # TODO(jansel): add a guard to check for monkey patching?
            return UserMethodVariable(method, self, **options).call_function(
                tx, args, kwargs
            )
        else:
            return super().call_method(tx, name, args, kwargs)


class SuperVariable(VariableTracker):
    def __init__(self, typevar, objvar=None, **kwargs):
        super(SuperVariable, self).__init__(**kwargs)
        self.typevar = typevar
        self.objvar = objvar

    def reconstruct(self, codegen):
        codegen(BuiltinVariable(super))
        codegen(self.typevar)
        if self.objvar is not None:
            codegen(self.objvar)
            return [create_instruction("CALL_FUNCTION", 2)]
        else:
            return [create_instruction("CALL_FUNCTION", 1)]

    def get_const_attr(self, tx, name):
        assert self.objvar, "1-arg super not implemented"
        search_type = self.typevar.as_python_constant()
        # TODO(jansel): there is a small chance this could trigger user code, prevent that
        return getattr(super(search_type, self.objvar.python_type()), name)

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        options = VariableTracker.propagate(
            self, args, kwargs.values(), self.objvar, self.typevar
        )
        inner_fn = self.get_const_attr(self, name)
        if not isinstance(inner_fn, types.FunctionType):
            unimplemented(f"non-function super: {typestr(inner_fn)}")
        return UserFunctionVariable(inner_fn, **options).call_function(
            tx, [self.objvar] + args, kwargs
        )


class UnknownVariable(VariableTracker):
    """
    It could be anything!
    """


class ClosureVariable(UnknownVariable):
    def __init__(self, name, **kwargs):
        super(ClosureVariable, self).__init__(**kwargs)
        self.name = name

    def reconstruct(self, codegen):
        return [codegen.create_load_closure(self.name)]


def typestr(*objs):
    if len(objs) == 1:
        (obj,) = objs
        if isinstance(obj, VariableTracker):
            return str(obj)
        else:
            return type(obj).__name__
    else:
        return " ".join(map(typestr, objs))
