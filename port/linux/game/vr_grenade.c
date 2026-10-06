/*
VR_GRENADE.C

The VR mode's grenade thrown with the left hand (HALO_VR; vr.grenade_throw =
"gesture", port/linux/README.md "VR"): the game's side of the gesture that
port/linux/src/vr.c sees, for player_control.c, units.c and
first_person_weapons.c.

The game throws a grenade in an animation: the throw button starts the
unit's throw animation (unit_throw_grenade_begin), its third frame puts the
grenade in the unit's left hand (unit_throw_grenade_move_to_hand), and its
key frame lets it go (unit_throw_grenade_release), from just before the
eyes, along the aim, at the unit's grenade velocity; let go before then (the
animation cut short), it is thrown more weakly, by the share of the ticks to
the key frame it was held. The first-person weapon plays a throw animation
of its own beside it (first_person_weapons.c), which lowers the weapon and
marks no release.

The hand's swing presses the throw button (vr.c, as the left trigger),
through the game's input and the netcode as any throw is; the hand's
release then times the throw: the animations (the unit's, which others see,
and the first-person one, released on the unit's key frame) wait on the
frame before the key frame while the hand still holds the grenade, and go
on to it at once when the hand lets go sooner, the grenade put in the hand
first if it is not yet there. The grenade then leaves from the hand, along
the hand's throw, as hard as the hand threw it (never harder than the
game's throw). With the hands the controllers' (vr.hands), the first-person
throw is not played: the left hand is the player's own, and the weapon
stays in the right. While the hand throws, the player's facing turns along
the throw (player_control.c), which the netcode carries: a host's or
another player's copy of the throw, released when its own animation lets
go, goes the same way.
*/

#ifdef HALO_VR

#include "cseries.h"
#include "math/real_math.h"
#include "game/game.h"
#include "game/players.h"
#include "items/weapons.h"
#include "objects/objects.h"
#include "units/units.h"

#include "../src/vr.h"

#include <math.h>

void platform_log(char const *format, ...);

static struct
{
	/* the unit whose throw (its throw animation playing) is known, else
	NONE, and whether the hand times it (else it is the button's); whether
	the hand has let go of its grenade, the unit's throw animation's key
	frame, and whether the animations' holds were logged (debug.vr_throw_log:
	the unit's, the first-person one's) */
	long unit_index;
	boolean hand;
	boolean released;
	short key_frame_index;
	boolean hold_logged[2];
} vr_grenade = { NONE };

/* the first player's unit, which the VR mode plays */
static long vr_grenade_player_unit(void)
{
	long player_index = local_player_get_player_index(0);

	if (!halo_vr_running() || player_index == NONE)
		return NONE;
	return player_get(player_index)->unit_index;
}

/* whether the unit's throw is the hand's: the VR player's, begun while the
hand swings or has let go, until its throw is over; the gesture as it is */
static boolean vr_grenade_hand_throw(long unit_index, struct halo_vr_throw *throw_state)
{
	struct unit_datum *unit;
	int phase = halo_vr_throw(throw_state);

	if (unit_index == NONE || unit_index != vr_grenade_player_unit())
		return FALSE;
	unit = unit_get(unit_index);
	switch (unit->unit.grenade_throw_state)
	{
	case _unit_grenade_throw_idle:
		vr_grenade.unit_index = NONE;
		break;
	case _unit_grenade_throw_wind_up:
	case _unit_grenade_throw_in_hand:
		if (vr_grenade.unit_index != unit_index)
		{
			vr_grenade.unit_index = unit_index;
			vr_grenade.hand = phase != HALO_VR_THROW_NONE;
			vr_grenade.released = FALSE;
			vr_grenade.key_frame_index = 0;
			vr_grenade.hold_logged[0] = vr_grenade.hold_logged[1] = FALSE;
			if (throw_state->log)
				platform_log("vr grenade: the game's throw began, tick %ld, %s", game_time_get(),
					phase == HALO_VR_THROW_SWING ? "the hand swinging" : phase == HALO_VR_THROW_RELEASED ?
					"the hand having let go" : "of the button");
		}
		break;
	default:
		break;
	}
	return vr_grenade.unit_index == unit_index && vr_grenade.hand;
}

/* player_control.c, each frame, for the first local player's unit (aiming:
the controller aims it): whether the game would throw a grenade now, for
the hand to take one; the hand's throw over with the game's; and while the
hand throws, the facing along the throw (TRUE, yaw and pitch) */
boolean vr_grenade_facing(long unit_index, boolean aiming, real *yaw, real *pitch)
{
	struct halo_vr_throw throw_state;
	struct unit_datum *unit;
	long weapon_index;
	short grenade_type;
	boolean hand;

	if (unit_index == NONE)
	{
		halo_vr_throw_ready(FALSE);
		return FALSE;
	}
	unit = unit_get(unit_index);
	weapon_index = unit_inventory_get_weapon(unit_index, unit->unit.current_weapon_index);
	grenade_type = unit_get_current_grenade_type(unit_index);
	/* (the rules of unit_throw_grenade_begin: one to throw, on foot, a
	weapon that lets the hand go; the animation's state it checks then) */
	halo_vr_throw_ready(aiming && unit->object.parent_object_index == NONE && grenade_type != NONE &&
		unit_get_grenade_count(unit_index, grenade_type) > 0 &&
		unit->unit.grenade_throw_state == _unit_grenade_throw_idle &&
		(weapon_index == NONE || !weapon_prevents_grenade_throwing(weapon_index)));
	hand = vr_grenade_hand_throw(unit_index, &throw_state);
	if (hand && throw_state.phase != HALO_VR_THROW_NONE &&
		unit->unit.grenade_throw_state == _unit_grenade_throw_ending)
	{
		/* (thrown, or let go of as the animation was cut short: by a
		flinch, a death) */
		halo_vr_throw_done();
		throw_state.phase = HALO_VR_THROW_NONE;
	}
	if (!aiming || throw_state.phase == HALO_VR_THROW_NONE)
		return FALSE;
	*yaw = throw_state.yaw;
	*pitch = throw_state.pitch;
	return TRUE;
}

