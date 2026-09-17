###########################################################################
#
# API Functions for http2opc - Copyright (c) 2015-2016 Matz Persson (matz@headstation.com)
#
# OpenOPC for Python Library Module - Copyright (c) 2007-2015 Barry Barnreiter (barrybb@gmail.com)
#
###########################################################################
import configparser
import logging

from includes import ApiServers

# A level below DEBUG for OpenOPC.py's own per-COM-call tracing
# (AddGroup/SyncRead/RemoveGroup/Connect/...), wired up in ApiFunctions.init()
# via OpenOPC.client.set_trace(). Keeping it separate from DEBUG means
# "debug" gives REST/business-level call detail (list(...) -> N items, etc.)
# without also being flooded by low-level OPC wire tracing -- that's opt-in
# via level=trace specifically.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = _trace

LOG_LEVELS = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

config_filename = "main.conf"
config = configparser.ConfigParser()
with open(config_filename) as file_handle:
    config.read_file(file_handle)

log_level_name = config.get("logging", "level", fallback="info").lower()
log_level = LOG_LEVELS.get(log_level_name, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vplcd")

if log_level_name not in LOG_LEVELS:
    logger.warning(
        f"Unrecognized [logging] level '{log_level_name}' in {config_filename}, defaulting to info"
    )

logger.info(f"Starting http2opc daemon (config file: {config_filename})")

server_ip = config.get("rest", "server_ip")
server_port = int(config.get("rest", "server_port"))


if server_ip:
    logger.info("RestApi Server IP: " + server_ip)
else:
    logger.info("RestApi Server IP: Listening on ALL NICs")

logger.info("RestApi Server Port: " + str(server_port))

rest = ApiServers.RestApiServer(logger, server_ip, server_port)
rest.start()
