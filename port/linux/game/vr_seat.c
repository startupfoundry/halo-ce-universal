/*
VR_SEAT.C

The VR mode's view through a vehicle's seat (HALO_VR; vr.vehicle_view,
port/linux/README.md "VR"), for vr_render.c, and its automated test.

debug.vr_test_seat seats the first player in the nearest vehicle's seat
whose label has the given text in it ("gunner", "driver", ...), as the
action button does (unit_enter_seat: the seat's entering animation), and
takes them out as it does (unit_try_and_exit_seat) some seconds later. From
a second before getting in to a few seconds after getting out, each frame's
centre camera is logged against the last's, how far it moved and turned,
with the phase (on foot, entering, seated, exiting), and each phase's sums
as it ends: the view's motion the player did not make, with the head held
still (debug.vr_test_head = "0").

Called from the main loop every frame (main.c), and with each VR frame's
centre camera (vr_render.c).
*/

#ifdef HALO_VR

#include "cseries.h"
#include "math/real_math.h"
#include "game/game.h"
#include "game/players.h"
#include "objects/objects.h"
#include "units/units.h"
#include "units/unit_definitions.h"
#include "tag_files/tag_files.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

/* the platform layer's (port/linux/src/port_config.c) */
const char *config_string(char const *name);
void platform_log(char const *format, ...);

#define METRES_PER_WORLD_UNIT 3.048f
#define DEGREES_PER_RADIAN (180.0f / 3.14159265f)

enum
{
	_seat_phase_on_foot,
	_seat_phase_entering,
	_seat_phase_seated,
	_seat_phase_exiting,
	NUMBER_OF_SEAT_PHASES
};

static char const *const seat_phase_names[NUMBER_OF_SEAT_PHASES] =
{
	"on foot",
	"entering",
	"seated",
	"exiting",
};

static struct
{
	boolean checked;
	/* debug.vr_test_seat: the seat's label's text, when to get in and for
	how long (seconds) */
	boolean testing;
	char seat_label[32];
	real enter_seconds;
	real seated_seconds;
	/* the game's time (ticks) the test got in, and whether it got out */
	long entered_time;
	boolean brought;
	boolean entered;
	boolean exited;
	/* the log: the last frame's camera, its phase, and the phase's sums */
	boolean logging;
	boolean last_valid;
	real_point3d last_position;
	real_vector3d last_forward;
	real_vector3d last_up;
	short phase;
	long phase_frames;
	real phase_distance;
	real phase_turn;
	real phase_largest_move;
	real phase_largest_turn;
	long frame;
} vr_seat;

static void vr_seat_read_settings(void)
{
	char const *setting = config_string("debug.vr_test_seat");

	vr_seat.checked = TRUE;
	vr_seat.seated_seconds = 10.0f;
	if (sscanf(setting, "%31s %f %f", vr_seat.seat_label, &vr_seat.enter_seconds, &vr_seat.seated_seconds) >= 2)
	{
		vr_seat.testing = TRUE;
		platform_log("vr seat test: the \"%s\" seat %.1f s in, for %.1f s", vr_seat.seat_label,
			vr_seat.enter_seconds, vr_seat.seated_seconds);
	}
}

/* the first player's unit, else NONE */
static long first_player_unit(void)
{
	long player_index = local_player_get_player_index(0);

	return player_index == NONE ? NONE : player_get(player_index)->unit_index;
}

/* the nearest vehicle with a free seat whose label has the test's text in
it, and the seat, else NONE */
static long vr_seat_test_vehicle(long unit_index, short *seat_found)
{
	real_point3d const *position = &object_get(unit_index)->object.position;
	struct object_iterator vehicles;
	long nearest_index = NONE;
	real nearest_distance = 0.0f;

	object_iterator_new(&vehicles, _object_mask_vehicle, 0);
	while (object_iterator_next(&vehicles))
	{
		struct unit_definition *definition = unit_definition_get(object_get(vehicles.index)->definition_index);
		real distance = distance_squared3d(position, &object_get(vehicles.index)->object.position);
		short seat_index;

		if (nearest_index != NONE && distance >= nearest_distance)
			continue;
		for (seat_index = 0; seat_index < definition->unit.seats.count; seat_index++)
		{
			struct unit_seat *seat = TAG_BLOCK_GET_ELEMENT(&definition->unit.seats, seat_index, struct unit_seat);

			if (strstr(seat->label, vr_seat.seat_label) && unit_can_enter_seat(unit_index, vehicles.index, seat_index, NULL))
			{
				nearest_index = vehicles.index;
				*seat_found = seat_index;
				nearest_distance = distance;
				break;
			}
		}
	}
	return nearest_index;
}

