#!/usr/bin/env python3
"""
Fertigation test suite for the OpenSprinkler fork.

Runs against the DEMO build (-DDEMO, no GPIO), so it is safe to run anywhere
and never touches a valve. Starts its own firmware instance on a scratch data
directory and tears it down afterwards.

Build the demo binary first. Note the explicit HTTP_PORT: the DEMO target
otherwise hard-codes port 80, which will not bind as an unprivileged user.

    ws=$(ls external/TinyWebsockets/tiny_websockets_lib/src/*.cpp)
    otf=$(ls external/OpenThings-Framework-Firmware-Library/*.cpp)
    sens=$(ls sensors/*.cpp)
    g++ -o OpenSprinkler_demo -DDEMO -DHTTP_PORT=8181 -DSMTP_OPENSSL \
        -std=c++14 -include string.h -include cstdint \
        main.cpp OpenSprinkler.cpp program.cpp opensprinkler_server.cpp \
        utils.cpp weather.cpp gpio.cpp mqtt.cpp notifier.cpp smtp.c \
        RCSwitch.cpp ads1115.cpp $sens \
        -Iexternal/TinyWebsockets/tiny_websockets_lib/include $ws \
        -Iexternal/OpenThings-Framework-Firmware-Library/ $otf \
        -lpthread -lmosquitto -lssl -lcrypto

Usage:  python3 test_fertigation.py ./OpenSprinkler_demo

Two authentication assertions are skipped against a DEMO build, which returns
true unconditionally from process_password(). They were verified manually
against an OSPI build.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PW = "a6d82bced638de3def1e9bbb4983225c"  # md5("opendoor"), the factory default
PORT = 8181  # DEMO build forces the port at compile time via -DHTTP_PORT
BASE = f"http://127.0.0.1:{PORT}"

# OpenSprinkler API result codes
OK, UNAUTHORIZED, MISMATCH, DATA_MISSING, OUT_OF_BOUND = 1, 2, 3, 16, 17

_failures = []
_passed = 0
_skipped = []
AUTH_ENFORCED = False  # set at startup; the DEMO build hard-bypasses auth


def api(path, **params):
    params.setdefault("pw", PW)
    q = "&".join(f"{k}={v}" for k, v in params.items())
    with urllib.request.urlopen(f"{BASE}/{path}?{q}", timeout=10) as r:
        return json.loads(r.read().decode())


def api_noauth(path):
    with urllib.request.urlopen(f"{BASE}/{path}", timeout=10) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))


def skip(name, reason):
    _skipped.append(name)
    print(f"  SKIP  {name}\n          {reason}")


def nstations():
    return api("jc")["nbrd"] * 8


def clear_programs():
    api("dp", pid=-1)


# ---------------------------------------------------------------- config API

def test_fert_station_config():
    print("\n[/jf, /cf] fertigation station configuration")

    api("cf", fs=255)
    check("defaults to 255 (unconfigured)", api("jf")["fert_station"] == 255)

    r = api("cf", fs=3)
    check("accepts a valid station id", r["result"] == OK, f"got {r}")
    check("persists and reads back", api("jf")["fert_station"] == 3,
          f'got {api("jf")}')

    r = api("cf", fs=200)
    check("rejects out-of-range station id", r["result"] == OUT_OF_BOUND,
          f"expected {OUT_OF_BOUND}, got {r}")
    check("rejected write did not clobber the stored value",
          api("jf")["fert_station"] == 3)

    # Below MAX_NUM_STATIONS but beyond the boards actually attached. This used
    # to be accepted, leaving the fertigation valve pointed at a station that
    # does not exist and silently never firing.
    absent = nstations() + 4
    r = api("cf", fs=absent)
    check(f"rejects station {absent}, which this controller does not have",
          r["result"] == OUT_OF_BOUND, f"expected {OUT_OF_BOUND}, got {r}")
    check("rejected absent-station write did not clobber the stored value",
          api("jf")["fert_station"] == 3)

    r = api("cf", fs=255)
    check("accepts 255 to unconfigure", r["result"] == OK, f"got {r}")
    check("unconfigure persists", api("jf")["fert_station"] == 255)

    reason = ("the DEMO build returns true unconditionally from "
              "process_password(); run against an OSPI build to assert this")
    if not AUTH_ENFORCED:
        skip("/jf rejects a request with no password", reason)
        skip("/cf rejects a write with no password", reason)
    else:
        try:
            r = api_noauth("jf")
            check("/jf rejects a request with no password",
                  r.get("result") == UNAUTHORIZED, f"got {r}")
        except urllib.error.HTTPError as e:
            check("/jf rejects a request with no password", e.code in (401, 403))

        try:
            with urllib.request.urlopen(f"{BASE}/cf?fs=1", timeout=10) as resp:
                r = json.loads(resp.read().decode())
            check("/cf rejects a write with no password",
                  r.get("result") == UNAUTHORIZED, f"got {r}")
        except urllib.error.HTTPError as e:
            check("/cf rejects a write with no password", e.code in (401, 403))

    api("cf", fs=2)
    check("/ja carries the fertigation block, agreeing with /jf",
          api("ja").get("fertigation", {}).get("fert_station")
          == api("jf")["fert_station"] == 2,
          f'/ja: {api("ja").get("fertigation")}, /jf: {api("jf")}')
    api("cf", fs=255)


# --------------------------------------------------------- program encoding

PROG_ENABLED = 1 << 0
# starttime_type: 0 = repeating (starttimes[1] and [2] are repeat count and
# interval), 1 = fixed start times. Leaving this clear while passing -1 for the
# unused start-time slots feeds -1 in as the repeat count, which schedules a
# spurious run. Always set it for fixed-start programs.
PROG_FIXED_START = 1 << 6
PROG_EN_DATERANGE = 1 << 7  # en_daterange is the last 1-bit field of the flag byte

DEFAULT_FLAG = PROG_ENABLED | PROG_FIXED_START


def make_program(durations, fert=None, name="test", start=0, flag=DEFAULT_FLAG):
    """Build the packed `v=` payload for /cp."""
    ns = len(durations)
    starts = [start, -1, -1, -1]
    v = f"[{flag},127,0,[{','.join(str(s) for s in starts)}],"
    v += f"[{','.join(str(d) for d in durations)}]"
    if fert is not None:
        v += f",[{','.join(str(f) for f in fert[:ns])}]"
    v += "]"
    return v


def get_program(pid=0):
    return api("jp")["pd"][pid]


def test_program_fert_roundtrip():
    print("\n[/cp, /jp] fertigation durations in programs")
    clear_programs()
    ns = nstations()

    durs = [60] + [0] * (ns - 1)
    ferts = [20] + [0] * (ns - 1)
    r = api("cp", pid=-1, v=make_program(durs, ferts, "ferttest"), name="ferttest")
    check("program with a fertigation array is accepted", r["result"] == OK, f"got {r}")

    p = get_program(0)
    check("program round-trips with the fertigation array present", len(p) >= 6,
          f"program entry has {len(p)} fields: {p}")
    if len(p) >= 6:
        check("fertigation durations survive the round-trip",
              p[5][0] == 20 and all(x == 0 for x in p[5][1:]),
              f"got {p[5]}")
        check("station durations are unaffected by the fertigation array",
              p[4][0] == 60, f"got {p[4]}")

    clear_programs()
    r = api("cp", pid=-1, v=make_program(durs, None, "nofert"), name="nofert")
    check("program with NO fertigation array is still accepted (backward compat)",
          r["result"] == OK, f"got {r}")
    p = get_program(0)
    if len(p) >= 6:
        check("omitted fertigation array defaults to all zeros",
              all(x == 0 for x in p[5]), f"got {p[5]}")


def test_program_name_and_daterange():
    """Regression for 226c51d: name and date range parsing broke when the
    fertigation array was appended to the packed program payload."""
    print("\n[/cp, /jp] program name and date range (regression 226c51d)")
    clear_programs()
    ns = nstations()
    durs = [60] + [0] * (ns - 1)
    ferts = [20] + [0] * (ns - 1)

    r = api("cp", pid=-1,
            v=make_program(durs, ferts, flag=DEFAULT_FLAG | PROG_EN_DATERANGE),
            name="MyProgram", **{"from": 33, "to": 415})
    check("program with name + date range is accepted", r["result"] == OK, f"got {r}")

    p = get_program(0)
    check("program name is not corrupted by the fertigation array",
          p[6] == "MyProgram", f"got name {p[6]!r}")
    check("date range enable flag round-trips", p[7][0] == 1, f"got {p[7]}")
    check("date range values round-trip", p[7][1] == 33 and p[7][2] == 415,
          f"got {p[7]}")


def test_runonce_fert_accepted():
    print("\n[/cr] run-once fertigation parameters")
    clear_programs()
    ns = nstations()
    api("cf", fs=ns - 1)

    # /cr returns DATA_MISSING unless at least one station has a nonzero
    # duration, so station 0 always gets one.
    t = ["0"] * ns
    t[0] = "20"
    t = ",".join(t)

    r = api("cr", t=f"[{t}]", uwt=0, fd0=10)
    check("run-once accepts fd<n> fertigation parameters", r["result"] == OK,
          f"got {r}")
    api("cv", rsn=1)
    time.sleep(1)

    api("cf", fs=255)
    r = api("cr", t=f"[{t}]", uwt=0, fd0=10)
    check("run-once still succeeds when no fertigation station is configured",
          r["result"] == OK, f"got {r}")
    api("cv", rsn=1)
    time.sleep(1)


# ------------------------------------------------------------- runtime tests

def station_bits():
    return api("js")["sn"]


def watch_station(sid, seconds, poll=1.0):
    """Return [(elapsed_seconds, bit)] transitions for one station."""
    t0 = time.time()
    last = None
    events = []
    while time.time() - t0 < seconds:
        bits = station_bits()
        b = bits[sid]
        if b != last:
            events.append((round(time.time() - t0, 1), b))
            last = b
        time.sleep(poll)
    return events


def test_fert_station_not_directly_schedulable():
    """Regression for 6abb3bf: the fertigation station must not run as an
    ordinary station even when explicitly given a run-once duration."""
    print("\n[runtime] fertigation station cannot be scheduled directly")
    clear_programs()
    ns = nstations()
    fert_sid = ns - 1
    api("cf", fs=fert_sid)

    t = ["0"] * ns
    t[fert_sid] = "20"
    api("cr", t=f"[{','.join(t)}]", uwt=0)

    time.sleep(6)
    bits = station_bits()
    check("fertigation station stays off when scheduled directly",
          bits[fert_sid] == 0,
          f"fert station {fert_sid} bit={bits[fert_sid]}, all bits={bits}")

    api("cv", rsn=1)
    time.sleep(1)


def test_fert_timing_is_centred():
    """The fertigation window must sit in the middle of the station's run:
        delay    = (station_dur - fert_dur) / 2
        fert on  at station_start + delay
        fert off at station_start + delay + fert_dur
    """
    print("\n[runtime] fertigation window is centred in the station run")
    clear_programs()
    ns = nstations()
    fert_sid = ns - 1
    zone = 0
    station_dur, fert_dur = 30, 10
    expected_on, expected_off = (station_dur - fert_dur) // 2, (station_dur - fert_dur) // 2 + fert_dur

    api("cf", fs=fert_sid)
    t = ["0"] * ns
    t[zone] = str(station_dur)
    r = api("cr", t=f"[{','.join(t)}]", uwt=0, **{f"fd{zone}": fert_dur})
    check("run-once with fertigation starts", r["result"] == OK, f"got {r}")

    print(f"        watching {station_dur + 6}s "
          f"(expect fert on ~+{expected_on}s, off ~+{expected_off}s)...")
    t0 = time.time()
    zone_on = fert_on = fert_off = None
    while time.time() - t0 < station_dur + 6:
        bits = station_bits()
        now = time.time() - t0
        if zone_on is None and bits[zone] == 1:
            zone_on = now
        if zone_on is not None and fert_on is None and bits[fert_sid] == 1:
            fert_on = now - zone_on
        if fert_on is not None and fert_off is None and bits[fert_sid] == 0:
            fert_off = now - zone_on
        time.sleep(0.5)

    check("zone station actually ran", zone_on is not None)
    check("fertigation valve opened during the run", fert_on is not None,
          "fertigation valve never opened")
    if fert_on is not None:
        check(f"fertigation opens centred (~{expected_on}s after zone start)",
              abs(fert_on - expected_on) <= 3.0,
              f"opened at +{fert_on:.1f}s, expected +{expected_on}s (tol 3s)")
    check("fertigation valve closed again", fert_off is not None,
          "fertigation valve never closed")
    if fert_off is not None:
        check(f"fertigation closes after {fert_dur}s (~+{expected_off}s)",
              abs(fert_off - expected_off) <= 3.0,
              f"closed at +{fert_off:.1f}s, expected +{expected_off}s (tol 3s)")

    api("cv", rsn=1)
    time.sleep(1)


def test_fert_duration_clamped_to_station():
    """fert_dur > station_dur must clamp to station_dur, not overrun it."""
    print("\n[runtime] fertigation duration clamps to the station duration")
    clear_programs()
    ns = nstations()
    fert_sid, zone = ns - 1, 0
    station_dur, fert_dur = 15, 60

    api("cf", fs=fert_sid)
    t = ["0"] * ns
    t[zone] = str(station_dur)
    api("cr", t=f"[{','.join(t)}]", uwt=0, **{f"fd{zone}": fert_dur})

    t0 = time.time()
    fert_seen = False
    overran = False
    while time.time() - t0 < station_dur + 8:
        bits = station_bits()
        if bits[fert_sid] == 1:
            fert_seen = True
            if bits[zone] == 0:
                overran = True
        time.sleep(0.5)

    check("fertigation valve opened", fert_seen)
    check("fertigation valve never stayed open past the zone",
          not overran, "fert valve was on while the zone was off")

    api("cv", rsn=1)
    time.sleep(1)


def test_zero_fert_duration_never_opens():
    print("\n[runtime] fertigation duration of 0 never opens the valve")
    clear_programs()
    ns = nstations()
    fert_sid, zone = ns - 1, 0

    api("cf", fs=fert_sid)
    t = ["0"] * ns
    t[zone] = "20"
    api("cr", t=f"[{','.join(t)}]", uwt=0, **{f"fd{zone}": 0})

    events = watch_station(fert_sid, 14, poll=0.5)
    opened = any(bit == 1 for _, bit in events)
    check("fertigation valve stays closed when fd=0", not opened,
          f"transitions: {events}")

    api("cv", rsn=1)
    time.sleep(1)


# ---------------------------------------------------------------- harness

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    binary = os.path.abspath(sys.argv[1])
    datadir = tempfile.mkdtemp(prefix="ostest-")

    proc = subprocess.Popen(
        [binary, "-d", datadir],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(binary),
    )

    try:
        for _ in range(40):
            try:
                api("jc")
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("FATAL: firmware did not come up on port", PORT)
            sys.exit(1)

        print(f"firmware up on {BASE}, data dir {datadir}")
        print(f"stations: {nstations()}")

        # The DEMO build ships with IOPT_IGNORE_PASSWORD enabled, which would
        # make the authentication assertions vacuous. Force it off (index 25).
        global AUTH_ENFORCED
        api("co", o25=0)
        try:
            AUTH_ENFORCED = api_noauth("jf").get("result") == UNAUTHORIZED
        except urllib.error.HTTPError:
            AUTH_ENFORCED = True
        print("password enforcement: "
              + ("on" if AUTH_ENFORCED else "bypassed (DEMO build)"))

        test_fert_station_config()
        test_program_fert_roundtrip()
        test_program_name_and_daterange()
        test_runonce_fert_accepted()
        test_fert_station_not_directly_schedulable()
        test_fert_timing_is_centred()
        test_fert_duration_clamped_to_station()
        test_zero_fert_duration_never_opens()

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(datadir, ignore_errors=True)

    total = _passed + len(_failures)
    print(f"\n{'=' * 60}\n{_passed}/{total} passed"
          + (f", {len(_skipped)} skipped" if _skipped else ""))
    if _failures:
        print("failed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all green")


if __name__ == "__main__":
    main()