/* units.c, as the unit's throw begins: whether the first-person weapon
plays its throw: not the hand's throw with the hands the controllers'
(vr.hands): the left hand is the player's own, which throws, and the weapon
stays in the right (the throw lowers it out of the hand) */
boolean vr_grenade_first_person_throw(long unit_index)
{
	struct halo_vr_throw throw_state;

	if (!vr_grenade_hand_throw(unit_index, &throw_state) || !throw_state.hands)
		return TRUE;
	if (throw_state.log)
		platform_log("vr grenade: the first-person weapon stays in the right hand");
	return FALSE;
}

/* units.c and first_person_weapons.c: one of the throw's animations (the
unit's, or the first-person weapon's of the unit's player), at frame_index,
about to go on a frame, its key frame the release: FALSE holds it where it
is, the hand still holding the grenade a frame before the release; the hand
having let go before then, frame_index is moved to the frame before the key
frame, which the animation then goes on to (TRUE). The first-person throws
mark no key frame: theirs is the unit's (both begin on the same tick, and
the game lets the grenade go at the unit's). */
boolean vr_grenade_throw_advance(long unit_index, short *frame_index, short key_frame_index, boolean first_person)
{
	struct halo_vr_throw throw_state;
	char const *which = first_person ? "first-person" : "unit's";

	if (!vr_grenade_hand_throw(unit_index, &throw_state))
		return TRUE;
	if (!first_person)
		vr_grenade.key_frame_index = key_frame_index;
	else if (key_frame_index <= 0)
		key_frame_index = vr_grenade.key_frame_index;
	if (key_frame_index <= *frame_index)
		return TRUE;
	if (throw_state.phase == HALO_VR_THROW_RELEASED)
		vr_grenade.released = TRUE;
	if (vr_grenade.released)
	{
		if (*frame_index + 1 < key_frame_index)
		{
			if (throw_state.log)
				platform_log("vr grenade: tick %ld: the %s animation on from frame %d to its release, %d",
					game_time_get(), which, *frame_index, key_frame_index);
			*frame_index = key_frame_index - 1;
		}
		return TRUE;
	}
	if (throw_state.phase == HALO_VR_THROW_SWING && *frame_index + 1 >= key_frame_index)
	{
		if (throw_state.log && !vr_grenade.hold_logged[first_person])
		{
			platform_log("vr grenade: tick %ld: the %s animation held at frame %d, before its release, %d, "
				"until the hand lets go", game_time_get(), which, *frame_index, key_frame_index);
			vr_grenade.hold_logged[first_person] = TRUE;
		}
		return FALSE;
	}
	return TRUE;
}

/* units.c, as the unit lets its grenade go with the velocity the game
gives it (world units a tick): the hand's throw (TRUE), the grenade moved to
the hand and its velocity turned along the throw, as hard as the hand threw
it of the game's; FALSE when it is not the hand's (the game's own, or cut
short before the hand let go) */
boolean vr_grenade_release(long unit_index, long grenade_index, real_vector3d *initial_velocity)
{
	struct halo_vr_throw throw_state;
	real_point3d eye, hand;
	real_vector3d direction;
	real speed, units, c, s;

	if (!vr_grenade_hand_throw(unit_index, &throw_state) || throw_state.phase != HALO_VR_THROW_RELEASED)
		return FALSE;
	c = (real)cos(throw_state.body_yaw);
	s = (real)sin(throw_state.body_yaw);
	units = throw_state.world_units_per_metre;
	/* from the hand: the head's place in the world is the eye's (as the
	view's is, vr_render.c), the hand from it as from the recentred head */
	unit_get_camera_position(unit_index, &eye);
	hand.x = eye.x + (throw_state.position[0] * c - throw_state.position[1] * s) * units;
	hand.y = eye.y + (throw_state.position[0] * s + throw_state.position[1] * c) * units;
	hand.z = eye.z + throw_state.position[2] * units;
	object_translate(grenade_index, &hand, NULL);
	direction.i = throw_state.direction[0] * c - throw_state.direction[1] * s;
	direction.j = throw_state.direction[0] * s + throw_state.direction[1] * c;
	direction.k = throw_state.direction[2];
	if (normalize3d(&direction) <= 0.0f)
		return FALSE;
	speed = magnitude3d(initial_velocity) * throw_state.power;
	scale_vector3d(&direction, speed, initial_velocity);
	vr_grenade.released = TRUE;
	halo_vr_throw_done();
	if (throw_state.log)
	{
		platform_log("vr grenade: tick %ld: thrown from the hand (%.2f %.2f %.2f m from the head: forward, left, "
			"up), %.2f world units a second (power %.2f), pitched %.0f degrees up", game_time_get(),
			throw_state.position[0], throw_state.position[1], throw_state.position[2], speed * TICKS_PER_SECOND,
			throw_state.power, throw_state.pitch * 180.0f / _pi);
	}
	return TRUE;
}

#endif
