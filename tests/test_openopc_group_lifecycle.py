"""
Regression tests for the anonymous OPC group leak.

Every REST read/write goes through OpenOPC.client.iread()/iwrite(), which
create a brand-new anonymous COM group per call. Before the fix, group
removal ran *after* the read/write logic instead of in a `finally`, so any
exception (a timeout, a COM error, or even a plain Python bug) skipped
cleanup and permanently leaked the group, and, for async reads, its entry
in `self._group_hooks`, which was never deleted even on the success path.
These tests build a fake win32com/pythoncom layer (none of that is
importable on non-Windows), so the group lifecycle logic itself, which is
pure Python, can be exercised without a real OPC server.
"""

import queue
import types

import pytest

from http2opc.includes import OpenOPC


class FakeComError(Exception):
    pass


@pytest.fixture
def fake_win32(monkeypatch):
    """Stub the win32com/pythoncom/pywintypes surface OpenOPC.py touches."""
    fake_pythoncom = types.SimpleNamespace(
        com_error=FakeComError,
        CoInitialize=lambda: None,
        PumpWaitingMessages=lambda: None,
    )
    fake_pywintypes = types.SimpleNamespace(TimeType=type("FakeTimeType", (), {}))
    fake_win32com = types.SimpleNamespace(
        client=types.SimpleNamespace(WithEvents=lambda group, handler_cls: FakeHook())
    )
    monkeypatch.setattr(OpenOPC, "pythoncom", fake_pythoncom, raising=False)
    monkeypatch.setattr(OpenOPC, "pywintypes", fake_pywintypes, raising=False)
    monkeypatch.setattr(OpenOPC, "win32com", fake_win32com, raising=False)
    return types.SimpleNamespace(
        pythoncom=fake_pythoncom, pywintypes=fake_pywintypes, win32com=fake_win32com
    )


class FakeHook:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeItems:
    def Validate(self, count, names):
        return [0] * count

    def AddItems(self, count, valid_tags, client_handles):
        return list(range(count)), [0] * count


class FakeGroup:
    def __init__(
        self,
        name,
        client,
        sync_read_result=None,
        sync_read_error=None,
        deliver_callback=True,
    ):
        self.Name = name
        self.OPCItems = FakeItems()
        self.IsSubscribed = 0
        self.IsActive = 0
        self._client = client
        self._sync_read_result = sync_read_result
        self._sync_read_error = sync_read_error
        self._deliver_callback = deliver_callback

    def SyncRead(self, data_source, count, server_handles):
        if self._sync_read_error is not None:
            raise self._sync_read_error
        return self._sync_read_result

    def AsyncRefresh(self, data_source, tx_id):
        if self._deliver_callback:
            # Simulate the OPC server delivering the callback synchronously
            # for the single tag registered as client handle 0.
            self._client.callback_queue.put((tx_id, [0], [42], [192], ["2024-01-01"]))
        # else: simulate a hung/slow server that never calls back.

    def SyncWrite(self, count, server_handles, values):
        return [0] * count


class FakeGroups:
    def __init__(self, group_factory, remove_error=None):
        self._group_factory = group_factory
        self._remove_error = remove_error
        self.added = []
        self.removed = []

    def Add(self):
        group = self._group_factory(len(self.added))
        self.added.append(group)
        return group

    def Remove(self, name):
        if self._remove_error is not None:
            raise self._remove_error
        self.removed.append(name)


def make_client(group_factory, remove_error=None):
    client = OpenOPC.client.__new__(OpenOPC.client)
    client._groups = {}
    client._group_tags = {}
    client._group_valid_tags = {}
    client._group_server_handles = {}
    client._group_handles_tag = {}
    client._group_hooks = {}
    client._tx_id = 0
    client.trace = None
    client.cpu = None
    client.callback_queue = queue.Queue()
    groups = FakeGroups(lambda i: group_factory(i, client), remove_error=remove_error)
    client._opc = types.SimpleNamespace(OPCGroups=groups)
    return client, groups


