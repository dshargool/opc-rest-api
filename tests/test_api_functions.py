import time

from http2opc.includes import ApiFunctions as api_functions_module
from http2opc.includes.ApiFunctions import ApiFunctions, OpcConnection


class FakeLogger:
    """Records calls per level so tests can assert *which* level was used --
    a log line at the wrong level (e.g. a failed write logged at info instead
    of warning) would defeat the point of instrumenting it without ever
    failing functionally, so this is worth locking in."""

    def __init__(self):
        self.calls = {"debug": [], "info": [], "warning": [], "error": []}

    def debug(self, msg, *a, **k):
        self.calls["debug"].append(msg)

    def info(self, msg, *a, **k):
        self.calls["info"].append(msg)

    def warning(self, msg, *a, **k):
        self.calls["warning"].append(msg)

    def error(self, msg, *a, **k):
        self.calls["error"].append(msg)


def make_funcs(show_root=False):
    funcs = ApiFunctions(FakeLogger())
    funcs.preferences = {"show_root": show_root}
    funcs.connections = []
    return funcs


def add_connection(funcs, opc, name="primary", connected=True):
    """Attach a fake OpcConnection to `funcs`, backed by the given fake OPC
    client double. Uses __new__ to skip OpcConnection.__init__ (which would
    otherwise construct a real OpenOPC.client() and call set_trace() on it)
    so the fake `opc` double doesn't need to support that."""
    conn = OpcConnection.__new__(OpcConnection)
    conn.name = name
    conn.logger = funcs.logger
    conn.classname = "Test.Class"
    conn.servers = "Test.Server"
    conn.host = "127.0.0.1"
    conn.opc = opc
    conn.connected = connected
    conn._ever_connected = connected
    conn._disconnected_at = None if connected else time.time()
    conn._next_reconnect_attempt = 0
    conn._next_health_check = 0
    conn._last_outage_log = 0
    funcs.connections.append(conn)
    return conn


def test_list_prepends_root_when_show_root_false():
    captured = {}

    class Opc:
        def list(self, params, recursive, flat, include_type):
            captured.update(
                params=params, recursive=recursive, flat=flat, include_type=include_type
            )
            return [("Root.Tag1.Value", "Leaf")]

    funcs = make_funcs(show_root=False)
    add_connection(funcs, Opc())

    result = funcs.list("*")

    assert captured == {
        "params": "Root.*",
        "recursive": False,
        "flat": False,
        "include_type": True,
    }
    assert result == [("Root.Tag1.Value", "Leaf")]


def test_list_does_not_prepend_root_when_show_root_true():
    captured = {}

    class Opc:
        def list(self, params, recursive, flat, include_type):
            captured["params"] = params
            return []

    funcs = make_funcs(show_root=True)
    add_connection(funcs, Opc())

    funcs.list("Root.*")

    assert captured["params"] == "Root.*"


def test_list_recursive_passes_through_without_root_prefix():
    captured = {}

    class Opc:
        def list(self, params, recursive, flat, include_type):
            captured.update(params=params, recursive=recursive)
            return []

    funcs = make_funcs(show_root=False)
    add_connection(funcs, Opc())

    funcs.listRecursive("Root.*")

    assert captured == {"params": "Root.*", "recursive": True}


def test_search_wraps_pattern_with_wildcards():
    captured = {}

    class Opc:
        def list(self, params, recursive, flat, include_type):
            captured["params"] = params
            return []

    funcs = make_funcs()
    add_connection(funcs, Opc())

    funcs.search("Common")

    assert captured["params"] == "*Common*"


def test_read_passthrough():
    class Opc:
        def read(self, params, sync=False):
            return (1.23, "Good", "2024-01-01")

    funcs = make_funcs()
    add_connection(funcs, Opc())

    assert funcs.read("Root.Tag1") == (1.23, "Good", "2024-01-01")


