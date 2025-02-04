import outcome
import pytest
import sys
import os
import signal
import threading
import contextlib
import time
import inspect

try:
    from async_generator import yield_, async_generator
except ImportError:  # pragma: no cover
    async_generator = yield_ = None

from ... import _core
from ...testing import wait_all_tasks_blocked
from ..._util import signal_raise, is_main_thread
from ..._timeouts import sleep
from .tutil import slow


def ki_self():
    signal_raise(signal.SIGINT)


def test_ki_self():
    with pytest.raises(KeyboardInterrupt):
        ki_self()


async def test_ki_enabled():
    # Regular tasks aren't KI-protected
    assert not _core.currently_ki_protected()

    # Low-level call-soon callbacks are KI-protected
    token = _core.current_trio_token()
    record = []

    def check():
        record.append(_core.currently_ki_protected())

    token.run_sync_soon(check)
    await wait_all_tasks_blocked()
    assert record == [True]

    @_core.enable_ki_protection
    def protected():
        assert _core.currently_ki_protected()
        unprotected()

    @_core.disable_ki_protection
    def unprotected():
        assert not _core.currently_ki_protected()

    protected()

    @_core.enable_ki_protection
    async def aprotected():
        assert _core.currently_ki_protected()
        await aunprotected()

    @_core.disable_ki_protection
    async def aunprotected():
        assert not _core.currently_ki_protected()

    await aprotected()

    # make sure that the decorator here overrides the automatic manipulation
    # that start_soon() does:
    async with _core.open_nursery() as nursery:
        nursery.start_soon(aprotected)
        nursery.start_soon(aunprotected)

    @_core.enable_ki_protection
    def gen_protected():
        assert _core.currently_ki_protected()
        yield

    for _ in gen_protected():
        pass

    @_core.disable_ki_protection
    def gen_unprotected():
        assert not _core.currently_ki_protected()
        yield

    for _ in gen_unprotected():
        pass


# This used to be broken due to
#
#   https://bugs.python.org/issue29590
#
# Specifically, after a coroutine is resumed with .throw(), then the stack
# makes it look like the immediate caller is the function that called
# .throw(), not the actual caller. So child() here would have a caller deep in
# the guts of the run loop, and always be protected, even when it shouldn't
# have been. (Solution: we don't use .throw() anymore.)
async def test_ki_enabled_after_yield_briefly():
    @_core.enable_ki_protection
    async def protected():
        await child(True)

    @_core.disable_ki_protection
    async def unprotected():
        await child(False)

    async def child(expected):
        import traceback

        traceback.print_stack()
        assert _core.currently_ki_protected() == expected
        await _core.checkpoint()
        traceback.print_stack()
        assert _core.currently_ki_protected() == expected

    await protected()
    await unprotected()


# This also used to be broken due to
#   https://bugs.python.org/issue29590
async def test_generator_based_context_manager_throw():
    @contextlib.contextmanager
    @_core.enable_ki_protection
    def protected_manager():
        assert _core.currently_ki_protected()
        try:
            yield
        finally:
            assert _core.currently_ki_protected()

    with protected_manager():
        assert not _core.currently_ki_protected()

    with pytest.raises(KeyError):
        # This is the one that used to fail
        with protected_manager():
            raise KeyError


@pytest.mark.skipif(async_generator is None, reason="async_generator not installed")
async def test_async_generator_agen_protection():
    @_core.enable_ki_protection
    @async_generator
    async def agen_protected1():
        assert _core.currently_ki_protected()
        try:
            await yield_()
        finally:
            assert _core.currently_ki_protected()

    @_core.disable_ki_protection
    @async_generator
    async def agen_unprotected1():
        assert not _core.currently_ki_protected()
        try:
            await yield_()
        finally:
            assert not _core.currently_ki_protected()

    # Swap the order of the decorators:
    @async_generator
    @_core.enable_ki_protection
    async def agen_protected2():
        assert _core.currently_ki_protected()
        try:
            await yield_()
        finally:
            assert _core.currently_ki_protected()

    @async_generator
    @_core.disable_ki_protection
    async def agen_unprotected2():
        assert not _core.currently_ki_protected()
        try:
            await yield_()
        finally:
            assert not _core.currently_ki_protected()

    await _check_agen(agen_protected1)
    await _check_agen(agen_protected2)
    await _check_agen(agen_unprotected1)
    await _check_agen(agen_unprotected2)


