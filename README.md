# OPC Rest Api
Python Rest API using OpenOPC to provide direct API access for any OS platform. This proxy has to be installed on a Windows box with Python and OpenOPC installed to provide a API Gateway to other platforms.

# Dependencies
This code is dependent on https://sourceforge.net/projects/openopc/files/ by Barry Barnreiter and Python 2.7

# Installation
On your Windows server, install OpenOpc. Make sure that OpenOPC connects properly to your OPC Server. Documentation for OpenOPC on http://openopc.sourceforge.net/. Once OpenOPC is connected, download http2opc code above and drop it in your preferred diretory.

# Usage
Taken (and expanded upon) from: http://headstation.com/archives/using-opc-rest-api/
## Functions
### List
This function is a mirror to OpenOPC’s list function with non-recursive options set. You can parse in either ‘*’ or the branch that you would like to list.

Request:
```bash 
http://172.20.60.43:8003/method=list&m=*
http://172.20.60.43:8003/method=list&m=Root.*
```
Response:
```bash 
[["Root.Boilers.#1 Boiler.FI8110.PV.Value", "Leaf"], ["Root.Boilers.#1 Boiler.PI8110.PV.Value", "Leaf"], ["Root.Boilers.#1 Boiler.TI8110.PV.Value", "Leaf"]]
```

### List Recursive
This function is a custom list function that was designed to simulate branching when the database has been set up as a flat directory where leaves are categorized by points. It returns the next level of “branch” based on the parameters set. You can parse in either ‘*’ or the branch you would like the next level of. Request example:
```
http://172.20.60.43:8003/method=listtree&m=* or
http://172.20.60.43:8003/method=listtree&m=Root.*
```
Response:
```
[["Boilers", "Branch", null], ["Cane Prep & Milling", "Branch", null], ["Cane Receivals", "Branch", null], ["Evaporation", "Branch", null], ["Injection Water", "Branch", null], ["Juice Treatment", "Branch", null], ["Pan Stage", "Branch", null], ["Powerhouse", "Branch", null], ["Sugar & Molasses Handling", "Branch", null], ["Wireless Data", "Branch", null]]
```
### Read
This function mimics OpenOPC’s read function. Parsing in a branch or leaf and it will return a tuple of values.

Request:
```
http://172.20.60.43:8003/method=read&m=Root.Int4
```

### Write
This function mimics OpenOPC's write function.  Parsing in a branch or leaf and a value and returning a boolean success or fail.

Request:
```
http://172.20.60.43:8003/method=write&m=Root.Int4&s=123.0
```

Write requests also accept named request parameters so that we can specify the location (*loc*) of the write and the value (*val*) being written
```
http://172.20.60.43:8003/method=write&loc=Root.Int4&val=123.0
```

### Properties
This mimics the OpenOPC’s properties function. Parsing in a leaf name or multiple leaves will return the result that you would expect when calling it via OpenOPC, unformatted.
```
http://172.20.60.43:8003/method=properties&m=Root.Powerhouse.Common%20Items.Auto%20Export%20Functions.PY7700.SW.Value 
or 
http://172.20.60.43:8003/method=properties&m=Root.Powerhouse.Common%20Items.Auto%20Export%20Functions.PY7700.SW.Value,Root.Powerhouse.Common Items.EI7703.PV.Value
```
### JSON Properties
This is a custom function that takes the results of the OpenOPC’s properties function and formats it to a more common array structure. Parsing in a leaf name or multiple leaves will return an array for each leaf’s properties.
```
http://172.20.60.43:8003/method=jsonproperties&m=Root.Powerhouse.Common%20Items.Auto%20Export%20Functions.PY7700.SW.Value 
or
http://172.20.60.43:8003/method=jsonproperties&m=Root.Powerhouse.Common%20Items.Auto%20Export%20Functions.PY7700.SW.Value,Root.Powerhouse.Common Items.EI7703.PV.Value
```
### Search
This is a search utility that goes through OpenOPC’s list function attempting to locate branches and leaves with the parsed params.
```
http://172.20.60.43:8003/method=search&m=Common
```

# Copyright
Copyright 2016 Headstation. (http://headstation.com) All rights reserved. It is free software and may be redistributed under the terms specified in the LICENSE file (Apache License 2.0). 