/* three seconds before getting in, a vehicle of the kind found is made
before the first player (a level's are far apart, or carried by dropships):
two world units ahead, upright, a little above the ground it falls to */
static void vr_seat_test_bring(long unit_index)
{
	short seat_index = NONE;
	long vehicle_index = vr_seat_test_vehicle(unit_index, &seat_index);
	struct object_datum *unit = object_get(unit_index);
	struct object_placement_data data;
	long new_index;

	if (vehicle_index == NONE)
	{
		platform_log("vr seat test: no vehicle with a free \"%s\" seat", vr_seat.seat_label);
		return;
	}
	object_placement_data_new(&data, object_get(vehicle_index)->definition_index, NONE);
	data.forward.i = unit->object.forward.i;
	data.forward.j = unit->object.forward.j;
	data.forward.k = 0.0f;
	normalize3d(&data.forward);
	data.up = *global_up3d;
	data.position.x = unit->object.position.x + data.forward.i * 2.0f;
	data.position.y = unit->object.position.y + data.forward.j * 2.0f;
	data.position.z = unit->object.position.z + 0.2f;
	new_index = object_new(&data);
	platform_log("vr seat test: %s %s before the player", tag_get_name(object_get(vehicle_index)->definition_index),
		new_index == NONE ? "could not be made" : "made");
}

/* the first player gets into the nearest such seat, standing at its
entrance, as the action button does */
static void vr_seat_test_enter(long unit_index)
{
	short seat_index = NONE;
	long vehicle_index = vr_seat_test_vehicle(unit_index, &seat_index);
	struct unit_seat *seat;
	real_point3d entrance, seat_point;

	if (vehicle_index == NONE)
	{
		platform_log("vr seat test: no vehicle with a free \"%s\" seat", vr_seat.seat_label);
		return;
	}
	seat = TAG_BLOCK_GET_ELEMENT(&unit_definition_get(object_get(vehicle_index)->definition_index)->unit.seats,
		seat_index, struct unit_seat);
	if (unit_get_seat_entrance_point(unit_index, vehicle_index, seat_index, &entrance, &seat_point, NULL))
		object_set_position(unit_index, &entrance, NULL, NULL);
	if (unit_enter_seat(unit_index, vehicle_index, seat_index))
	{
		platform_log("vr seat test: frame %ld: into %s's seat %d (%s)", vr_seat.frame,
			tag_get_name(object_get(vehicle_index)->definition_index), seat_index, seat->label);
	}
	else
	{
		platform_log("vr seat test: could not get into %s's seat %d (%s)",
			tag_get_name(object_get(vehicle_index)->definition_index), seat_index, seat->label);
	}
}

/* each frame: gets in and out on time */
void vr_seat_test_update(void)
{
	long unit_index;
	long time;

	if (!vr_seat.checked)
		vr_seat_read_settings();
	if (!vr_seat.testing || !game_in_progress() || (unit_index = first_player_unit()) == NONE)
		return;
	time = game_time_get();
	vr_seat.logging = time >= (long)((vr_seat.enter_seconds - 1.0f) * TICKS_PER_SECOND) &&
		(!vr_seat.exited || time < vr_seat.entered_time + (long)((vr_seat.seated_seconds + 5.0f) * TICKS_PER_SECOND));
	if (!vr_seat.brought && time >= (long)((vr_seat.enter_seconds - 3.0f) * TICKS_PER_SECOND))
	{
		vr_seat.brought = TRUE;
		vr_seat_test_bring(unit_index);
	}
	if (!vr_seat.entered && time >= (long)(vr_seat.enter_seconds * TICKS_PER_SECOND))
	{
		vr_seat.entered = TRUE;
		vr_seat.entered_time = time;
		vr_seat_test_enter(unit_index);
	}
	if (vr_seat.entered && !vr_seat.exited &&
		time >= vr_seat.entered_time + (long)(vr_seat.seated_seconds * TICKS_PER_SECOND))
	{
		vr_seat.exited = TRUE;
		if (unit_try_and_exit_seat(unit_index))
			platform_log("vr seat test: frame %ld: out of the seat", vr_seat.frame);
		else
			platform_log("vr seat test: could not get out of the seat");
	}
}