async def test_native_agen_protection():
    # Native async generators
    @_core.enable_ki_protection
    async def agen_protected():
        assert _core.currently_ki_protected()
        try:
            yield
        finally:
            assert _core.currently_ki_protected()

    @_core.disable_ki_protection
    async def agen_unprotected():
        assert not _core.currently_ki_protected()
        try:
            yield
        finally:
            assert not _core.currently_ki_protected()

    await _check_agen(agen_protected)
    await _check_agen(agen_unprotected)


async def _check_agen(agen_fn):
    async for _ in agen_fn():  # noqa
        assert not _core.currently_ki_protected()

    # asynccontextmanager insists that the function passed must itself be an
    # async gen function, not a wrapper around one
    if inspect.isasyncgenfunction(agen_fn):
        async with contextlib.asynccontextmanager(agen_fn)():
            assert not _core.currently_ki_protected()

        # Another case that's tricky due to:
        #   https://bugs.python.org/issue29590
        with pytest.raises(KeyError):
            async with contextlib.asynccontextmanager(agen_fn)():
                raise KeyError


# Test the case where there's no magic local anywhere in the call stack
def test_ki_disabled_out_of_context():
    assert _core.currently_ki_protected()


def test_ki_disabled_in_del():
    def nestedfunction():
        return _core.currently_ki_protected()

    def __del__():
        assert _core.currently_ki_protected()
        assert nestedfunction()

    @_core.disable_ki_protection
    def outerfunction():
        assert not _core.currently_ki_protected()
        assert not nestedfunction()
        __del__()

    __del__()
    outerfunction()
    assert nestedfunction()