def test_read_uses_sync_mode():
    # Regression test: async (sync=False, the OpenOPC default) reads create a
    # per-request COM group + event subscription that leak if the request
    # errors or times out (see OpenOPC.iread). A request/response proxy like
    # this has no use for the async/callback path, so it must always read
    # synchronously.
    captured = {}

    class Opc:
        def read(self, params, sync=False):
            captured["sync"] = sync
            return (1.23, "Good", "2024-01-01")

    funcs = make_funcs()
    add_connection(funcs, Opc())

    funcs.read("Root.Tag1")

    assert captured["sync"] is True


def test_read_single_tag_is_passed_through_unsplit():
    # No comma -> preserve OpenOPC's single-tag response shape exactly as
    # before (a bare (value, quality, timestamp) tuple), not the multi-tag
    # list-of-tuples shape a length-1 list would produce.
    captured = {}

    class Opc:
        def read(self, tags, sync=False):
            captured["tags"] = tags
            return (1.23, "Good", "2024-01-01")

    funcs = make_funcs()
    add_connection(funcs, Opc())

    result = funcs.read("Root.Tag1")

    assert captured["tags"] == "Root.Tag1"
    assert result == (1.23, "Good", "2024-01-01")


def test_read_comma_separated_tags_batches_into_one_call():
    captured = {}

    class Opc:
        def read(self, tags, sync=False):
            captured["tags"] = tags
            return [("Root.Tag1", 1.0, "Good", "t1"), ("Root.Tag2", 2.0, "Good", "t2")]

    funcs = make_funcs()
    add_connection(funcs, Opc())

    result = funcs.read("Root.Tag1,Root.Tag2")

    assert captured["tags"] == ["Root.Tag1", "Root.Tag2"]
    assert result == [
        ("Root.Tag1", 1.0, "Good", "t1"),
        ("Root.Tag2", 2.0, "Good", "t2"),
    ]


def test_write_passthrough():
    class Opc:
        def write(self, params):
            assert params == ["Root.Tag1", "123.0"]
            return "Success"

    funcs = make_funcs()
    add_connection(funcs, Opc())

    assert funcs.write(["Root.Tag1", "123.0"]) == "Success"


def test_write_success_is_logged_at_info():
    class Opc:
        def write(self, params):
            return "Success"

    funcs = make_funcs()
    add_connection(funcs, Opc())

    funcs.write(["Root.Tag1", "123.0"])

    assert any("Success" in msg for msg in funcs.logger.calls["info"])
    assert funcs.logger.calls["warning"] == []


def test_write_failure_is_logged_at_warning_not_silently():
    class Opc:
        def write(self, params):
            return "Error"

    funcs = make_funcs()
    add_connection(funcs, Opc())

    funcs.write(["Root.Tag1", "123.0"])

    assert any("Error" in msg for msg in funcs.logger.calls["warning"])


def test_write_batch_passthrough():
    class Opc:
        def write(self, params):
            assert params == [("Root.Tag1", "1"), ("Root.Tag2", "2")]
            return [("Root.Tag1", "Success"), ("Root.Tag2", "Success")]

    funcs = make_funcs()
    add_connection(funcs, Opc())

    result = funcs.write([("Root.Tag1", "1"), ("Root.Tag2", "2")])

    assert result == [("Root.Tag1", "Success"), ("Root.Tag2", "Success")]


def test_write_batch_all_success_logs_info_not_warning():
    class Opc:
        def write(self, params):
            return [("Tag1", "Success"), ("Tag2", "Success")]

    funcs = make_funcs()
    add_connection(funcs, Opc())

    funcs.write([("Tag1", "1"), ("Tag2", "2")])

    assert any("all 2 succeeded" in msg for msg in funcs.logger.calls["info"])
    assert funcs.logger.calls["warning"] == []


def test_write_batch_partial_failure_logs_warning_with_detail():
    class Opc:
        def write(self, params):
            return [("Tag1", "Success"), ("Tag2", "Error")]

    funcs = make_funcs()
    add_connection(funcs, Opc())

    result = funcs.write([("Tag1", "1"), ("Tag2", "2")])

    assert result == [("Tag1", "Success"), ("Tag2", "Error")]
    assert any(
        "1/2 succeeded" in msg and "Tag2" in msg
        for msg in funcs.logger.calls["warning"]
    )


