"""
datagates.core.vocab — the controlledProperty vocabulary, and what it means.

`property:` in gates.yaml is free text, which is what makes a new gate cheap.
It is also how a platform ends up storing `energy`, `energyConsumption`,
`humidity` and `relativeHumidity` and leaving consumers to guess. This module
is the registry: one entry per property a built-in gate may emit, with its
unit, what kind of quantity it is, how it may be aggregated, and how it maps
to SAREF and QUDT for anyone exporting the model as RDF.

Three things it is for, in order of how much they are worth:

1. **Consumers.** "Is it `energy` or `energyConsumption`?" has an answer, and
   the answer says which is a meter register and which is a period total.
2. **Aggregation.** A dashboard downsampling a series has to know whether to
   take a mean, a sum, a difference or nothing at all. Averaging a meter
   register is meaningless; summing an instantaneous power reading is worse.
   The same table is what a downsampling task (ROADMAP M9) needs.
3. **Semantic export.** SAREF and QUDT IRIs, so the model can be projected to
   RDF without inventing the mapping at export time. Deliberately *only* a
   mapping: nothing here changes what Orion-LD stores, because adding a
   SAREF or Smart-Data-Model @context to stored entities expands attribute
   names and breaks every consumer's `q=` filter (see ROADMAP.md §3).

**The IRIs are not all verified.** Entries marked `candidate` are the mapping
this project believes is right but has not checked against the published
ontologies; they must be resolved against https://saref.etsi.org and
https://qudt.org before an RDF export is published to anyone. A wrong IRI is
worse than no IRI, because it type-checks.

Renaming a property is not a cosmetic change: the summary entity's id is
uuid5(device urn + property name), so a rename mints a new id, a new InfluxDB
measurement and an orphaned old series. Register the name you mean to keep.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── how a quantity behaves over time ─────────────────────────────────
INSTANT = "instantaneous"   # a value that is true at its timestamp
DELTA = "delta"             # a total for the period starting at its timestamp
CUMULATIVE = "cumulative"   # a running register that only goes up
STATE = "state"             # a code or a flag; not a measurement

# ── how it may be aggregated when downsampling ───────────────────────
MEAN = "mean"               # instantaneous quantities
SUM = "sum"                 # deltas
DIFFERENCE = "difference"   # cumulative registers: last minus first
LAST = "last"               # states, and anything else that cannot be combined
NONE = "none"               # aggregating it is always wrong

CONFIRMED, CANDIDATE, UNMAPPED = "confirmed", "candidate", "none"

SAREF = "https://saref.etsi.org/core/"
QUDT = "http://qudt.org/vocab/unit/"


@dataclass(frozen=True)
class Property:
    """One controlledProperty, as consumers and exporters see it."""
    name: str
    unit: str                    # the UN/CEFACT code gates should use
    kind: str                    # INSTANT | DELTA | CUMULATIVE | STATE
    aggregate: str               # MEAN | SUM | DIFFERENCE | LAST | NONE
    meaning: str
    saref: str | None = None
    status: str = UNMAPPED       # CONFIRMED | CANDIDATE | UNMAPPED
    note: str = ""


def _p(name, unit, kind, aggregate, meaning, saref=None, status=UNMAPPED, note=""):
    return Property(name, unit, kind, aggregate, meaning, saref, status, note)


# ── the registry ─────────────────────────────────────────────────────
# Names follow the FIWARE Smart Data Models' controlledProperty vocabulary
# where it has a term, because this platform follows those models elsewhere;
# extensions are marked in the note and exist because no standard term fitted.
PROPERTIES: dict[str, Property] = {p.name: p for p in [
    # -- thermal and air ----------------------------------------------
    _p("temperature", "CEL", INSTANT, MEAN, "dry-bulb air or medium temperature",
       SAREF + "Temperature", CONFIRMED),
    _p("returnTemperature", "CEL", INSTANT, MEAN, "temperature of the returning medium in a circuit",
       SAREF + "Temperature", CANDIDATE, "SAREF has one Temperature class; the return role is lost in RDF"),
    _p("feelsLikeTemperature", "CEL", INSTANT, MEAN, "apparent temperature, as published by a weather service",
       SAREF + "Temperature", CANDIDATE, "derived, not measured"),
    _p("temperatureSetpoint", "CEL", INSTANT, LAST, "the temperature a controller is asked to hold",
       SAREF + "Temperature", CANDIDATE, "a setpoint is a command, not an observation; SAREF models it "
                                         "through saref:Command, which this mapping does not express"),
    _p("relativeHumidity", "P1", INSTANT, MEAN, "relative humidity as a fraction from 0 to 1",
       SAREF + "Humidity", CONFIRMED, "stored as a fraction, not a percentage (docs/data-model.md)"),
    _p("co2", "59", INSTANT, MEAN, "carbon dioxide concentration in ppm",
       None, UNMAPPED, "no SAREF core term; SAREF4ENVI is the place to look before exporting"),
    _p("atmosphericPressure", "A97", INSTANT, MEAN, "barometric pressure in hPa",
       SAREF + "Pressure", CONFIRMED),

    # -- electricity --------------------------------------------------
    _p("power", "KWT", INSTANT, MEAN, "active power at the moment of reading",
       SAREF + "Power", CONFIRMED, "some devices report W (WTT): always read the summary's unitCode"),
    _p("energy", "KWH", CUMULATIVE, DIFFERENCE, "meter register: energy accumulated since the meter began",
       SAREF + "Energy", CONFIRMED, "difference consecutive readings; never sum them"),
    _p("energyConsumption", "KWH", DELTA, SUM, "energy consumed during the period starting at the timestamp",
       SAREF + "Energy", CONFIRMED, "the delta form of `energy`; these two are not interchangeable"),
    _p("voltage", "VLT", INSTANT, MEAN, "voltage at the point of measurement",
       None, CANDIDATE, "SAREF core has no Voltage property class"),
    _p("current", "AMP", INSTANT, MEAN, "current at the point of measurement",
       None, CANDIDATE, "SAREF core has no Current property class"),
    _p("electricityLoad", "MAW", INSTANT, MEAN, "electrical load of a market area or grid zone",
       None, UNMAPPED, "grid-level quantity; CIM/IEC 61970 is the fitting model, not SAREF"),
    _p("electricityGeneration", "MAW", INSTANT, MEAN, "generation of a market area, by production type",
       None, UNMAPPED, "as above"),
    _p("batteryLevel", "P1", INSTANT, LAST, "remaining charge as a fraction from 0 to 1",
       None, CANDIDATE, "device health rather than a measurement of the world"),

    # -- volumes and flows --------------------------------------------
    _p("flow", "MQH", INSTANT, MEAN, "volumetric flow rate",
       None, UNMAPPED, "SAREF core has no flow property; SAREF4WATR is where to look"),
    _p("waterConsumption", "MTQ", DELTA, SUM, "water consumed during the period starting at the timestamp",
       None, UNMAPPED, "SAREF4WATR territory; SAREF core stops at Energy and Power"),
    _p("waterVolume", "MTQ", CUMULATIVE, DIFFERENCE, "water meter register: volume since the meter began",
       None, UNMAPPED, "as waterConsumption; the register form"),
    _p("gasConsumption", "MTQ", DELTA, SUM, "gas consumed during the period starting at the timestamp",
       None, UNMAPPED, "no SAREF core term for a gas volume"),
    _p("gasVolume", "MTQ", CUMULATIVE, DIFFERENCE, "gas meter register: volume since the meter began",
       None, UNMAPPED, "as gasConsumption; the register form. Replaces the older name gasIndex"),
    _p("heatConsumption", "KWH", DELTA, SUM, "heat consumed during the period starting at the timestamp",
       None, CANDIDATE, "saref:Energy fits the quantity; the medium is lost"),
    _p("volume", "MTQ", CUMULATIVE, DIFFERENCE, "generic volume register, when the medium is not modelled",
       None, UNMAPPED, "prefer waterVolume or gasVolume; this exists because production data uses it"),
    _p("workingHours", "HUR", CUMULATIVE, DIFFERENCE, "run-hour counter of a machine",
       None, UNMAPPED, "equipment health; SAREF4BLDG describes the equipment, not its hour meter"),

    # -- weather ------------------------------------------------------
    _p("windSpeed", "MTS", INSTANT, MEAN, "wind speed in m/s",
       None, UNMAPPED, "weather quantity; SAREF core covers building services, not meteorology"),
    _p("windDirection", "DD", INSTANT, NONE, "wind direction in degrees",
       None, UNMAPPED, "a circular quantity: averaging 350 and 10 gives 180, which is due south"),
    _p("gustSpeed", "MTS", INSTANT, LAST, "maximum gust speed reported for the period",
       None, UNMAPPED, "already an extreme over a period: averaging gusts loses the point of them"),
    _p("precipitation", "MMT", DELTA, SUM, "precipitation over the period starting at the timestamp",
       None, UNMAPPED, "weather quantity, as windSpeed"),
    _p("precipitationProbability", "C62", INSTANT, MEAN, "forecast probability, 0 to 1",
       None, UNMAPPED, "a forecast about the world, not an observation of it"),
    _p("visibility", "MTR", INSTANT, MEAN, "horizontal visibility in metres",
       None, UNMAPPED, "weather quantity; the Open-Meteo archive does not serve it at all"),
    _p("uVIndexMax", "C62", INSTANT, LAST, "maximum UV index forecast for the period",
       None, UNMAPPED, "spelling follows the Smart Data Models' WeatherObserved"),

    # -- states and quality -------------------------------------------
    _p("occupancy", "C62", STATE, LAST, "1 when a space is occupied, 0 when it is not",
       SAREF + "Occupancy", CONFIRMED),
    _p("alarmState", "C62", STATE, LAST, "1 while an alarm is active, 0 otherwise",
       None, UNMAPPED, "never average it; a mean of 0.3 alarms means nothing"),
    _p("pumpState", "C62", STATE, LAST, "1 while a pump runs, 0 otherwise",
       None, UNMAPPED, "SAREF4BLDG models the pump as equipment, not its running state"),
    _p("readingQuality", "C62", STATE, LAST, "the upstream's own quality code for a reading",
       None, UNMAPPED, "codes differ per upstream; document yours in the gate's docstring"),

    # -- markets ------------------------------------------------------
    _p("dayAheadPrice", "EUR_MWH", INSTANT, MEAN, "day-ahead market price for the delivery period",
       None, UNMAPPED, "a price, not a physical quantity; stamped at the start of its delivery period"),
]}

# ── unit codes ───────────────────────────────────────────────────────
# UN/CEFACT Recommendation 20 codes, with the QUDT unit each corresponds to.
# Every QUDT IRI here is a candidate until checked against qudt.org.
UNITS: dict[str, tuple[str, str | None]] = {
    "CEL": ("degree Celsius", QUDT + "DEG_C"),
    "KWH": ("kilowatt hour", QUDT + "KiloW-HR"),
    "KWT": ("kilowatt", QUDT + "KiloW"),
    "WTT": ("watt", QUDT + "W"),
    "MAW": ("megawatt", QUDT + "MegaW"),
    "VLT": ("volt", QUDT + "V"),
    "AMP": ("ampere", QUDT + "A"),
    "MTQ": ("cubic metre", QUDT + "M3"),
    "MQH": ("cubic metre per hour", QUDT + "M3-PER-HR"),
    "LTR": ("litre", QUDT + "L"),
    "P1": ("percent, stored here as a fraction 0 to 1", QUDT + "UNITLESS"),
    "59": ("part per million", QUDT + "PPM"),
    "A97": ("hectopascal", QUDT + "HectoPA"),
    "MTR": ("metre", QUDT + "M"),
    "MTS": ("metre per second", QUDT + "M-PER-SEC"),
    "DD": ("degree (angle)", QUDT + "DEG"),
    "MMT": ("millimetre", QUDT + "MilliM"),
    "2N": ("decibel", QUDT + "DeciB"),
    "HUR": ("hour", QUDT + "HR"),
    "C62": ("one, dimensionless", QUDT + "UNITLESS"),
    "EUR_MWH": ("euro per megawatt hour, not a UN/CEFACT code", None),
}

# ── names that were used and should not be again ─────────────────────
# A rename mints a new summary id and a new InfluxDB measurement, so these are
# corrections to this repository's own examples, not migrations anyone owes.
DEPRECATED: dict[str, str] = {
    "humidity": "relativeHumidity",     # two names for one quantity
    "gasIndex": "gasVolume",            # "index" is meter-reading jargon; Volume matches waterVolume
    "energyTotal": "energy",            # `energy` is already the register
    "waterIndex": "waterVolume",        # as gasIndex
}


# What the counter guard protects. Derived, so that registering a cumulative
# property is the only step needed -- core/counter_guard.py imports this
# instead of keeping its own copy, which drifted from the client's copy and
# from docs/data-model.md, each of them promising in a comment to stay in step.
#
# Deprecated names are included on purpose: a deployment carrying `gasIndex`
# from before the rename must keep its guard, and losing protection quietly is
# worse than a stale name.
CUMULATIVE_PROPERTIES: frozenset[str] = frozenset(
    {name for name, prop in PROPERTIES.items() if prop.kind == CUMULATIVE}
    | {old for old, new in DEPRECATED.items()
       if (p := PROPERTIES.get(new)) is not None and p.kind == CUMULATIVE})


def lookup(name: str) -> Property | None:
    return PROPERTIES.get(name)


def is_registered(name: str) -> bool:
    return name in PROPERTIES


def canonical(name: str) -> str:
    """The name to use instead, if this one is deprecated."""
    return DEPRECATED.get(name, name)


def aggregation_of(name: str) -> str:
    """How a consumer may downsample this property. LAST when unknown, because
    it is the only answer that cannot silently invent a value."""
    prop = PROPERTIES.get(name)
    return prop.aggregate if prop else LAST


def unit_name(code: str) -> str:
    return UNITS.get(code, (code, None))[0]


def unmapped() -> list[str]:
    """Properties with no confirmed semantic mapping — the work an RDF export
    has to finish before it is published."""
    return sorted(n for n, p in PROPERTIES.items() if p.status != CONFIRMED)