def test_ki_protection_works():
    async def sleeper(name, record):
        try:
            while True:
                await _core.checkpoint()
        except _core.Cancelled:
            record.add(name + " ok")

    async def raiser(name, record):
        try:
            # os.kill runs signal handlers before returning, so we don't need
            # to worry that the handler will be delayed
            print("killing, protection =", _core.currently_ki_protected())
            ki_self()
        except KeyboardInterrupt:
            print("raised!")
            # Make sure we aren't getting cancelled as well as siginted
            await _core.checkpoint()
            record.add(name + " raise ok")
            raise
        else:
            print("didn't raise!")
            # If we didn't raise (b/c protected), then we *should* get
            # cancelled at the next opportunity
            try:
                await _core.wait_task_rescheduled(lambda _: _core.Abort.SUCCEEDED)
            except _core.Cancelled:
                record.add(name + " cancel ok")

    # simulated control-C during raiser, which is *unprotected*
    print("check 1")
    record = set()

    async def check_unprotected_kill():
        async with _core.open_nursery() as nursery:
            nursery.start_soon(sleeper, "s1", record)
            nursery.start_soon(sleeper, "s2", record)
            nursery.start_soon(raiser, "r1", record)

    with pytest.raises(KeyboardInterrupt):
        _core.run(check_unprotected_kill)
    assert record == {"s1 ok", "s2 ok", "r1 raise ok"}

    # simulated control-C during raiser, which is *protected*, so the KI gets
    # delivered to the main task instead
    print("check 2")
    record = set()

    async def check_protected_kill():
        async with _core.open_nursery() as nursery:
            nursery.start_soon(sleeper, "s1", record)
            nursery.start_soon(sleeper, "s2", record)
            nursery.start_soon(_core.enable_ki_protection(raiser), "r1", record)
            # __aexit__ blocks, and then receives the KI

    with pytest.raises(KeyboardInterrupt):
        _core.run(check_protected_kill)
    assert record == {"s1 ok", "s2 ok", "r1 cancel ok"}

    # kill at last moment still raises (run_sync_soon until it raises an
    # error, then kill)
    print("check 3")

    async def check_kill_during_shutdown():
        token = _core.current_trio_token()

        def kill_during_shutdown():
            assert _core.currently_ki_protected()
            try:
                token.run_sync_soon(kill_during_shutdown)
            except _core.RunFinishedError:
                # it's too late for regular handling! handle this!
                print("kill! kill!")
                ki_self()

        token.run_sync_soon(kill_during_shutdown)

    with pytest.raises(KeyboardInterrupt):
        _core.run(check_kill_during_shutdown)

    # KI arrives very early, before main is even spawned
    print("check 4")

    class InstrumentOfDeath:
        def before_run(self):
            ki_self()

    async def main():
        await _core.checkpoint()

    with pytest.raises(KeyboardInterrupt):
        _core.run(main, instruments=[InstrumentOfDeath()])

    # checkpoint_if_cancelled notices pending KI
    print("check 5")

    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        ki_self()
        with pytest.raises(KeyboardInterrupt):
            await _core.checkpoint_if_cancelled()

    _core.run(main)

    # KI arrives while main task is not abortable, b/c already scheduled
    print("check 6")

    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        ki_self()
        await _core.cancel_shielded_checkpoint()
        await _core.cancel_shielded_checkpoint()
        await _core.cancel_shielded_checkpoint()
        with pytest.raises(KeyboardInterrupt):
            await _core.checkpoint()

    _core.run(main)

    # KI arrives while main task is not abortable, b/c refuses to be aborted
    print("check 7")

    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        ki_self()
        task = _core.current_task()

        def abort(_):
            _core.reschedule(task, outcome.Value(1))
            return _core.Abort.FAILED

        assert await _core.wait_task_rescheduled(abort) == 1
        with pytest.raises(KeyboardInterrupt):
            await _core.checkpoint()

    _core.run(main)

    # KI delivered via slow abort
    print("check 8")

    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        ki_self()
        task = _core.current_task()

        def abort(raise_cancel):
            result = outcome.capture(raise_cancel)
            _core.reschedule(task, result)
            return _core.Abort.FAILED

        with pytest.raises(KeyboardInterrupt):
            assert await _core.wait_task_rescheduled(abort)
        await _core.checkpoint()

    _core.run(main)

    # KI arrives just before main task exits, so the run_sync_soon machinery
    # is still functioning and will accept the callback to deliver the KI, but
    # by the time the callback is actually run, main has exited and can't be
    # aborted.
    print("check 9")

    @_core.enable_ki_protection
    async def main():
        ki_self()

    with pytest.raises(KeyboardInterrupt):
        _core.run(main)

    print("check 10")
    # KI in unprotected code, with
    # restrict_keyboard_interrupt_to_checkpoints=True
    record = []

    async def main():
        # We're not KI protected...
        assert not _core.currently_ki_protected()
        ki_self()
        # ...but even after the KI, we keep running uninterrupted...
        record.append("ok")
        # ...until we hit a checkpoint:
        with pytest.raises(KeyboardInterrupt):
            await sleep(10)

    _core.run(main, restrict_keyboard_interrupt_to_checkpoints=True)
    assert record == ["ok"]
    record = []
    # Exact same code raises KI early if we leave off the argument, doesn't
    # even reach the record.append call:
    with pytest.raises(KeyboardInterrupt):
        _core.run(main)
    assert record == []

    # KI arrives while main task is inside a cancelled cancellation scope
    # the KeyboardInterrupt should take priority
    print("check 11")

    @_core.enable_ki_protection
    async def main():
        assert _core.currently_ki_protected()
        with _core.CancelScope() as cancel_scope:
            cancel_scope.cancel()
            with pytest.raises(_core.Cancelled):
                await _core.checkpoint()
            ki_self()
            with pytest.raises(KeyboardInterrupt):
                await _core.checkpoint()
            with pytest.raises(_core.Cancelled):
                await _core.checkpoint()

    _core.run(main)


def test_ki_is_good_neighbor():
    # in the unlikely event someone overwrites our signal handler, we leave
    # the overwritten one be
    try:
        orig = signal.getsignal(signal.SIGINT)

        def my_handler(signum, frame):  # pragma: no cover
            pass

        async def main():
            signal.signal(signal.SIGINT, my_handler)

        _core.run(main)

        assert signal.getsignal(signal.SIGINT) is my_handler
    finally:
        signal.signal(signal.SIGINT, orig)


# Regression test for #461
def test_ki_with_broken_threads():
    thread = threading.main_thread()

    # scary!
    original = threading._active[thread.ident]

    # put this in a try finally so we don't have a chance of cascading a
    # breakage down to everything else
    try:
        del threading._active[thread.ident]

        @_core.enable_ki_protection
        async def inner():
            assert signal.getsignal(signal.SIGINT) != signal.default_int_handler

        _core.run(inner)
    finally:
        threading._active[thread.ident] = original
