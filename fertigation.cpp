/* OpenSprinkler Unified Firmware
 *
 * Fertigation support. See fertigation.h.
 *
 * This file is part of the OpenSprinkler library
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see
 * <http://www.gnu.org/licenses/>.
 */

#include "fertigation.h"
#include "OpenSprinkler.h"
#include "program.h"
#include "utils.h"

extern OpenSprinkler os;
extern ProgramData pd;
extern uint16_t parse_listdata(char **p);

namespace Fertigation {

unsigned char station = FERT_STATION_NONE;

namespace {

/** The computed injection window for one station's current run. */
struct Window {
	unsigned char active:1;
	time_os_t start;
	time_os_t end;
};

Window   windows[MAX_NUM_STATIONS];
uint16_t runonce[MAX_NUM_STATIONS];
bool     runonce_set = false;

} // anonymous namespace

// ---- configuration ----

void load() {
	if (!file_exists(FERT_FILENAME)) {
		station = FERT_STATION_NONE;
		return;
	}
	// A stored 0 is meaningful here (station 0), which is why the existence
	// check above matters: file_read_byte cannot distinguish "absent" from 0.
	station = file_read_byte(FERT_FILENAME, 0);
	if (station >= MAX_NUM_STATIONS && station != FERT_STATION_NONE) {
		station = FERT_STATION_NONE;
	}
}

void save() {
	file_write_byte(FERT_FILENAME, 0, station);
}

bool is_station(unsigned char sid) {
	return station < MAX_NUM_STATIONS && station == sid;
}

bool set_station(unsigned char sid) {
	// Validate against the stations that actually exist, not the compile-time
	// maximum: designating a station the controller does not have is silently
	// inert and impossible to diagnose from the UI.
	if (sid >= os.nstations && sid != FERT_STATION_NONE) return false;
	station = sid;
	save();
	return true;
}

// ---- run-once ----

void clear_runonce() {
	runonce_set = false;
	memset(runonce, 0, sizeof(runonce));
}

void set_runonce(unsigned char sid, int seconds) {
	// seconds is signed on purpose: it comes straight from atoi() on a query
	// parameter, and a negative must be dropped rather than wrapped.
	if (sid >= MAX_NUM_STATIONS) return;
	if (seconds > 0 && station < MAX_NUM_STATIONS) {
		runonce[sid] = (uint16_t)seconds;
		runonce_set = true;
	}
}

// ---- scheduler ----

void station_turned_off(unsigned char sid) {
	if (sid >= MAX_NUM_STATIONS || !windows[sid].active) return;
	windows[sid].active = 0;
	if (station < MAX_NUM_STATIONS && os.is_running(station)) {
		os.set_station_bit(station, 0, 1);
	}
}

void tick(time_os_t curr_time) {
	if (station >= MAX_NUM_STATIONS) return;

	// Decide whether ANY station wants the valve this tick, then act once.
	// Setting it per station would let station B (outside its own window)
	// close the valve that concurrent station A just opened.
	bool valve_needed = false;
	ProgramStruct prog; // declared once; overwritten per station as needed

	for (unsigned char sid = 0; sid < os.nstations; sid++) {
		if (!os.is_running(sid) || pd.station_qid[sid] >= pd.nqueue) {
			windows[sid].active = 0;
			continue;
		}

		RuntimeQueueStruct *q = pd.queue + pd.station_qid[sid];
		unsigned char prog_id       = q->pid;
		uint16_t      station_dur   = q->dur;
		time_os_t     station_start = q->st;

		if (station_dur == 0 || station_start == 0) continue;

		// Resolve the fertigation seconds for this station's current run.
		uint16_t fert_dur = 0;
		if (prog_id > 0 && prog_id <= pd.nprograms) {
			pd.read(prog_id - 1, &prog); // queue pid is a 1-based program index
			fert_dur = prog.fert_duration[sid];
		} else if (prog_id == RUNONCE_PID && runonce_set) {
			fert_dur = runonce[sid];
		}

		if (fert_dur == 0) {
			windows[sid].active = 0;
			continue;
		}

		// Centre the window inside the run. Note this compares the stored
		// window start against the run start, so it recomputes on every tick
		// rather than only when a new run begins. Recomputation is idempotent
		// for a given run, so the outcome is the same either way.
		if (!windows[sid].active || windows[sid].start != station_start) {
			if (fert_dur > station_dur) fert_dur = station_dur;
			uint16_t delay = (station_dur - fert_dur) / 2;
			windows[sid].start  = station_start + delay;
			windows[sid].end    = station_start + delay + fert_dur;
			windows[sid].active = 1;
		}

		if (curr_time >= windows[sid].start && curr_time < windows[sid].end) {
			valve_needed = true;
		}
	}

	bool valve_running = os.is_running(station);
	if (valve_needed && !valve_running) {
		os.set_station_bit(station, 1, 1);
	} else if (!valve_needed && valve_running) {
		os.set_station_bit(station, 0, 1);
	}
}

} // namespace Fertigation
