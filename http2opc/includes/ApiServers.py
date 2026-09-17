import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

from . import OpenOPC
from .ApiFunctions import ApiFunctions

logger = None
funcs = None


class MissingParameterError(ValueError):
    pass


def _send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(body)


def _get_param(qs, name):
    values = qs.get(name)
    if not values:
        return None
    return values[0]


def _require_param(qs, name):
    value = _get_param(qs, name)
    if value is None:
        raise MissingParameterError(f"Missing required parameter '{name}'")
    return value


GET_METHODS = {
    "list": lambda qs: funcs.list(_require_param(qs, "m")),
    "listrecursive": lambda qs: funcs.listRecursive(_require_param(qs, "m")),
    "listtree": lambda qs: funcs.listTree(_require_param(qs, "m")),
    "listonedeep": lambda qs: funcs.listOneDeep(_require_param(qs, "m")),
    "read": lambda qs: funcs.read(_require_param(qs, "m")),
    "properties": lambda qs: funcs.properties(_require_param(qs, "m"), False),
    "jsonproperties": lambda qs: funcs.properties(_require_param(qs, "m"), True),
    "search": lambda qs: funcs.search(_require_param(qs, "m")),
    "testing": lambda qs: funcs.testing(_require_param(qs, "m")),
    "testcall": lambda qs: funcs.test_call(),
}


class OpcHTTPServer(HTTPServer):
    """Drives ApiFunctions' connection watchdog via service_actions(),
    called every serve_forever() poll cycle on the request-handling thread.
    That's required since OPC's COM client can't be touched from another
    thread. Reconnects without ever blocking a client request for long.
    """

    def service_actions(self):
        funcs.tick()


class RestApiServer:
    def __init__(self, console, server_ip="", server_port=8003):

        global logger
        logger = console

        self.server_ip = server_ip
        self.server_port = server_port

        self.httpd = OpcHTTPServer(
            (self.server_ip, self.server_port), RestRequestHandler
        )
        logger.info("Started REST API Server on port " + str(self.server_port))

        global funcs
        funcs = ApiFunctions(logger)

    def start(self):
        """Connect to the OPC server, then serve requests until interrupted."""
        if funcs.init():
            self.httpd.serve_forever()


class RestRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):

        qs = parse_qs(self.path[1:], keep_blank_values=True)
        method = _get_param(qs, "method")

        if not method:
            logger.debug(f"GET {self.path}: missing 'method' parameter")
            _send_json(self, 400, {"error": "Missing required parameter 'method'"})
            return

        handler = GET_METHODS.get(method.lower())
        if handler is None:
            # Likely a caller integration bug (typo'd/unsupported method),
            # unlike the routine 4xx cases above, so it's worth INFO
            # visibility by default rather than requiring debug to notice it.
            logger.info(f"GET {self.path}: unknown method '{method}'")
            _send_json(self, 404, {"error": f"Unknown method '{method}'"})
            return

        if not funcs.connected:
            logger.debug(f"GET {self.path}: rejected, OPC server unavailable")
            _send_json(self, 503, {"error": "OPC server unavailable, reconnecting"})
            return

        try:
            result = handler(qs)
        except MissingParameterError as err:
            logger.debug(f"GET {self.path}: {err}")
            _send_json(self, 400, {"error": str(err)})
            return
        except OpenOPC.OPCError as err:
            # Already failed over across every configured connection (see
            # ApiFunctions._with_failover) before raising this.
            logger.debug(f"OPC error handling {method}: {err}")
            _send_json(self, 503, {"error": str(err)})
            return

        _send_json(self, 200, result)

    def do_PUT(self):

        qs = parse_qs(self.path[1:], keep_blank_values=True)
        method = _get_param(qs, "method")

        if not method:
            logger.debug(f"PUT {self.path}: missing 'method' parameter")
            _send_json(self, 400, {"error": "Missing required parameter 'method'"})
            return

        if method.lower() != "write":
            logger.info(f"PUT {self.path}: unknown method '{method}'")
            _send_json(self, 404, {"error": f"Unknown method '{method}'"})
            return

        # Repeating loc/val (or m/s), e.g. loc=A&val=1&loc=B&val=2, batches
        # multiple writes into one OPC call instead of one round-trip per tag.
        locations = qs.get("loc") or qs.get("m")
        values = qs.get("val") or qs.get("s")

        if not locations or not values:
            logger.debug(f"PUT {self.path}: missing loc/val (or m/s) parameters")
            _send_json(
                self,
                400,
                {
                    "error": "'write' requires 'loc' and 'val' parameters (or 'm' and 's'); "
                    "repeat the pair for a batch write"
                },
            )
            return

        if len(locations) != len(values):
            logger.debug(
                f"PUT {self.path}: mismatched loc/val counts "
                f"({len(locations)} vs {len(values)})"
            )
            _send_json(
                self,
                400,
                {
                    "error": f"Mismatched number of loc/val (or m/s) parameters: "
                    f"{len(locations)} vs {len(values)}"
                },
            )
            return

        write_params = (
            [locations[0], values[0]]
            if len(locations) == 1
            else list(zip(locations, values))
        )

        if not funcs.connected:
            logger.debug(f"PUT {self.path}: rejected, OPC server unavailable")
            _send_json(self, 503, {"error": "OPC server unavailable, reconnecting"})
            return

        try:
            result = funcs.write(write_params)
        except OpenOPC.OPCError as err:
            # See the matching comment in do_GET.
            logger.debug(f"OPC error handling write: {err}")
            _send_json(self, 503, {"error": str(err)})
            return

        if isinstance(result, list):
            # (tag, status) pairs; 200 only if every tag succeeded, so a
            # status-code-only check can't miss a partial failure.
            all_succeeded = all(row[1] == "Success" for row in result)
            _send_json(self, 200 if all_succeeded else 500, result)
        elif result == "Success":
            _send_json(self, 200, result)
        else:
            # ApiFunctions.write() already logged this at warning.
            _send_json(self, 500, {"error": result})

    def log_message(self, format, *args):
        # Redirect the base class's access-log line through our logger
        # instead of stderr (request line + status, not just the client IP).
        logger.debug(f"{self.client_address[0]} - {format % args}")
