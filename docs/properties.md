# Properties and units

The vocabulary every gate draws from: what each `controlledProperty` means,
what unit it carries, and how a consumer may aggregate it. The registry is
`plugins/datagates/core/vocab.py`; this page documents it, and
`tests/test_vocab.py` fails if a built-in gate emits a property that is not in
it, or a unit code that does not exist.

Why it exists: without it, the same quantity arrives under two names. This
repository had `energy` and `energyConsumption`, `humidity` and
`relativeHumidity`, and a `gasIndex` that meant the same kind of thing as
`energy` — plus three separate copies of "which properties are running
totals", each with a comment promising to stay in step with the others. A
consumer cannot resolve that by guessing.

- [How to read the table](#how-to-read-the-table)
- [The properties](#the-properties)
- [Unit codes](#unit-codes)
- [Aggregating a series](#aggregating-a-series)
- [SAREF and QUDT](#saref-and-qudt)
- [Adding or renaming a property](#adding-or-renaming-a-property)
- [Names no longer in use](#names-no-longer-in-use)

## How to read the table

**Kind** is how the quantity behaves over time, and it decides everything else:

| Kind | Meaning | Example |
|---|---|---|
| **instant** | true at its timestamp, and only then | `temperature`, `power` |
| **delta** | a total for the period *starting* at its timestamp | `energyConsumption`, `precipitation` |
| **register** | a counter that only goes up, guarded at ingestion by `core/counter_guard.py` | `energy`, `gasVolume` |
| **state** | a flag or a code; not a measurement | `occupancy`, `alarmState` |

The pairs are deliberate and not interchangeable: `energy` is what the meter's
display shows, `energyConsumption` is what was used during an hour. A gate
reading a meter register declares `cumulative: true` and uses the register
name; a gate reading an already-differenced export uses the consumption name.

**Downsample with** is the only aggregation that does not invent a number —
see [below](#aggregating-a-series).

## The properties

### Thermal and air

| Property | Unit | Kind | Downsample with | Meaning |
|---|---|---|---|---|
| `temperature` | `CEL` degree Celsius | instant | `mean` | dry-bulb air or medium temperature. |
| `returnTemperature` | `CEL` degree Celsius | instant | `mean` | temperature of the returning medium in a circuit. SAREF has one Temperature class; the return role is lost in RDF |
| `feelsLikeTemperature` | `CEL` degree Celsius | instant | `mean` | apparent temperature, as published by a weather service. derived, not measured |
| `temperatureSetpoint` | `CEL` degree Celsius | instant | `last` | the temperature a controller is asked to hold. a setpoint is a command, not an observation |
| `relativeHumidity` | `P1` percent | instant | `mean` | relative humidity as a fraction from 0 to 1. stored as a fraction, not a percentage |
| `co2` | `59` part per million | instant | `mean` | carbon dioxide concentration in ppm. no SAREF core term; SAREF4ENVI is the place to look |
| `atmosphericPressure` | `A97` hectopascal | instant | `mean` | barometric pressure in hPa. |

### Electricity

| Property | Unit | Kind | Downsample with | Meaning |
|---|---|---|---|---|
| `power` | `KWT` kilowatt | instant | `mean` | active power at the moment of reading. some devices report W (`WTT`): always read the summary's `unitCode` |
| `energy` | `KWH` kilowatt hour | register | `difference` | meter register: energy accumulated since the meter began. difference consecutive readings; never sum them |
| `energyConsumption` | `KWH` kilowatt hour | delta | `sum` | energy consumed during the period starting at the timestamp. the delta form of `energy`; not interchangeable with it |
| `voltage` | `VLT` volt | instant | `mean` | voltage at the point of measurement. SAREF core has no Voltage property class |
| `current` | `AMP` ampere | instant | `mean` | current at the point of measurement. SAREF core has no Current property class |
| `electricityLoad` | `MAW` megawatt | instant | `mean` | electrical load of a market area or grid zone. CIM/IEC 61970 is the fitting model, not SAREF |
| `electricityGeneration` | `MAW` megawatt | instant | `mean` | generation of a market area, by production type. as above |
| `batteryLevel` | `P1` percent | instant | `last` | remaining charge as a fraction from 0 to 1. device health rather than a measurement of the world |

### Volumes, flows and heat

| Property | Unit | Kind | Downsample with | Meaning |
|---|---|---|---|---|
| `flow` | `MQH` cubic metre per hour | instant | `mean` | volumetric flow rate. SAREF core has no flow property; SAREF4WATR is where to look |
| `waterConsumption` | `MTQ` cubic metre | delta | `sum` | water consumed during the period starting at the timestamp. SAREF4WATR territory |
| `waterVolume` | `MTQ` cubic metre | register | `difference` | water meter register: volume since the meter began. |
| `gasConsumption` | `MTQ` cubic metre | delta | `sum` | gas consumed during the period starting at the timestamp. no SAREF core term for a gas volume |
| `gasVolume` | `MTQ` cubic metre | register | `difference` | gas meter register: volume since the meter began. replaces the older name `gasIndex` |
| `heatConsumption` | `KWH` kilowatt hour | delta | `sum` | heat consumed during the period starting at the timestamp. `saref:Energy` fits the quantity; the medium is lost |
| `volume` | `MTQ` cubic metre | register | `difference` | generic volume register, when the medium is not modelled. prefer `waterVolume` or `gasVolume` |
| `workingHours` | `HUR` hour | register | `difference` | run-hour counter of a machine. equipment health; SAREF4BLDG describes the equipment, not its hour meter |

### Weather

| Property | Unit | Kind | Downsample with | Meaning |
|---|---|---|---|---|
| `windSpeed` | `MTS` metre per second | instant | `mean` | wind speed in m/s. SAREF core covers building services, not meteorology |
| `windDirection` | `DD` degree (angle) | instant | `none` | wind direction in degrees. a circular quantity: the mean of 350 and 10 is 180, due south |
| `gustSpeed` | `MTS` metre per second | instant | `last` | maximum gust speed reported for the period. already an extreme: averaging gusts loses the point of them |
| `precipitation` | `MMT` millimetre | delta | `sum` | precipitation over the period starting at the timestamp. |
| `precipitationProbability` | `C62` one | instant | `mean` | forecast probability, 0 to 1. a forecast about the world, not an observation of it |
| `visibility` | `MTR` metre | instant | `mean` | horizontal visibility in metres. the Open-Meteo archive does not serve it at all |
| `uVIndexMax` | `C62` one | instant | `last` | maximum UV index forecast for the period. spelling follows the Smart Data Models' WeatherObserved |

### States and quality

| Property | Unit | Kind | Downsample with | Meaning |
|---|---|---|---|---|
| `occupancy` | `C62` one | state | `last` | 1 when a space is occupied, 0 when it is not. |
| `alarmState` | `C62` one | state | `last` | 1 while an alarm is active, 0 otherwise. never average it; a mean of 0.3 alarms means nothing |
| `pumpState` | `C62` one | state | `last` | 1 while a pump runs, 0 otherwise. SAREF4BLDG models the pump as equipment, not its running state |
| `readingQuality` | `C62` one | state | `last` | the upstream's own quality code. codes differ per upstream; document yours in the gate's docstring |

### Markets

| Property | Unit | Kind | Downsample with | Meaning |
|---|---|---|---|---|
| `dayAheadPrice` | `EUR_MWH` euro per megawatt hour | instant | `mean` | day-ahead market price for the delivery period. stamped at the start of its delivery period |

## Unit codes

UN/CEFACT Recommendation 20, because the Smart Data Models use them and
`unitCode` on every summary entity carries one. `EUR_MWH` is not a UN/CEFACT
code: none exists for currency per energy.

Two gates may legitimately report one property in different units — `power` in
`KWT` from a ThingsBoard meter and in `WTT` from an SNMP PDU. **Always read
`unitCode` from the summary entity**; never assume the registry's default.

| Code | Unit | Code | Unit |
|---|---|---|---|
| `CEL` | degree Celsius | `MTQ` | cubic metre |
| `KWH` | kilowatt hour | `MQH` | cubic metre per hour |
| `KWT` | kilowatt | `LTR` | litre |
| `WTT` | watt | `MTR` | metre |
| `MAW` | megawatt | `MTS` | metre per second |
| `VLT` | volt | `DD` | degree (angle) |
| `AMP` | ampere | `MMT` | millimetre |
| `A97` | hectopascal | `2N` | decibel |
| `59` | part per million | `HUR` | hour |
| `P1` | percent — **stored as a fraction, 0 to 1** | `C62` | one, dimensionless |

## Aggregating a series

The rule is in the table, and getting it wrong is silent:

```flux
// instant (temperature, power): mean
|> aggregateWindow(every: 1h, fn: mean, createEmpty: false)

// delta (energyConsumption, precipitation): sum
|> aggregateWindow(every: 1d, fn: sum, createEmpty: false)

// register (energy, gasVolume): difference — never sum
|> aggregateWindow(every: 1d, fn: last, createEmpty: false)
|> difference(nonNegative: true)

// state (occupancy, alarmState): last, or do not aggregate at all
|> aggregateWindow(every: 1h, fn: last, createEmpty: false)
```

Two cases worth naming. Summing a register produces a number that grows with
the number of readings and means nothing — the most common mistake made with
meter data. And `windDirection` is circular: the mean of 350° and 10° is 180°,
due south, when the answer is due north. Its rule is `none`.

`core.vocab.aggregation_of(name)` returns the rule, and `last` for anything
unregistered, because `last` cannot invent a value that was never read.

## SAREF and QUDT

Each property carries a mapping towards [SAREF](https://saref.etsi.org) and
each unit towards [QUDT](https://qudt.org), for projecting the model to RDF.
Two things to know before relying on them.

**They are a projection, never a store.** Nothing here changes what Orion-LD
holds. Attaching a SAREF or Smart-Data-Model `@context` to stored entities
makes the broker expand attribute names, and every consumer's `q=` filter stops
matching — the decision, and the scar, are in `ROADMAP.md` §3. Apply the
context on export.

**Most mappings are unverified.** 7 of 35 properties have a mapping checked
against SAREF core; the other 28 are `candidate` (the term this project
believes fits) or `none` (no standard term, with a note saying where to look —
usually SAREF4ENVI, SAREF4WATR or CIM). `core.vocab.unmapped()` lists them, and
every QUDT IRI is a candidate. **Resolve them against the published ontologies
before publishing an export**: a wrong IRI is worse than a missing one, because
it validates.

What SAREF core genuinely covers here is the building-services middle —
temperature, humidity, pressure, power, energy, occupancy. Weather, market
prices, grid load, water and gas volumes and device health are not in it, and
inventing IRIs for them would be a disservice to whoever loads the result.

Time series stay in InfluxDB. An RDF export should carry the model — devices,
buildings, properties, units, relationships — and the `influxMeasurement` name
as a pointer to the history, exactly as a consumer uses it today. Emitting
every observation as triples is how semantic building projects become unusably
slow.

## Adding or renaming a property

To add one: an entry in `PROPERTIES` in `core/vocab.py` with its unit, kind,
aggregation and meaning; a note if it has no standard mapping; a row in the
table above. `tests/test_vocab.py` checks the rest.

**Renaming is not cosmetic.** A summary entity's id is
`uuid5(device urn + property name)`, so a rename mints a new id, a new InfluxDB
measurement and an orphaned old series. Nothing migrates it. Choose the name
you mean to keep; if you must rename, decide first what happens to the old
series.

A custom gate may emit a property that is not registered — the platform will
not stop you, and an upstream nobody anticipated is exactly why `property:` is
free text. Register it when you contribute the gate.

## Names no longer in use

Corrections to this repository's own examples. No deployment owes a migration:
these names only ever appeared in example configuration. They stay in
`DEPRECATED`, and in the counter guard's set, so that a deployment carrying
them from before the rename keeps its protection.

| Was | Use | Why |
|---|---|---|
| `humidity` | `relativeHumidity` | two names for one quantity |
| `gasIndex` | `gasVolume` | "index" is meter-reading jargon; matches `waterVolume` |
| `waterIndex` | `waterVolume` | as above |
| `energyTotal` | `energy` | `energy` is already the register |
