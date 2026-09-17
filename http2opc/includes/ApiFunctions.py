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
    """One configured OPC server -- the primary, or a failover -- with its
    own OpenOPC.client() and connect/reconnect/health-check state, so
    several servers can be ticked independently from ApiFunctions.tick().
    All still driven from the same thread: OPC's COM client is bound to a
    single-threaded apartment and can't safely be touched from a second one,
    so "multiple connections" means multiple OpcConnection objects owned by
    one thread, never multiple threads sharing one.
    """

    def __init__(self, name, logger, classname, servers, host):
        self.name = name
        self.logger = logger
        self.classname = classname
        self.servers = servers
        self.host = host

        self.opc = OpenOPC.client()
        # OpenOPC.py already has trace calls sprinkled through every COM
        # operation (AddGroup/RemoveGroup/SyncRead/AsyncRefresh/Connect/...)
        # but nothing ever wired them up. This is the cheapest possible way
        # to get low-level OPC operation tracing in the field: enable with
        # [logging] level=trace, no code changes needed at the call site.
        # Tagged with the connection name now that there can be more than
        # one. logger.trace() is added to the Logger class by main.py; fall
        # back to debug for any logger that doesn't have it (e.g. a plain
        # test double or a logger obtained without going through main.py).
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

        Only appropriate for the primary at startup: there's nothing else
        for this thread to be doing yet, and the process has nothing useful
        to do until it succeeds. Failover connections connect best-effort
        instead (see ApiFunctions.init()) so a down failover server never
        delays startup.
        """
        while not self.connected:
            self._attempt_reconnect()
            if not self.connected:
                time.sleep(RECONNECT_INTERVAL_SECONDS)

    # -- Called frequently (via ApiServers' HTTPServer.service_actions(), on
    # the same thread that handles requests) so a lost connection is
    # retried, and a still-good connection is periodically double-checked,
    # without ever blocking request handling for long: this does at most one
    # quick connect/info call per tick.
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

    # -- Record that this connection is down so callers fail over to the
    # next one (or fail fast) instead of making another doomed COM call, and
    # so tick() starts retrying it. Safe to call repeatedly; only
    # logs/resets state on the transition into the down state.
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

    # -- Lightweight, side-effect-free health probe: true if the OPC server
    # responds and reports itself as 'Running'. Reconnect decisions live in
    # tick()/mark_disconnected()/_attempt_reconnect(), not here.
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

        # Optional [opc] failover=name1,name2 lists additional OPC servers,
        # tried in order after the primary whenever the primary is down --
        # see ApiFunctions._with_failover(). Each name must have its own
        # [name] section with at least `host` (classname/servers fall back
        # to the primary's, since the common case is the same OPC server
        # program reachable at a different address).
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

        # Only the primary blocks startup -- see OpcConnection.connect_blocking().
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

    # -- Detect and log the ALL-down / recovered-from-all-down transitions,
    # distinctly from any single OpcConnection's own lost/restored messages.
    # An operator needs to be able to tell "one of several redundant servers
    # dropped" (degraded, but the REST API is still serving out of the
    # others) apart from "every configured server is down" (the REST API is
    # now failing every request with 503). Call this right after anything
    # that might change a connection's up/down state.
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
                    f"are down -- requests will get 503 until at least one recovers"
                )

    # -- Try each configured connection in priority order (primary first),
    # skipping ones currently known to be down, calling `operation(opc)`
    # against the first one that's connected. On OpenOPC.OPCError, marks
    # that connection down and fails over to the next.
    #
    # Writes fail over too, not just reads: confirmed safe for this
    # deployment specifically, because these OPC servers are independent
    # front-ends that all ultimately write through to the same Honeywell
    # ESV, so a write via a failover connection reaches the same physical
    # destination and any transient inconsistency between servers settles
    # out downstream. That assumption lives here, in this one place, in case
    # it stops being true for some future server added to the list.
    #
    # Raises the last error if every connection failed, or a generic
    # OPCError if none were even connected to try (ApiServers' `if not
    # funcs.connected` gate normally short-circuits before that happens).
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

    # -- Mirror to OpenOPC's list function with non-recursive options set. You can parse in either '*' or the branch that you would like to list.
    def list(self, params):
        if not self.preferences["show_root"]:
            params = "Root." + params

        self.logger.debug(f"list({params})")
        lst = self._with_failover(lambda opc: opc.list(params, False, False, True))
        self.logger.debug(f"list({params}) -> {len(lst)} item(s)")
        return lst

    # -- Mirror to the OpenOPC's list function with recursive options set. It returns a flat list of all leaves from the parameters set. You can parse in either '*' or the branch you would like to list.
    def listRecursive(self, params):
        self.logger.debug(f"listRecursive({params})")
        lst = self._with_failover(lambda opc: opc.list(params, True, False, True))
        self.logger.debug(f"listRecursive({params}) -> {len(lst)} item(s)")
        return lst

    # -- Returns the next level of "branch" based on the parameters set. You can parse in either '*' or the branch you would like the next level of.

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

    # -- Mimics OpenOPC's read function. Parsing in a branch or leaf and it will return a tuple of values.
    # A single tag ('Root.Int4') returns OpenOPC's single-tag shape
    # (value, quality, timestamp), unchanged from before. A comma-separated
    # list ('Root.Int4,Root.Int5') batches them into one OPC call and returns
    # OpenOPC's multi-tag shape: a list of (tag, value, quality, timestamp).
    def read(self, params):
        tags = params.split(",") if "," in params else params

        # sync=True: this is a request/response proxy, not a subscriber, so
        # there's no reason to use OpenOPC's async/callback path here. The
        # async path creates a per-request COM group + event subscription
        # that (before the OpenOPC.py cleanup fix) could leak on every call,
        # and always leaked its _group_hooks entry regardless.
        self.logger.debug(f"read({tags})")
        lst = self._with_failover(lambda opc: opc.read(tags, sync=True))
        self.logger.debug(f"read({tags}) -> {lst}")

        return lst

    # -- Mimics OpenOPC's write function. `params` is [tag, value] for a
    # single write (returns a plain 'Success'/error string), or
    # [[tag1, value1], [tag2, value2], ...] for a batch write (returns a
    # list of (tag, status) pairs) -- OpenOPC's write() already dispatches
    # on this shape; see ApiServers.do_PUT for how the batch is assembled.
    def write(self, params):
        # Writes change physical/process state, so they're logged at INFO
        # (not DEBUG like the read-only paths above) -- worth having in the
        # log by default as a record of what was written and whether it
        # succeeded, without needing to turn on debug logging.
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

    # -- Mimics the OpenOPC's properties function. Parsing in a leaf name or multiple leaves will return the result that you would expect when calling it via OpenOPC
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
