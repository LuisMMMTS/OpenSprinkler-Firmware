/* OpenSprinkler Unified Firmware
 *
 * Fertigation support.
 *
 * One station may be designated the fertigation station. It is never
 * scheduled on its own; instead it is opened for a configured number of
 * seconds centred inside another station's run, so fertilizer is injected
 * into the middle of that zone's watering.
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

#pragma once

#include "defines.h"
#include "types.h"

class ProgramStruct;

#define FERT_FILENAME       "fert.dat" // fertigation station data file
#define FERT_STATION_NONE   255        // no fertigation station configured

namespace Fertigation {

// ---- configuration ----

/** Station designated as the fertigation valve, or FERT_STATION_NONE. */
extern unsigned char station;

void load();  // read the configured station from FERT_FILENAME
void save();  // persist the configured station to FERT_FILENAME

/** True if sid is the configured fertigation station. */
bool is_station(unsigned char sid);

/** Validate, set and persist the fertigation station.
 *  Returns false (and changes nothing) if sid is out of range. */
bool set_station(unsigned char sid);

// ---- run-once ----
// Run-once programs carry their fertigation durations out of band, because
// the run-once queue is built without a stored ProgramStruct.

void clear_runonce();
void set_runonce(unsigned char sid, int seconds); // ignores values <= 0

// ---- scheduler ----

/** Evaluate every running station and open or close the fertigation valve.
 *  Call once per scheduler tick, after the station queue is processed. */
void tick(time_os_t curr_time);

/** Notify that a station was turned off, so its fertigation window ends. */
void station_turned_off(unsigned char sid);

} // namespace Fertigation