/* the first player's phase: on foot, getting in, seated or getting out */
static short vr_seat_phase(long unit_index)
{
	struct unit_datum *unit;

	if (unit_index == NONE || object_get(unit_index)->object.parent_object_index == NONE)
		return _seat_phase_on_foot;
	unit = unit_get(unit_index);
	if (unit->unit.animation.state == _unit_state_entering_seat)
		return _seat_phase_entering;
	if (unit->unit.animation.state == _unit_state_exiting_seat)
		return _seat_phase_exiting;
	return _seat_phase_seated;
}

static real angle_between(real_vector3d const *a, real_vector3d const *b)
{
	real cosine = dot_product3d(a, b);

	return (real)acos(cosine > 1.0f ? 1.0f : cosine < -1.0f ? -1.0f : cosine) * DEGREES_PER_RADIAN;
}

static void vr_seat_log_phase_end(void)
{
	if (vr_seat.phase_frames == 0)
		return;
	platform_log("vr seat log: %s: %ld frames, moved %.3f m (at most %.1f mm a frame), turned %.1f degrees "
		"(at most %.2f a frame)", seat_phase_names[vr_seat.phase], vr_seat.phase_frames, vr_seat.phase_distance,
		vr_seat.phase_largest_move * 1000.0f, vr_seat.phase_turn, vr_seat.phase_largest_turn);
}

/* each VR frame's centre camera (vr_render.c): logs its motion while the
test logs */
void vr_seat_log_camera(real_point3d const *position, real_vector3d const *forward, real_vector3d const *up)
{
	long unit_index;
	short phase;

	vr_seat.frame++;
	if (!vr_seat.logging)
	{
		if (vr_seat.last_valid)
			vr_seat_log_phase_end();
		vr_seat.last_valid = FALSE;
		return;
	}
	unit_index = first_player_unit();
	phase = vr_seat_phase(unit_index);
	if (vr_seat.last_valid)
	{
		real move = distance3d(position, &vr_seat.last_position) * METRES_PER_WORLD_UNIT;
		real turn = MAX(angle_between(forward, &vr_seat.last_forward), angle_between(up, &vr_seat.last_up));
		real_point3d local;
		long parent_index = unit_index == NONE ? NONE : object_get(unit_index)->object.parent_object_index;

		if (phase != vr_seat.phase)
		{
			vr_seat_log_phase_end();
			vr_seat.phase_frames = 0;
			vr_seat.phase_distance = 0.0f;
			vr_seat.phase_turn = 0.0f;
			vr_seat.phase_largest_move = 0.0f;
			vr_seat.phase_largest_turn = 0.0f;
		}
		vr_seat.phase_frames++;
		vr_seat.phase_distance += move;
		vr_seat.phase_turn += turn;
		vr_seat.phase_largest_move = MAX(vr_seat.phase_largest_move, move);
		vr_seat.phase_largest_turn = MAX(vr_seat.phase_largest_turn, turn);
		/* (and the eye in the vehicle's frame, its root node's: whether the
		seat swings about the vehicle) */
		local.x = local.y = local.z = 0.0f;
		if (parent_index != NONE)
			matrix4x3_inverse_transform_point(object_get_node_matrix(parent_index, 0), position, &local);
		platform_log("vr seat log: frame %ld %s: moved %.1f mm, turned %.2f degrees; pitch %.1f; "
			"in the vehicle %.3f %.3f %.3f m", vr_seat.frame, seat_phase_names[phase], move * 1000.0f, turn,
			(real)asin(forward->k > 1.0f ? 1.0f : forward->k < -1.0f ? -1.0f : forward->k) * DEGREES_PER_RADIAN,
			local.x * METRES_PER_WORLD_UNIT, local.y * METRES_PER_WORLD_UNIT, local.z * METRES_PER_WORLD_UNIT);
	}
	vr_seat.phase = phase;
	vr_seat.last_valid = TRUE;
	vr_seat.last_position = *position;
	vr_seat.last_forward = *forward;
	vr_seat.last_up = *up;
}

#endif
