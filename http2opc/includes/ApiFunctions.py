###########################################################################
#
# API Functions for http2opc
#
# OpenOPC for Python Library Module - Copyright (c) 2007-2015 Barry Barnreiter (barrybb@gmail.com)
#
###########################################################################

import configparser
import time

from . import OpenOPC

# Minimum gap between reconnect attempts once the connection is known down.
RECONNECT_INTERVAL_SECONDS = 5
# How often to proactively probe the OPC server while nominally connected,
# so a drop is detected even during a lull in request traffic.
HEALTH_CHECK_INTERVAL_SECONDS = 30
# While an outage is ongoing, how often to re-log it at WARNING (rather than
# DEBUG) so a long outage stays visible in normal-verbosity logs, not just
# at the moment it started and the moment it's resolved.
OUTAGE_RELOG_INTERVAL_SECONDS = 60


class OpcConnection:
    """One configured OPC server (primary or failover), with its own
    OpenOPC.client() and reconnect/health-check state. Still single-
    threaded: OPC's COM client can't be touched from a second thread, so
    multiple connections means multiple objects on one thread, not threads.
    """

    def __init__(self, name, logger, classname, servers, host):
        self.name = name
        self.logger = logger
        self.classname = classname
        self.servers = servers
        self.host = host

        self.opc = OpenOPC.client()
        # Wires up OpenOPC.py's own (otherwise dead) per-COM-call tracing to
        # the `trace` log level, tagged with this connection's name.
        # logger.trace() is added by main.py; fall back to debug if absent.
        trace_log = getattr(self.logger, "trace", self.logger.debug)
        self.opc.set_trace(lambda msg: trace_log(f"OpenOPC[{self.name}]: {msg}"))

        self.connected = False
        self._ever_connected = False
        self._disconnected_at = time.time()
        self._next_reconnect_attempt = 0
        self._next_health_check = 0
        self._last_outage_log = 0

    def connect_blocking(self):
        """Block, retrying, until connected.

        Only for the primary at startup, when there's nothing else to do
        yet. Failover connections connect best-effort instead (init()) so a
        down failover never delays startup.
        """
        while not self.connected:
            self._attempt_reconnect()
            if not self.connected:
                time.sleep(RECONNECT_INTERVAL_SECONDS)

    # Called every service_actions() poll cycle on the request-handling
    # thread: retries a lost connection, or periodically re-checks a good
    # one, without ever blocking request handling for long.
    def tick(self):
        now = time.time()

        if self.connected:
            if now >= self._next_health_check:
                self._next_health_check = now + HEALTH_CHECK_INTERVAL_SECONDS
                if self.ping():
                    self.logger.debug(f"[{self.name}] health check OK")
                else:
                    self.mark_disconnected("periodic health check failed")
        else:
            if now >= self._next_reconnect_attempt:
                self._next_reconnect_attempt = now + RECONNECT_INTERVAL_SECONDS
                self._attempt_reconnect()

    # Marks this connection down so callers fail over / fail fast instead
    # of retrying a doomed call. Idempotent: only acts on the transition.
    def mark_disconnected(self, reason):
        if self.connected:
            self.connected = False
            self._disconnected_at = time.time()
            self._last_outage_log = self._disconnected_at
            self._next_reconnect_attempt = 0
            self.logger.error(f"[{self.name}] OPC connection lost: {reason}")

    def _attempt_reconnect(self):
        self.logger.debug(
            f"[{self.name}] Attempting OPC connect({self.servers}, {self.host})"
        )
        try:
            result = self.opc.connect(self.servers, self.host)
        except OpenOPC.OPCError as err:
            result = False
            self.logger.debug(f"[{self.name}] OPC reconnect attempt failed: {err}")

        if result:
            if self._ever_connected:
                outage = time.time() - self._disconnected_at
                self.logger.warning(
                    f"[{self.name}] OPC connection re-established after {outage:.0f}s"
                )
            else:
                self.logger.info(f"[{self.name}] Connected to OPC server")
                self._ever_connected = True
            self.connected = True
            self._disconnected_at = None
        else:
            now = time.time()
            if now - self._last_outage_log >= OUTAGE_RELOG_INTERVAL_SECONDS:
                self._last_outage_log = now
                outage = now - self._disconnected_at
                self.logger.warning(
                    f"[{self.name}] OPC still unreachable after {outage:.0f}s, "
                    f"retrying every {RECONNECT_INTERVAL_SECONDS}s"
                )

    # Side-effect-free health probe: true if the server reports 'Running'.
    def ping(self):
        try:
            info = self.opc.info()
        except OpenOPC.OPCError as err:
            self.logger.debug(f"[{self.name}] Health check failed: {err}")
            return False

        for prop_name, value in info:
            if prop_name == "State":
                if value != "Running":
                    self.logger.warning(
                        f"[{self.name}] OPC server reports state={value} "
                        f"(expected Running)"
                    )
                return value == "Running"

        self.logger.debug(f"[{self.name}] Health check: no State reported")
        return False