def test_test_call_passthrough():
    class Opc:
        def info(self):
            return [("State", "Running")]

    funcs = make_funcs()
    add_connection(funcs, Opc())

    assert funcs.test_call() == [("State", "Running")]


def test_properties_splits_comma_separated_string():
    captured = {}

    class Opc:
        def properties(self, tags):
            captured["tags"] = tags
            return []

    funcs = make_funcs()
    add_connection(funcs, Opc())

    funcs.properties("Tag1,Tag2", False)

    assert captured["tags"] == ["Tag1", "Tag2"]


def test_properties_accepts_list_unchanged():
    captured = {}

    class Opc:
        def properties(self, tags):
            captured["tags"] = tags
            return []

    funcs = make_funcs()
    add_connection(funcs, Opc())

    funcs.properties(["Tag1", "Tag2"], False)

    assert captured["tags"] == ["Tag1", "Tag2"]


def test_properties_plain_returns_raw_rows():
    rows = [("Tag1", 0, "Item ID (virtual property)", "Tag1")]

    class Opc:
        def properties(self, tags):
            return rows

    funcs = make_funcs()
    add_connection(funcs, Opc())

    assert funcs.properties("Tag1", False) == rows


def test_properties_as_json_builds_json_list():
    rows = [("Tag1", 0, "Item ID (virtual property)", "Tag1")]

    class Opc:
        def properties(self, tags):
            return rows

    funcs = make_funcs()
    add_connection(funcs, Opc())

    assert funcs.properties("Tag1", True) == [{"Item ID (virtual property)": "Tag1"}]


def test_build_json_list_groups_rows_by_tag():
    rows = [
        ("Tag1", 0, "Item ID (virtual property)", "Tag1"),
        ("Tag1", 1, "Item Canonical DataType", "VT_R4"),
        ("Tag2", 0, "Item ID (virtual property)", "Tag2"),
        ("Tag2", 1, "Item Canonical DataType", "VT_I4"),
    ]
    funcs = make_funcs()

    result = funcs.buildJsonList(rows)

    assert result == [
        {"Item ID (virtual property)": "Tag1", "Item Canonical DataType": "VT_R4"},
        {"Item ID (virtual property)": "Tag2", "Item Canonical DataType": "VT_I4"},
    ]


def test_list_tree_builds_branches_and_leaves():
    class Opc:
        def list(self, params, recursive, flat, include_type):
            assert params == "Root.*"
            return [
                ("Root.Int4.Value", "Leaf"),
                ("Root.Boilers.FI8110.Value", "Leaf"),
                ("Root.Boilers.PI8110.Value", "Leaf"),
            ]

        def properties(self, tags):
            tag = tags[0]
            return [
                (tag, 0, "Item ID (virtual property)", tag),
                (tag, 4, "Item Description", "Boiler Instrument"),
            ]

    funcs = make_funcs(show_root=False)
    add_connection(funcs, Opc())

    tree = funcs.listTree("*")

    assert tree == [
        ["Root.Int4.Value", "Leaf"],
        ["Boilers", "Branch", "Boiler Instrument"],
    ]


def test_list_one_deep_groups_properties_and_marks_leaf():
    class Opc:
        def list(self, params, recursive, flat, include_type):
            return [("Root.A.Value", "Leaf"), ("Root.B.Value", "Leaf")]

        def properties(self, tags):
            rows = []
            for i, tag in enumerate(tags):
                rows.append((tag, 0, "Item ID (virtual property)", tag))
                rows.append((tag, 5, "Item Value", 100 + i))
            return rows

    funcs = make_funcs(show_root=False)
    add_connection(funcs, Opc())

    result = funcs.listOneDeep("*")

    assert result == {
        "Root.A.Value": [
            ("Root.A.Value", 0, "Item ID (virtual property)", "Root.A.Value"),
            ("Root.A.Value", 5, "Item Value", 100),
            "Leaf",
        ],
        "Root.B.Value": [
            ("Root.B.Value", 0, "Item ID (virtual property)", "Root.B.Value"),
            ("Root.B.Value", 5, "Item Value", 101),
            "Leaf",
        ],
    }


