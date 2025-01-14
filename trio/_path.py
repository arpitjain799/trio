# type: ignore

from functools import wraps, partial
import os
import types
import pathlib

import trio
from trio._util import async_wraps, Final


# re-wrap return value from methods that return new instances of pathlib.Path
def rewrap_path(value):
    if isinstance(value, pathlib.Path):
        value = Path(value)
    return value


def _forward_factory(cls, attr_name, attr):
    @wraps(attr)
    def wrapper(self, *args, **kwargs):
        attr = getattr(self._wrapped, attr_name)
        value = attr(*args, **kwargs)
        return rewrap_path(value)

    return wrapper


def _forward_magic(cls, attr):
    sentinel = object()

    @wraps(attr)
    def wrapper(self, other=sentinel):
        if other is sentinel:
            return attr(self._wrapped)
        if isinstance(other, cls):
            other = other._wrapped
        value = attr(self._wrapped, other)
        return rewrap_path(value)

    return wrapper


def iter_wrapper_factory(cls, meth_name):
    @async_wraps(cls, cls._wraps, meth_name)
    async def wrapper(self, *args, **kwargs):
        meth = getattr(self._wrapped, meth_name)
        func = partial(meth, *args, **kwargs)
        # Make sure that the full iteration is performed in the thread
        # by converting the generator produced by pathlib into a list
        items = await trio.to_thread.run_sync(lambda: list(func()))
        return (rewrap_path(item) for item in items)

    return wrapper


def thread_wrapper_factory(cls, meth_name):
    @async_wraps(cls, cls._wraps, meth_name)
    async def wrapper(self, *args, **kwargs):
        meth = getattr(self._wrapped, meth_name)
        func = partial(meth, *args, **kwargs)
        value = await trio.to_thread.run_sync(func)
        return rewrap_path(value)

    return wrapper


def classmethod_wrapper_factory(cls, meth_name):
    @classmethod
    @async_wraps(cls, cls._wraps, meth_name)
    async def wrapper(cls, *args, **kwargs):
        meth = getattr(cls._wraps, meth_name)
        func = partial(meth, *args, **kwargs)
        value = await trio.to_thread.run_sync(func)
        return rewrap_path(value)

    return wrapper


class AsyncAutoWrapperType(Final):
    def __init__(cls, name, bases, attrs):
        super().__init__(name, bases, attrs)

        cls._forward = []
        type(cls).generate_forwards(cls, attrs)
        type(cls).generate_wraps(cls, attrs)
        type(cls).generate_magic(cls, attrs)
        type(cls).generate_iter(cls, attrs)

    def generate_forwards(cls, attrs):
        # forward functions of _forwards
        for attr_name, attr in cls._forwards.__dict__.items():
            if attr_name.startswith("_") or attr_name in attrs:
                continue

            if isinstance(attr, property):
                cls._forward.append(attr_name)
            elif isinstance(attr, types.FunctionType):
                wrapper = _forward_factory(cls, attr_name, attr)
                setattr(cls, attr_name, wrapper)
            else:
                raise TypeError(attr_name, type(attr))

    def generate_wraps(cls, attrs):
        # generate wrappers for functions of _wraps
        for attr_name, attr in cls._wraps.__dict__.items():
            # .z. exclude cls._wrap_iter
            if attr_name.startswith("_") or attr_name in attrs:
                continue
            if isinstance(attr, classmethod):
                wrapper = classmethod_wrapper_factory(cls, attr_name)
                setattr(cls, attr_name, wrapper)
            elif isinstance(attr, types.FunctionType):
                wrapper = thread_wrapper_factory(cls, attr_name)
                setattr(cls, attr_name, wrapper)
            else:
                raise TypeError(attr_name, type(attr))

    def generate_magic(cls, attrs):
        # generate wrappers for magic
        for attr_name in cls._forward_magic:
            attr = getattr(cls._forwards, attr_name)
            wrapper = _forward_magic(cls, attr)
            setattr(cls, attr_name, wrapper)

    def generate_iter(cls, attrs):
        # generate wrappers for methods that return iterators
        for attr_name, attr in cls._wraps.__dict__.items():
            if attr_name in cls._wrap_iter:
                wrapper = iter_wrapper_factory(cls, attr_name)
                setattr(cls, attr_name, wrapper)


class Path(metaclass=AsyncAutoWrapperType):
    """A :class:`pathlib.Path` wrapper that executes blocking methods in
    :meth:`trio.to_thread.run_sync`.

    """

    _wraps = pathlib.Path
    _forwards = pathlib.PurePath
    _forward_magic = [
        "__str__",
        "__bytes__",
        "__truediv__",
        "__rtruediv__",
        "__eq__",
        "__lt__",
        "__le__",
        "__gt__",
        "__ge__",
        "__hash__",
    ]
    _wrap_iter = ["glob", "rglob", "iterdir"]

    def __init__(self, *args):
        self._wrapped = pathlib.Path(*args)

    def __getattr__(self, name):
        if name in self._forward:
            value = getattr(self._wrapped, name)
            return rewrap_path(value)
        raise AttributeError(name)

    def __dir__(self):
        return super().__dir__() + self._forward

    def __repr__(self):
        return f"trio.Path({repr(str(self))})"

    def __fspath__(self):
        return os.fspath(self._wrapped)

    @wraps(pathlib.Path.open)
    async def open(self, *args, **kwargs):
        """Open the file pointed to by the path, like the :func:`trio.open_file`
        function does.

        """

        func = partial(self._wrapped.open, *args, **kwargs)
        value = await trio.to_thread.run_sync(func)
        return trio.wrap_file(value)


Path.iterdir.__doc__ = """
    Like :meth:`pathlib.Path.iterdir`, but async.

    This is an async method that returns a synchronous iterator, so you
    use it like::

       for subpath in await mypath.iterdir():
           ...

    Note that it actually loads the whole directory list into memory
    immediately, during the initial call. (See `issue #501
    <https://github.com/python-trio/trio/issues/501>`__ for discussion.)

"""

# The value of Path.absolute.__doc__ makes a reference to
# :meth:~pathlib.Path.absolute, which does not exist. Removing this makes more
# sense than inventing our own special docstring for this.
del Path.absolute.__doc__

os.PathLike.register(Path)