class ApiFunctions:
    def __init__(self, logger):
        self.logger = logger
        self.connections = []
        self._all_down_since = None

    def init(self):
        config_filename = "main.conf"
        self.logger.debug(f"Loading config from {config_filename}")
        self.config = configparser.ConfigParser()
        with open(config_filename) as file_handle:
            self.config.read_file(file_handle)

        primary_classname = self.config.get("opc", "classname")
        primary_servers = self.config.get("opc", "servers")
        primary_host = self.config.get("opc", "host")

        self.logger.info("Opc Class: " + primary_classname)
        self.logger.info("Opc Servers: " + primary_servers)
        self.logger.info("Opc Host: " + primary_host)

        self.preferences = {}
        self.preferences["show_root"] = self.config.getboolean(
            "preferences", "show_root"
        )
        self.logger.debug(f"Preferences: {self.preferences}")

        primary = OpcConnection(
            "primary", self.logger, primary_classname, primary_servers, primary_host
        )
        self.connections = [primary]

        # Optional: [opc] failover=name1,name2 lists additional servers,
        # tried in order when the primary is down (see _with_failover()).
        # Each needs its own [name] section with `host`; classname/servers
        # default to the primary's.
        failover_names = [
            name.strip()
            for name in self.config.get("opc", "failover", fallback="").split(",")
            if name.strip()
        ]
        for name in failover_names:
            self.connections.append(
                OpcConnection(
                    name,
                    self.logger,
                    self.config.get(name, "classname", fallback=primary_classname),
                    self.config.get(name, "servers", fallback=primary_servers),
                    self.config.get(name, "host"),
                )
            )
        if failover_names:
            self.logger.info(
                f"Configured {len(self.connections)} OPC connections "
                f"(primary + failover: {', '.join(failover_names)})"
            )

        # Only the primary blocks startup; see OpcConnection.connect_blocking().
        primary.connect_blocking()

        # Failover connections connect best-effort; tick() keeps retrying
        # them afterward if they're not up yet, same as any other reconnect.
        for conn in self.connections[1:]:
            conn._attempt_reconnect()

        return True

    @property
    def connected(self):
        """True if at least one configured OPC connection is currently up."""
        return any(conn.connected for conn in self.connections)

    def tick(self):
        for conn in self.connections:
            conn.tick()
            self._note_aggregate_state()

    # Logs the ALL-down / recovered transitions distinctly from any single
    # connection's own messages, so "one of several dropped" (still
    # serving) reads differently from "everything is down" in the logs.
    def _note_aggregate_state(self):
        if self.connected:
            if self._all_down_since is not None:
                outage = time.time() - self._all_down_since
                self.logger.warning(
                    f"OPC connectivity restored (at least one of "
                    f"{len(self.connections)} configured connection(s) is up) "
                    f"after {outage:.0f}s fully down"
                )
                self._all_down_since = None
        else:
            if self._all_down_since is None:
                self._all_down_since = time.time()
                self.logger.error(
                    f"ALL {len(self.connections)} configured OPC connection(s) "
                    f"are down. Requests will get 503 until at least one recovers"
                )

    # Tries each connection in priority order (primary first, skipping ones
    # known down) until operation(opc) succeeds. On OPCError, marks that
    # connection down and moves to the next. Writes fail over too; that's
    # safe here because these servers all write through to the same
    # Honeywell ESV (see README). Raises the last error once every
    # connection has failed.
    def _with_failover(self, operation):
        last_err = None
        for conn in self.connections:
            if not conn.connected:
                continue
            try:
                return operation(conn.opc)
            except OpenOPC.OPCError as err:
                conn.mark_disconnected(str(err))
                self._note_aggregate_state()
                last_err = err
        if last_err is not None:
            raise last_err
        raise OpenOPC.OPCError("No OPC connections available")

    # Non-recursive; pass '*' or a branch path.
    def list(self, params):
        if not self.preferences["show_root"]:
            params = "Root." + params

        self.logger.debug(f"list({params})")
        lst = self._with_failover(lambda opc: opc.list(params, False, False, True))
        self.logger.debug(f"list({params}) -> {len(lst)} item(s)")
        return lst

    # Recursive: returns every leaf under the given branch (or '*').
    def listRecursive(self, params):
        self.logger.debug(f"listRecursive({params})")
        lst = self._with_failover(lambda opc: opc.list(params, True, False, True))
        self.logger.debug(f"listRecursive({params}) -> {len(lst)} item(s)")
        return lst

    # Next level of "branch" only, not fully recursive.
    def listTree(self, params):
        if not self.preferences["show_root"]:
            params = "Root." + params

        self.logger.debug(f"listTree({params})")
        lst = self._with_failover(lambda opc: opc.list(params, False, False, True))
        parent = params[:-1]
        tree = []
        description = None
        for leaf in lst:
            leaf_stripped = leaf[0].replace(parent, "")

            split = leaf_stripped.split(".")

            if split[1] == "Value":
                final_leaf = [leaf[0], "Leaf"]

            elif split[2] == "Value":
                values = self.properties(leaf[0])
                for v in values:
                    if v[2] == "Item Description":
                        description = v[3]

                final_leaf = [split[0], "Branch", description]

            else:
                final_leaf = [split[0], "Branch", description]

            if final_leaf not in tree:
                tree.append(final_leaf)

        self.logger.debug(f"listTree({params}) -> {len(tree)} node(s)")
        return tree

    def listOneDeep(self, params):
        if not self.preferences["show_root"]:
            params = "Root." + params

        self.logger.debug(f"listOneDeep({params})")
        lst = self._with_failover(lambda opc: opc.list(params, False, False, True))

        new_params = [leaf[0] for leaf in lst]

        new_lst = self.properties(new_params)

        branch = {}
        for tag_row in new_lst:
            if tag_row[0] not in branch:
                branch[tag_row[0]] = []

            branch[tag_row[0]].append(tag_row)

        for rows in branch.values():
            rows.append("Leaf")

        self.logger.debug(f"listOneDeep({params}) -> {len(branch)} branch(es)")
        return branch

    # A single tag keeps OpenOPC's single-tag shape (value, quality,
    # timestamp); a comma-separated list batches into one OPC call and
    # returns the multi-tag shape (tag, value, quality, timestamp).
    def read(self, params):
        tags = params.split(",") if "," in params else params

        # Always synchronous: the async path creates a per-request COM
        # group + event subscription that isn't needed here and used to
        # leak (see OpenOPC.iread).
        self.logger.debug(f"read({tags})")
        lst = self._with_failover(lambda opc: opc.read(tags, sync=True))
        self.logger.debug(f"read({tags}) -> {lst}")

        return lst

    # `params` is [tag, value] for a single write, or a list of such pairs
    # for a batch (OpenOPC.write() dispatches on the shape; see
    # ApiServers.do_PUT for how a batch is assembled).
    def write(self, params):
        # State-changing, so logged at INFO (not DEBUG) by default.
        self.logger.info(f"write({params})")
        success = self._with_failover(lambda opc: opc.write(params))

        if isinstance(success, list):
            failures = [row for row in success if row[1] != "Success"]
            if failures:
                succeeded = len(success) - len(failures)
                self.logger.warning(
                    f"write({params}) -> {succeeded}/{len(success)} succeeded, "
                    f"failed: {failures}"
                )
            else:
                self.logger.info(f"write({params}) -> all {len(success)} succeeded")
        elif success == "Success":
            self.logger.info(f"write({params}) -> Success")
        else:
            self.logger.warning(f"write({params}) -> {success}")

        return success

    # Accepts a single leaf name or a comma-separated list of leaves.
    def properties(self, params, as_json=False):
        if type(params).__name__ == "list":
            split = params
        else:
            split = params.split(",")

        self.logger.debug(f"properties({split}, as_json={as_json})")
        lst = self._with_failover(lambda opc: opc.properties(split))

        if as_json:
            lst = self.buildJsonList(lst)

        self.logger.debug(f"properties({split}) -> {len(lst)} row(s)")
        return lst

    def buildJsonList(self, results):
        branch = []
        leaf = {}

        for result in results:
            if "Item ID (virtual property)" not in leaf:
                leaf = {}

            elif leaf["Item ID (virtual property)"] != result[0]:
                branch.append(leaf)
                leaf = {}

            leaf[result[2]] = result[3]

        branch.append(leaf)

        return branch

    def search(self, params):
        params = "*" + params + "*"

        self.logger.debug(f"search({params})")
        lst = self._with_failover(lambda opc: opc.list(params, False, False, True))
        self.logger.debug(f"search({params}) -> {len(lst)} item(s)")
        return lst

    def testing(self, params):
        params = "*" + params + "*"

        self.logger.debug(f"testing({params})")
        lst = self._with_failover(lambda opc: opc.list(params, False, False, True))
        self.logger.debug(f"testing({params}) -> {len(lst)} item(s)")

        return lst

    def test_call(self):
        self.logger.debug("test_call()")
        lst = self._with_failover(lambda opc: opc.info())
        self.logger.debug(f"test_call() -> {lst}")
        return lst