def test_init_retries_until_connected(tmp_path, monkeypatch):
    conf = tmp_path / "main.conf"
    conf.write_text(
        "[opc]\n"
        "classname=Some.Class\n"
        "servers=Some.Server\n"
        "host=127.0.0.1\n"
        "\n"
        "[preferences]\n"
        "show_root=False\n"
    )
    monkeypatch.chdir(tmp_path)

    connect_calls = []

    class FakeClient:
        def set_trace(self, trace):
            pass

        def connect(self, servers, host):
            connect_calls.append((servers, host))
            return len(connect_calls) >= 2  # fail the first attempt, succeed the second

    monkeypatch.setattr(api_functions_module.OpenOPC, "client", lambda: FakeClient())
    monkeypatch.setattr(api_functions_module.time, "sleep", lambda seconds: None)

    funcs = ApiFunctions(FakeLogger())
    result = funcs.init()

    assert result is True
    assert connect_calls == [("Some.Server", "127.0.0.1"), ("Some.Server", "127.0.0.1")]
    assert funcs.preferences == {"show_root": False}
    assert funcs.connected is True
    assert len(funcs.connections) == 1
    assert funcs.connections[0].name == "primary"


def test_init_configures_failover_connections(tmp_path, monkeypatch):
    conf = tmp_path / "main.conf"
    conf.write_text(
        "[opc]\n"
        "classname=Some.Class\n"
        "servers=Some.Server\n"
        "host=10.0.0.1\n"
        "failover=secondary\n"
        "\n"
        "[secondary]\n"
        "host=10.0.0.2\n"
        "\n"
        "[preferences]\n"
        "show_root=False\n"
    )
    monkeypatch.chdir(tmp_path)

    made = []

    class FakeClient:
        def __init__(self):
            made.append(self)
            self.connect_calls = []

        def set_trace(self, trace):
            pass

        def connect(self, servers, host):
            self.connect_calls.append((servers, host))
            return True

    monkeypatch.setattr(api_functions_module.OpenOPC, "client", FakeClient)
    monkeypatch.setattr(api_functions_module.time, "sleep", lambda seconds: None)

    funcs = ApiFunctions(FakeLogger())
    funcs.init()

    assert [c.name for c in funcs.connections] == ["primary", "secondary"]
    # classname/servers fall back to the primary's; host does not (there's
    # no sensible default, since the whole point of a failover is a
    # different host).
    assert funcs.connections[1].classname == "Some.Class"
    assert funcs.connections[1].servers == "Some.Server"
    assert funcs.connections[1].host == "10.0.0.2"
    # Both connect at startup (primary blocking, failover best-effort).
    assert made[0].connect_calls == [("Some.Server", "10.0.0.1")]
    assert made[1].connect_calls == [("Some.Server", "10.0.0.2")]
    assert funcs.connections[0].connected is True
    assert funcs.connections[1].connected is True


def test_ping_true_when_state_running():
    class Opc:
        def server_state(self):
            return "Running"

    funcs = make_funcs()
    conn = add_connection(funcs, Opc())

    assert conn.ping() is True


def test_ping_false_when_state_not_running():
    class Opc:
        def server_state(self):
            return "Failed"

    funcs = make_funcs()
    conn = add_connection(funcs, Opc())

    assert conn.ping() is False
    assert any("Failed" in msg for msg in funcs.logger.calls["warning"])


def test_ping_false_when_server_state_raises_opc_error():
    class Opc:
        def server_state(self):
            raise api_functions_module.OpenOPC.OPCError("unreachable")

    funcs = make_funcs()
    conn = add_connection(funcs, Opc())

    assert conn.ping() is False


