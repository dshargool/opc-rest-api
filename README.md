# OPC Rest Api
Python Rest API using OpenOPC to provide direct API access for any OS platform. This proxy has to be installed on a Windows box with Python and OpenOPC installed to provide a API Gateway to other platforms.

# Dependencies
This code vendors a Python 3 port of OpenOPC (originally by Barry Barnreiter, https://sourceforge.net/projects/openopc/files/) and requires Python 3 plus `pywin32` on the Windows host that runs it. See `requirements.txt`.

# Installation
On your Windows server, install `pywin32` (`pip install -r requirements.txt`). Make sure the vendored OpenOPC client connects properly to your OPC Server; see http://openopc.sourceforge.net/ for background on OPC DA and COM/DCOM setup. Once that's confirmed, download the http2opc code above and drop it in your preferred directory.

# Running as a Windows service (NSSM)
`main.py` is a plain foreground script (it reads `main.conf` from its own
directory and blocks in `serve_forever()`), so it needs a service wrapper to
run unattended and restart on boot/crash. [NSSM](https://nssm.cc/) is the
usual choice for this.

The app itself logs to stderr (there's no file-logging or rotation built into
`main.py`; see `[logging] level=` in `main.conf` for verbosity), so let NSSM
capture and rotate that output rather than trying to configure Python-side
file logging:

```bat
:: Install the service, pointing at your Python interpreter and main.py
nssm install Http2Opc "C:\Python3\python.exe" "C:\opc-rest-api\http2opc\main.py"

:: main.conf is loaded via a relative path, so the working directory matters
nssm set Http2Opc AppDirectory "C:\opc-rest-api\http2opc"

:: Capture stdout/stderr (where the logger writes) to a file
mkdir C:\opc-rest-api\logs
nssm set Http2Opc AppStdout "C:\opc-rest-api\logs\http2opc.log"
nssm set Http2Opc AppStderr "C:\opc-rest-api\logs\http2opc.log"

:: Rotate that file instead of letting it grow forever: NSSM renames the
:: existing file (with a timestamp suffix) and starts a fresh one whenever
:: either threshold below is hit. AppRotateOnline lets rotation happen while
:: the service keeps running, with no restart needed.
nssm set Http2Opc AppRotateFiles 1
nssm set Http2Opc AppRotateOnline 1
nssm set Http2Opc AppRotateBytes 10485760
nssm set Http2Opc AppRotateSeconds 86400

nssm start Http2Opc
```

(`nssm edit Http2Opc` opens a GUI for the same settings, under the I/O and
Rotation tabs, if you'd rather not use the command line for all of it.)

Set `[logging] level=` in `main.conf` to `debug` or `trace` temporarily if
you need more detail while diagnosing an issue in the field; see
[Operations](#operations) below for what each level shows.

# Development / Testing
The REST layer (`ApiFunctions`, `ApiServers`) is fully unit-testable on any platform using a mocked OPC client, since it never touches `win32com` directly. The vendored OpenOPC.py itself can only be exercised against a real OPC server on Windows.

```bash
pip install -r requirements-dev.txt
pytest
```

# Operations
If the OPC server connection drops, the service does not exit or require a
restart: it fails requests fast with HTTP 503 while reconnecting in the
background (checked/retried on every request-handling cycle, roughly twice a
second), and resumes serving normally once the OPC server is reachable again.

`[logging] level=` in `main.conf` controls how much of this is visible:
- `info` (the default): the connection being lost (once) and restored (with
  outage duration), a recurring `warning` every 60s while an outage
  continues, and every write (state-changing) with its result.
- `debug`: adds per-request call detail for every function (params + result
  count) and per-attempt reconnect detail.
- `trace`: adds OpenOPC's own low-level per-COM-call tracing (AddGroup,
  SyncRead, RemoveGroup, Connect, ...). Several lines per request; use only
  when `debug` isn't enough to see what's going wrong.

## Multiple OPC servers (failover)

If you have more than one OPC server that can answer the same tags, list the
extras under `[opc] failover=` in `main.conf` (see the commented-out example
there). Requests try the primary first, then each failover server in order,
skipping any currently known to be down; a request only gets a 503 once
*every* configured server has failed. Each server gets its own independent
reconnect/health-check cycle, and its own `[name] ...` prefix on every
connection-related log line so you can tell which one an event is about.

**This applies to writes too, not just reads.** A write can be served by
any currently-healthy configured server, not only the primary. That's only
safe if a write via any of them reaches the same real destination (e.g.
independent OPC front-ends that all ultimately write through to the same
underlying controller, where a momentary difference between servers settles
out downstream). If your servers are instead genuinely independent systems
where writing to the wrong one would be wrong, not just delayed, don't
configure them as failovers of each other this way.

If every configured server is down, that's logged distinctly from any one
server dropping (`ALL N configured OPC connection(s) are down`) so it's
obvious from the logs alone whether an outage is "one of several redundant
servers is down" (degraded, still serving) or "the whole thing is down"
(every request will 503 until at least one recovers).

# Usage
Taken (and expanded upon) from: http://headstation.com/archives/using-opc-rest-api/

Examples below assume the service is running on the same machine you're
querying from, on the default port (8003); substitute your server's actual
address otherwise. Every response is JSON. Errors (4xx/5xx) come back as
`{"error": "..."}`; see [Status codes](#status-codes) below.

## Functions
### List
This function is a mirror to OpenOPC’s list function with non-recursive options set. You can parse in either ‘*’ or the branch that you would like to list.

Request:
```bash
curl http://127.0.0.1:8003/method=list&m=*
curl http://127.0.0.1:8003/method=list&m=Root.*
```
Response:
```json
[["Root.Boilers.#1 Boiler.FI8110.PV.Value", "Leaf"], ["Root.Boilers.#1 Boiler.PI8110.PV.Value", "Leaf"], ["Root.Boilers.#1 Boiler.TI8110.PV.Value", "Leaf"]]
```

### List Recursive
This function is a custom list function that was designed to simulate branching when the database has been set up as a flat directory where leaves are categorized by points. It returns the next level of “branch” based on the parameters set. You can parse in either ‘*’ or the branch you would like the next level of. Request example:
```bash
curl http://127.0.0.1:8003/method=listtree&m=*
curl http://127.0.0.1:8003/method=listtree&m=Root.*
```
Response:
```json
[["Boilers", "Branch", null], ["Cane Prep & Milling", "Branch", null], ["Cane Receivals", "Branch", null], ["Evaporation", "Branch", null], ["Injection Water", "Branch", null], ["Juice Treatment", "Branch", null], ["Pan Stage", "Branch", null], ["Powerhouse", "Branch", null], ["Sugar & Molasses Handling", "Branch", null], ["Wireless Data", "Branch", null]]
```
### Read
This function mimics OpenOPC’s read function. Parsing in a branch or leaf and it will return a tuple of values.

Request:
```bash
curl http://127.0.0.1:8003/method=read&m=Root.Int4
```
Response:
```json
[123.0, "Good", "2024-01-01 12:00:00"]
```

Requesting multiple comma-separated tags batches them into a single OPC call instead of one request per tag, and returns a list of `[tag, value, quality, timestamp]` rows instead of the single-tag shape above:
```bash
curl http://127.0.0.1:8003/method=read&m=Root.Int4,Root.Int5
```
```json
[["Root.Int4", 123.0, "Good", "2024-01-01 12:00:00"], ["Root.Int5", 45.6, "Good", "2024-01-01 12:00:00"]]
```

### Write
This function mimics OpenOPC's write function. Parsing in a branch or leaf and a value and returning a boolean success or fail. Writes are `PUT` requests, not `GET`.

Request:
```bash
curl -X PUT http://127.0.0.1:8003/method=write&m=Root.Int4&s=123.0
```

Write requests also accept named request parameters so that we can specify the location (*loc*) of the write and the value (*val*) being written
```bash
curl -X PUT http://127.0.0.1:8003/method=write&loc=Root.Int4&val=123.0
```
Response (200 on success):
```json
"Success"
```

Repeating the `loc`/`val` (or `m`/`s`) pair batches multiple writes into a single OPC call instead of one request per tag:
```bash
curl -X PUT "http://127.0.0.1:8003/method=write&loc=Root.Int4&val=123.0&loc=Root.Int5&val=45.6"
```
Response is a list of `[tag, status]` pairs. 200 only if every write succeeded, 500 if any failed (the body still shows exactly which ones, either way):
```json
[["Root.Int4", "Success"], ["Root.Int5", "Success"]]
```

### Properties
This mimics the OpenOPC’s properties function. Parsing in a leaf name or multiple leaves will return the result that you would expect when calling it via OpenOPC, unformatted.
```bash
curl "http://127.0.0.1:8003/method=properties&m=Root.Powerhouse.Common%20Items.Auto%20Export%20Functions.PY7700.SW.Value"
# or, multiple leaves:
curl "http://127.0.0.1:8003/method=properties&m=Root.Powerhouse.Common%20Items.Auto%20Export%20Functions.PY7700.SW.Value,Root.Powerhouse.Common%20Items.EI7703.PV.Value"
```
### JSON Properties
This is a custom function that takes the results of the OpenOPC’s properties function and formats it to a more common array structure. Parsing in a leaf name or multiple leaves will return an array for each leaf’s properties.
```bash
curl "http://127.0.0.1:8003/method=jsonproperties&m=Root.Powerhouse.Common%20Items.Auto%20Export%20Functions.PY7700.SW.Value"
# or, multiple leaves:
curl "http://127.0.0.1:8003/method=jsonproperties&m=Root.Powerhouse.Common%20Items.Auto%20Export%20Functions.PY7700.SW.Value,Root.Powerhouse.Common%20Items.EI7703.PV.Value"
```
### Search
This is a search utility that goes through OpenOPC’s list function attempting to locate branches and leaves with the parsed params.
```bash
curl http://127.0.0.1:8003/method=search&m=Common
```

## Status codes
- `200`: success (see each function above for the response shape).
- `400`: missing or malformed parameters (e.g. no `method`, no `m`, mismatched batch `loc`/`val` counts).
- `404`: unknown `method`.
- `500`: the write (or batch write) itself failed at the OPC server; for a batch, check the response body for which tag(s) failed.
- `503`: the OPC server connection is currently down. The service is retrying in the background (see [Operations](#operations) below); safe to retry the request shortly.

# Copyright
Copyright 2016 Headstation. (http://headstation.com) All rights reserved. The http2opc REST wrapper (everything outside `http2opc/includes/OpenOPC.py`) is free software and may be redistributed under the terms specified in the `License` file (Apache License 2.0).

`http2opc/includes/OpenOPC.py` is a separately-licensed, vendored dependency: Copyright 2007-2015 Barry Barnreiter and contributors, licensed under the GNU GPL v2 with a special linking exception (permitting it to be linked into this Apache-2.0-licensed project). See `LICENSE-OpenOPC.txt` for the exact terms. 
