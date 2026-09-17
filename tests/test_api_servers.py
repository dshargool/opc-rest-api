import json
import threading
import urllib.error
import urllib.request

import pytest

from http2opc.includes import ApiServers, OpenOPC


class FakeLogger:
    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class FakeFuncs:
    """Stands in for ApiFunctions at the ApiServers layer. Note that real
    ApiFunctions methods handle connection failover/marking-down internally
    (see ApiFunctions._with_failover) and only ever raise OpenOPC.OPCError
    once every configured connection has failed. ApiServers just reports
    that as a 503; it doesn't manage connection state itself. This fake
    mimics that end state (self.connected flips to False) without
    simulating the failover loop itself, which is covered by
    test_api_functions.py instead."""

    def __init__(self):
        self.connected = True

    def tick(self):
        pass

    def list(self, m):
        if m == "Bad.Tag":
            self.connected = False
            raise OpenOPC.OPCError("boom")
        return {"called": "list", "m": m}

    def listRecursive(self, m):
        return {"called": "listRecursive", "m": m}

    def listTree(self, m):
        return {"called": "listTree", "m": m}

    def listOneDeep(self, m):
        return {"called": "listOneDeep", "m": m}

    def read(self, m):
        if m == "@MemFree,Root.Tag1":
            # Mirrors the real ApiFunctions.read(): OpenOPC.client.read()
            # raises a bare TypeError (not OpenOPC.OPCError) for mixing
            # health and OPC tags in one call.
            raise TypeError(
                "system health and OPC tags cannot be included in the same group"
            )
        return {"called": "read", "m": m}

    def properties(self, m, as_json):
        return {"called": "properties", "m": m, "as_json": as_json}

    def search(self, m):
        return {"called": "search", "m": m}

    def testing(self, m):
        return {"called": "testing", "m": m}

    def test_call(self):
        return {"called": "test_call"}

    def write(self, params):
        if isinstance(params[0], (list, tuple)):
            return [
                (loc, "Error" if loc == "Fail.Tag" else "Success")
                for loc, _val in params
            ]
        loc, _val = params
        if loc == "Fail.Tag":
            return "Error"
        return "Success"


@pytest.fixture
def server():
    ApiServers.logger = FakeLogger()
    ApiServers.funcs = FakeFuncs()
    httpd = ApiServers.OpcHTTPServer(("127.0.0.1", 0), ApiServers.RestRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join()


def _request(server, path, method="GET"):
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_get_list_passes_m_param(server):
    status, body = _request(server, "/method=list&m=*")
    assert status == 200
    assert body == {"called": "list", "m": "*"}


def test_get_multiple_query_params_do_not_clobber_each_other(server):
    # Regression test: the old hand-rolled parser only kept the last
    # non-'method' key it saw, so 'm' silently disappeared if another
    # unrelated param came after it in the query string.
    status, body = _request(server, "/method=properties&extra=ignored&m=Root.Tag1")
    assert status == 200
    assert body == {"called": "properties", "m": "Root.Tag1", "as_json": False}


def test_get_jsonproperties(server):
    status, body = _request(server, "/method=jsonproperties&m=Root.Tag1")
    assert status == 200
    assert body == {"called": "properties", "m": "Root.Tag1", "as_json": True}


def test_get_testcall_needs_no_params(server):
    status, body = _request(server, "/method=testcall")
    assert status == 200
    assert body == {"called": "test_call"}


def test_get_missing_method_is_400(server):
    status, body = _request(server, "/m=*")
    assert status == 400
    assert "method" in body["error"]


def test_get_unknown_method_is_404(server):
    status, _body = _request(server, "/method=bogus&m=*")
    assert status == 404


def test_get_missing_required_param_is_400(server):
    status, body = _request(server, "/method=list")
    assert status == 400
    assert "m" in body["error"]


def test_get_opc_error_is_503_and_marks_disconnected(server):
    status, body = _request(server, "/method=list&m=Bad.Tag")
    assert status == 503
    assert body["error"] == "boom"
    assert ApiServers.funcs.connected is False


def test_get_unexpected_exception_is_500_not_a_raw_crash(server):
    # Regression test: a bare (non-OpenOPC.OPCError) exception from deeper
    # in ApiFunctions/OpenOPC -- e.g. mixing health and OPC tags in one
    # read -- used to propagate straight out of do_GET uncaught instead of
    # becoming a JSON response like every other error path here.
    status, body = _request(server, "/method=read&m=@MemFree,Root.Tag1")
    assert status == 500
    assert "health and OPC tags" in body["error"]


def test_get_fails_fast_when_already_disconnected(server):
    ApiServers.funcs.connected = False

    status, body = _request(server, "/method=list&m=*")

    assert status == 503
    assert "unavailable" in body["error"]


def test_put_fails_fast_when_already_disconnected(server):
    ApiServers.funcs.connected = False

    status, body = _request(server, "/method=write&loc=Root.Int4&val=1", method="PUT")

    assert status == 503
    assert "unavailable" in body["error"]


def test_put_write_with_loc_val(server):
    status, body = _request(
        server, "/method=write&loc=Root.Int4&val=123.0", method="PUT"
    )
    assert status == 200
    assert body == "Success"


def test_put_write_with_m_s_aliases(server):
    status, body = _request(server, "/method=write&m=Root.Int4&s=123.0", method="PUT")
    assert status == 200
    assert body == "Success"


def test_put_write_failure_is_500(server):
    status, _body = _request(server, "/method=write&loc=Fail.Tag&val=1", method="PUT")
    assert status == 500


def test_put_write_missing_value_is_400(server):
    status, _body = _request(server, "/method=write&loc=Root.Int4", method="PUT")
    assert status == 400


def test_put_batch_write_all_success(server):
    status, body = _request(
        server, "/method=write&loc=A&val=1&loc=B&val=2", method="PUT"
    )
    assert status == 200
    assert body == [["A", "Success"], ["B", "Success"]]


def test_put_batch_write_with_m_s_aliases(server):
    status, body = _request(server, "/method=write&m=A&s=1&m=B&s=2", method="PUT")
    assert status == 200
    assert body == [["A", "Success"], ["B", "Success"]]


def test_put_batch_write_partial_failure_is_500_with_per_tag_detail(server):
    status, body = _request(
        server, "/method=write&loc=Fail.Tag&val=1&loc=B&val=2", method="PUT"
    )
    assert status == 500
    assert body == [["Fail.Tag", "Error"], ["B", "Success"]]


def test_put_batch_write_mismatched_counts_is_400(server):
    status, body = _request(server, "/method=write&loc=A&loc=B&val=1", method="PUT")
    assert status == 400
    assert "Mismatched" in body["error"]


def test_put_unknown_method_is_404(server):
    status, _body = _request(server, "/method=bogus", method="PUT")
    assert status == 404