def test_mark_disconnected_flips_flag_once():
    funcs = make_funcs()
    conn = add_connection(funcs, object(), connected=True)

    conn.mark_disconnected("boom")
    assert conn.connected is False
    assert any("boom" in msg for msg in funcs.logger.calls["error"])
    disconnected_at = conn._disconnected_at
    assert disconnected_at is not None

    # A second call while already down must not reset the outage clock, or
    # log a second time. It should look like one continuous outage, not a
    # fresh one each time a request happens to fail.
    conn.mark_disconnected("boom again")
    assert conn._disconnected_at == disconnected_at
    assert len(funcs.logger.calls["error"]) == 1


def test_tick_does_nothing_before_health_check_is_due():
    calls = []

    class Opc:
        def server_state(self):
            calls.append("ping")
            return "Running"

    funcs = make_funcs()
    conn = add_connection(funcs, Opc(), connected=True)
    conn._next_health_check = time.time() + 1000  # far future

    funcs.tick()

    assert calls == []


def test_tick_pings_when_health_check_is_due_and_disconnects_on_failure():
    class Opc:
        def server_state(self):
            return "Failed"

    funcs = make_funcs()
    conn = add_connection(funcs, Opc(), connected=True)
    conn._next_health_check = 0  # due immediately

    funcs.tick()

    assert conn.connected is False


def test_tick_reconnects_when_disconnected_and_attempt_is_due():
    connect_calls = []

    class Opc:
        def connect(self, servers, host):
            connect_calls.append((servers, host))
            return True

    funcs = make_funcs()
    conn = add_connection(funcs, Opc(), connected=False)
    conn.servers = "Some.Server"
    conn.host = "127.0.0.1"
    conn._next_reconnect_attempt = 0  # due immediately

    funcs.tick()

    assert connect_calls == [("Some.Server", "127.0.0.1")]
    assert conn.connected is True


def test_tick_does_not_reconnect_before_next_attempt_is_due():
    connect_calls = []

    class Opc:
        def connect(self, servers, host):
            connect_calls.append((servers, host))
            return True

    funcs = make_funcs()
    conn = add_connection(funcs, Opc(), connected=False)
    conn._next_reconnect_attempt = time.time() + 1000  # far future

    funcs.tick()

    assert connect_calls == []
    assert conn.connected is False


def test_reconnect_after_a_prior_connection_closes_the_old_session_first():
    # Regression test: reconnecting used to call opc.connect() again on the
    # same client with no opc.close() first, unlike this project's pre-port
    # ping(), which always closed the old session before reconnecting.
    calls = []

    class Opc:
        def close(self):
            calls.append("close")

        def connect(self, servers, host):
            calls.append("connect")
            return True

    funcs = make_funcs()
    conn = add_connection(funcs, Opc(), connected=False)
    conn._ever_connected = True  # simulates "was connected before, then dropped"
    conn._next_reconnect_attempt = 0

    funcs.tick()

    assert calls == ["close", "connect"]


def test_initial_connect_does_not_call_close_first():
    # No prior session to close on the very first-ever connect attempt.
    calls = []

    class Opc:
        def close(self):
            calls.append("close")

        def connect(self, servers, host):
            calls.append("connect")
            return True

    funcs = make_funcs()
    conn = add_connection(funcs, Opc(), connected=False)
    assert conn._ever_connected is False
    conn._next_reconnect_attempt = 0

    funcs.tick()

    assert calls == ["connect"]


def test_reconnect_tolerates_close_failing():
    # close() on an already-dead connection can itself raise; that must not
    # prevent the reconnect attempt that follows it.
    class Opc:
        def close(self):
            raise RuntimeError("already disconnected")

        def connect(self, servers, host):
            return True

    funcs = make_funcs()
    conn = add_connection(funcs, Opc(), connected=False)
    conn._ever_connected = True
    conn._next_reconnect_attempt = 0

    funcs.tick()

    assert conn.connected is True


# Multi-connection failover


