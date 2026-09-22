# Connecting a legacy system

Most of the data worth having is already being collected by something old.
This document is the shortest path from "the site has *that*" to a working
gate, and the things worth knowing before you promise a date.

- [Survey the site first](#survey-the-site-first)
- [What you have → which gate](#what-you-have--which-gate)
- [Local protocol or cloud API?](#local-protocol-or-cloud-api)
- [The playbook](#the-playbook)
- [Working on an OT network](#working-on-an-ot-network)
- [What to write down](#what-to-write-down)

## Survey the site first

Four questions, in this order. They decide the gate, and they are all
questions for people rather than for a scanner.

1. **What already collects this data?** Nearly every building has one system
   that has been logging for years — a BMS, a JACE, a SCADA historian, a
   Zabbix server, a meter portal. Reading from it gives you history on day
   one. Reading from the sensors directly gives you data starting today.
2. **Who owns the credentials, and can they give you a read-only account?**
   This is usually the schedule risk, not the protocol. A BMS integrator can
   take weeks to create a user, and "the person who set it up has left" is a
   normal answer.
3. **Where does the history live, and how far back?** A Niagara station keeps
   years, Zabbix a fortnight, a PLC nothing at all. This determines whether
   you configure a backfill or accept that the series starts now.
4. **What is the network path?** A gate runs in a container on your side; the
   upstream is on a building network, behind a firewall, possibly on a private
   APN. BACnet additionally needs broadcasts, which containers do not get by
   default.

## What you have → which gate

| The site has | Gate | Notes |
|---|---|---|
| Tridium Niagara AX/N4 (a JACE) | [`obix`](gates.md#obix) | The best case: one credential, hundreds of points, years of history, and the station already speaks the field buses below. Enable HTTP Basic for the oBIX user explicitly. |
| Siemens Desigo, Johnson Controls Metasys, Honeywell, Trend, Delta, Schneider | [`bacnet`](gates.md#bacnet) | Ask for the points list as an EDE file; it gives you object types and instances. Needs host or macvlan networking. |
| Siemens S7-300/400/1200/1500 | [`s7`](gates.md#s7) | Needs non-optimised data blocks and PUT/GET enabled — changes to the PLC program, so the automation contractor must be involved. |
| Wago, Beckhoff, Unitronics, generic PLC | [`modbus`](gates.md#modbus) or [`opcua`](gates.md#opcua) | Modbus if there is a register map; OPC UA if the firmware has a server (usually anything post-2015). |
| Rockwell / Allen-Bradley, Mitsubishi, Omron | [`opcua`](gates.md#opcua) | Through the OPC server the site already runs (KEPServerEX, Matrikon). Ask whether historical access is licensed. |
| WinCC, Ignition, zenon, any SCADA | [`opcua`](gates.md#opcua) with `history: true`, or [`sql`](gates.md#sql) | The historian usually has both an OPC UA HA interface and a SQL database. SQL is faster for a large backfill. |
| Wonderware / AVEVA Historian, OSIsoft PI, GE Proficy | [`sql`](gates.md#sql) | Via their SQL or ODBC interface; write the query against their example views. Ask for a read-only login and filter on time. |
| Kamstrup, Landis+Gyr, Itron, Elster, Diehl meters | [`modbus`](gates.md#modbus), or the utility's portal | A meter with a Modbus module is direct; otherwise the data comes from the utility, not the meter. |
| M-Bus / wM-Bus concentrator | [`modbus`](gates.md#modbus) or [`csv_drop`](gates.md#csv_drop) | Concentrators expose readings as Modbus registers or write nightly files. Both are normal. |
| DLMS/COSEM head-end system | [`remote_drop`](gates.md#remote_drop), [`xml_drop`](gates.md#xml_drop), [`sql`](gates.md#sql) | You will not speak DLMS to the meter; you will read the head-end's exports or its database. |
| A utility or district-heating customer portal | [`http_csv`](gates.md#http_csv) or [`http_xml`](gates.md#http_xml) | The CSV export link is an API. Check whether they offer a data-exchange web service first; many do and do not advertise it. |
| An ERP or billing system (SAP, Navision, in-house) | [`http_xml`](gates.md#http_xml) (SOAP), [`sql`](gates.md#sql), [`remote_drop`](gates.md#remote_drop) | Whichever their integration team already supports. Do not ask them to build something new. |
| A spreadsheet somebody emails every month | [`excel_drop`](gates.md#excel_drop) | Agree on a stable layout and a drop location (SFTP or a shared folder). Read it as it is rather than asking for CSV: the conversion step is where the dates and decimal commas get lost. |
| An FTP server with nightly exports | [`remote_drop`](gates.md#remote_drop) | Still the most common partner interface there is. |
| Zabbix, and by extension the UPS/CRAC/probes it watches | [`zabbix`](gates.md#zabbix) | Free history for anything already monitored. Mind the housekeeping horizon. |
| UPS, rack PDU, generator, gateway with an agent | [`snmp`](gates.md#snmp) | Get the vendor MIB, read the DISPLAY-HINT for the scale factor, use numeric OIDs. |
| Solar inverters (SMA, Fronius, Huawei, Solinteg, Growatt) | [`modbus`](gates.md#modbus) locally, [`http_json`](gates.md#http_json) for the cloud | Local Modbus is real-time and free; the cloud API is easier and rate limited. |
| SolarEdge, Enphase, any PV monitoring portal | [`http_json`](gates.md#http_json) | Watch the daily request quota: set `min_interval_s` and a coarse schedule. |
| EV chargers via a CSMS (OCPP) | [`http_json`](gates.md#http_json) or [`mqtt`](gates.md#mqtt) | Talk to the CSMS, never to OCPP directly; that protocol is a control channel. |
| LoRaWAN (ChirpStack, The Things Stack), any MQTT feed | [`mqtt`](gates.md#mqtt) | Dotted field paths reach into the network server's nested payload. |
| ThingsBoard | [`thingsboard`](gates.md#thingsboard) | |
| A FIWARE deployment from 2016 (Orion v2, STH, QuantumLeap) | [`ngsi_v2`](gates.md#ngsi_v2) | Run both and mirror one; that is the cheapest migration path there is. |
| Another NGSI-LD broker (a city platform, a partner pilot) | [`ngsi_ld`](gates.md#ngsi_ld) | Federation, not migration. |
| An InfluxDB 1.8 behind somebody's Grafana | [`influx_source`](gates.md#influx_source) | |
| Prometheus | [`prometheus`](gates.md#prometheus) | Gives its short-retention series a long-retention home. |
| A REST API nobody has wrapped | [`http_json`](gates.md#http_json) | Try this before writing a gate type. |
| None of the above | [adding-a-gate.md](adding-a-gate.md) | Two methods, and the framework does the rest. |

## Local protocol or cloud API?

When a device offers both — most inverters, many meters, every modern
controller — the choice is not obvious:

|  | Local (Modbus, BACnet, S7, OPC UA) | Cloud API (`http_json`) |
|---|---|---|
| Resolution | whatever you poll | whatever they aggregate to, often 15 min |
| History | none: the series starts when you start | usually months or years, so a backfill works |
| Availability | fails when the network path fails | fails when their service fails, and you cannot fix it |
| Cost | none | rate limits, sometimes a subscription |
| Access | needs a route into the building network | needs an account and outbound HTTPS |

A common and good answer is **both**: the local protocol for live data at the
resolution you want, the cloud API once a night to backfill and to correct
what you missed. Two gates, two keys, the same device model — and the
watermark logic keeps the overlap idempotent.

## The playbook

1. **One device, read-only, by hand first.** `curl` the endpoint, or read one
   register with a Modbus client, from the machine the worker will run on.
   Half of all integration problems are network or credential problems and
   this finds them in five minutes.
2. **Write the gate entry with one device and one property**, `enabled: true`,
   and run `<key>_init`. Look at the Device and summary entities in the broker
   before ingesting anything.
3. **Run `<key>_run` once.** Check that the values are the right magnitude —
   this is where a missing `scale`, a wrong `word_order` or a timezone shows
   up. A temperature of 214.0 or 1.9e-41 is a decoding problem, not a sensor
   problem.
4. **Then add the rest of the properties and devices**, and only then think
   about the backfill. A backfill over a wrong mapping writes a year of wrong
   data that somebody has to delete.
5. **Run the backfill with `dry_run: true` first**; it reports what it would
   fetch per window without writing.
6. **Verify**: `python scripts/verify_platform.py --gate <key>` checks the
   model, the InfluxDB bridge and freshness the way a consumer would.

## Working on an OT network

Gates only read. Nothing in this platform writes to an upstream system, and
that is worth stating in writing to whoever owns the plant, because it is the
first question they will ask.

Beyond that, the usual rules of somebody else's control network apply, and
they are the site's rules, not yours:

- **Read-only accounts, one per gate**, so that a leaked credential cannot
  change a setpoint and so the plant's logs show which system read what.
- **Poll gently.** A PLC serving three connections can be locked out by an
  eager gate; an S7 CPU busy answering a poll every ten seconds is a CPU not
  doing its job. Use the slowest schedule the data allows and set
  `min_interval_s`.
- **Never bridge networks to make a gate work.** If the worker cannot reach
  the plant network, the answer is a route, a firewall rule for one address
  and port, or a data diode — not a second interface on the container host
  because it was quicker.
- **Expect no encryption and no authentication.** Modbus, BACnet and S7 have
  effectively none; the protection is the network. Plain FTP sends the
  password in clear text. Say so in the handover rather than quietly
  accepting it.
- **Agree on what happens when the upstream is upgraded.** Field mappings are
  the fragile part: a renamed BACnet object or a shifted data-block offset
  produces plausible numbers. Freshness alerts catch the silence; they do not
  catch a plausible wrong value.

## What to write down

For every gate you configure, in the gate's docstring if you wrote one, or in
your own handover if you did not:

- the credential, where it came from and who can reissue it;
- the network path and the firewall rule that makes it work;
- the register map, points list or item ids, and where the authoritative copy
  of that document lives;
- **every place the upstream does not behave as documented.** These are the
  most valuable sentences in the repository. The reason the built-in gates are
  short is that they have those sentences instead of defensive code: the
  archive that serves no visibility, the platform that answers 400 instead of
  204, the meter that reverses its words, the history query that silently
  caps at 1000 rows.