def test_iread_removes_anonymous_group_when_sync_read_raises(fake_win32):
    # A crash deep inside the COM call (anything, not just pythoncom.com_error)
    # must not prevent the group from being removed.
    client, groups = make_client(
        lambda i, c: FakeGroup(f"Group{i}", c, sync_read_error=RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError):
        list(client.iread(["Tag1"], sync=True))

    assert groups.removed == ["Group0"]


def test_iread_removes_anonymous_group_on_timeout(fake_win32):
    # The classic leak trigger: a slow OPC server causes iread's own
    # TimeoutError, raised well before the old (unconditional) cleanup code.
    client, groups = make_client(
        lambda i, c: FakeGroup(f"Group{i}", c, deliver_callback=False)
    )

    with pytest.raises(OpenOPC.TimeoutError):
        list(client.iread(["Tag1"], sync=False, timeout=-1))

    assert groups.removed == ["Group0"]


def test_iread_removes_anonymous_group_on_success(fake_win32):
    client, groups = make_client(
        lambda i, c: FakeGroup(
            f"Group{i}", c, sync_read_result=([42], [0], [192], ["2024-01-01"])
        )
    )

    results = list(client.iread(["Tag1"], sync=True))

    assert results == [("Tag1", 42, "Good", "2024-01-01")]
    assert groups.removed == ["Group0"]


def test_iread_async_deletes_group_hooks_entry_after_success(fake_win32):
    # This is the unbounded-growth leak: _group_hooks used to only ever be
    # added to (one entry per anonymous async request, keyed by the unique
    # COM-generated group name) and .close()'d but never popped, so it grew
    # forever for the life of the process.
    client, groups = make_client(lambda i, c: FakeGroup(f"Group{i}", c))

    results = list(client.iread(["Tag1"], sync=False))

    assert results == [("Tag1", 42, "Good", "2024-01-01")]
    assert client._group_hooks == {}  # popped, not just closed
    assert groups.removed == ["Group0"]


def test_iwrite_removes_anonymous_group_when_validate_raises_non_com_error(fake_win32):
    # Regression for the write-side equivalent: if the Validate() COM call
    # itself throws, 'errors' stays [] (set before the try) and the very next
    # `errors[i]` raises a plain IndexError, not a pythoncom.com_error, so
    # it wasn't caught by write's error handling either. The group must still
    # be removed.
    class BrokenItems(FakeItems):
        def Validate(self, count, names):
            raise RuntimeError("COM call failed")

    def factory(i, c):
        group = FakeGroup(f"Group{i}", c)
        group.OPCItems = BrokenItems()
        return group

    client, groups = make_client(factory)

    with pytest.raises(IndexError):
        list(client.iwrite([("Tag1", "123.0")]))

    assert groups.removed == ["Group0"]


def test_iwrite_removes_anonymous_group_on_success(fake_win32):
    client, groups = make_client(lambda i, c: FakeGroup(f"Group{i}", c))

    results = list(client.iwrite(("Tag1", "123.0")))

    assert results == ["Success"]
    assert groups.removed == ["Group0"]


def test_read_single_tag_removes_anonymous_group_synchronously(fake_win32):
    # Regression test: read()'s single-tag path used to do
    # `next(iter(results))`, which only pulls iread()'s first yielded value
    # and leaves the generator suspended *before* it reaches the `finally`
    # that removes the group. The fix is `list(results)[0]`, which fully
    # drains the generator first.
    client, groups = make_client(
        lambda i, c: FakeGroup(
            f"Group{i}", c, sync_read_result=([42], [0], [192], ["2024-01-01"])
        )
    )

    result = client.read("Tag1", sync=True)

    assert result == (42, "Good", "2024-01-01")
    assert groups.removed == ["Group0"]


def test_read_single_tag_propagates_cleanup_errors_instead_of_swallowing_them(
    fake_win32,
):
    # The real distinguishing behavior between `list(results)[0]` and
    # `next(iter(results))`: under CPython, an unreferenced generator is
    # collected (and its `finally` run) essentially immediately either way,
    # so a plain success/failure check doesn't tell the two apart. What
    # does: `next(iter(...))` only pulls the first value and returns:
    # cleanup then runs later, as a side effect of garbage collection
    # closing the now-unreferenced generator. Any exception raised at that
    # point (not just pythoncom.com_error, which the finally already
    # catches) becomes "unraisable" -- Python prints it and swallows it,
    # read() returns its value as if nothing went wrong. Fully draining
    # with list()[0] instead makes that same exception propagate normally,
    # so a real cleanup bug is visible instead of silently discarded.
    client, _groups = make_client(
        lambda i, c: FakeGroup(
            f"Group{i}", c, sync_read_result=([42], [0], [192], ["2024-01-01"])
        ),
        remove_error=RuntimeError("cleanup boom"),
    )

    with pytest.raises(RuntimeError, match="cleanup boom"):
        client.read("Tag1", sync=True)


def test_write_single_pair_removes_anonymous_group_synchronously(fake_win32):
    # Same regression as above, for write()'s single-pair path.
    client, groups = make_client(lambda i, c: FakeGroup(f"Group{i}", c))

    result = client.write(("Tag1", "123.0"))

    assert result == "Success"
    assert groups.removed == ["Group0"]


def test_write_single_pair_propagates_cleanup_errors_instead_of_swallowing_them(
    fake_win32,
):
    # See the matching read() test for why this is the behavior that
    # actually distinguishes list(status)[0] from next(iter(status)).
    client, _groups = make_client(
        lambda i, c: FakeGroup(f"Group{i}", c),
        remove_error=RuntimeError("cleanup boom"),
    )

    with pytest.raises(RuntimeError, match="cleanup boom"):
        client.write(("Tag1", "123.0"))