def test_with_failover_uses_primary_when_up():
    calls = []

    class PrimaryOpc:
        def list(self, *a):
            calls.append("primary")
            return ["from primary"]

    class SecondaryOpc:
        def list(self, *a):
            calls.append("secondary")
            return ["from secondary"]

    funcs = make_funcs()
    add_connection(funcs, PrimaryOpc(), name="primary", connected=True)
    add_connection(funcs, SecondaryOpc(), name="secondary", connected=True)

    result = funcs.list("*")

    assert result == ["from primary"]
    assert calls == ["primary"]


def test_with_failover_skips_down_connections():
    class SecondaryOpc:
        def list(self, *a):
            return ["from secondary"]

    funcs = make_funcs()
    add_connection(funcs, object(), name="primary", connected=False)
    add_connection(funcs, SecondaryOpc(), name="secondary", connected=True)

    result = funcs.list("*")

    assert result == ["from secondary"]


def test_with_failover_falls_over_on_opc_error_and_marks_primary_down():
    class PrimaryOpc:
        def list(self, *a):
            raise api_functions_module.OpenOPC.OPCError("primary down")

    class SecondaryOpc:
        def list(self, *a):
            return ["from secondary"]

    funcs = make_funcs()
    primary = add_connection(funcs, PrimaryOpc(), name="primary", connected=True)
    add_connection(funcs, SecondaryOpc(), name="secondary", connected=True)

    result = funcs.list("*")

    assert result == ["from secondary"]
    assert primary.connected is False


def test_with_failover_raises_when_every_connection_fails():
    class BrokenOpc:
        def list(self, *a):
            raise api_functions_module.OpenOPC.OPCError("down")

    funcs = make_funcs()
    add_connection(funcs, BrokenOpc(), name="primary", connected=True)
    add_connection(funcs, BrokenOpc(), name="secondary", connected=True)

    try:
        funcs.list("*")
        assert False, "expected OPCError"
    except api_functions_module.OpenOPC.OPCError as err:
        assert "down" in str(err)

    assert funcs.connected is False


def test_write_fails_over_to_secondary():
    # Confirmed acceptable for this deployment: these OPC servers are
    # independent front-ends that all ultimately write through to the same
    # Honeywell ESV, so a write via a failover connection reaches the same
    # physical destination.
    class PrimaryOpc:
        def write(self, params):
            raise api_functions_module.OpenOPC.OPCError("primary down")

    class SecondaryOpc:
        def write(self, params):
            return "Success"

    funcs = make_funcs()
    add_connection(funcs, PrimaryOpc(), name="primary", connected=True)
    add_connection(funcs, SecondaryOpc(), name="secondary", connected=True)

    assert funcs.write(["Root.Tag1", "1"]) == "Success"


def test_all_connections_down_logs_aggregate_error_once():
    funcs = make_funcs()
    primary = add_connection(funcs, object(), name="primary", connected=True)
    secondary = add_connection(funcs, object(), name="secondary", connected=True)

    primary.mark_disconnected("boom")
    funcs._note_aggregate_state()
    assert funcs.connected is True  # secondary still up: not yet a total outage
    assert funcs.logger.calls["error"] == ["[primary] OPC connection lost: boom"]

    secondary.mark_disconnected("boom too")
    funcs._note_aggregate_state()
    assert funcs.connected is False
    assert any(
        "ALL 2 configured OPC connection(s) are down" in msg
        for msg in funcs.logger.calls["error"]
    )

    # Calling it again while still fully down must not re-log.
    funcs._note_aggregate_state()
    assert len([m for m in funcs.logger.calls["error"] if "ALL 2 configured" in m]) == 1


def test_recovery_from_all_down_logs_aggregate_warning():
    funcs = make_funcs()
    conn = add_connection(funcs, object(), name="primary", connected=True)

    conn.mark_disconnected("boom")
    funcs._note_aggregate_state()
    assert funcs.connected is False

    conn.connected = True  # simulate a successful reconnect
    funcs._note_aggregate_state()

    assert funcs.connected is True
    assert any(
        "OPC connectivity restored" in msg for msg in funcs.logger.calls["warning"]
    )
