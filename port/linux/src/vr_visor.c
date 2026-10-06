/*
VR_VISOR.C

The HUD on the helmet's visor (HALO_VR, vr.hud = "visor"; vr_visor.h,
port/linux/README.md "VR"): the HUD as Master Chief's helmet projects it,
not a screen floating before the face.

The HUD is curved about the eyes, each of its elements as large and as
square at the edges as in the middle, all of it at one distance: the game's
640x480 HUD is laid out in angles, its width vr.hud_size degrees around and
its height in proportion, so that the shields' meter, the motion tracker,
the ammunition and the messages are where the game puts them, toward the
edges of the view, but no farther out than the eyes turn comfortably. On a
cylinder layer about the head where the runtime has them
(XR_KHR_composition_layer_cylinder), else on a quad whose image is drawn
curved (d3d8_gl.c): from the eyes the two look the same, the cylinder's
edges merely as near as its middle. The HUD's camera projects through the
same curve (vr_render.c, vr_render_hud_to_screen), so that the waypoints are
where they belong.

The HUD's orientation follows the head's a little behind it (vr.hud_lag):
smoothed, never more than FOLLOW_LIMIT behind, and never past it (no
spring, no overshoot), so that it is still while the head is. The visor's
rim does not lag: it is the helmet, and turns with the head exactly.

The visor (vr.visor_frame, its own layer under the HUD's): the helmet
darkening the edges of the view, and the glass a faint gold (no line where
the visor ends: a line fixed in the view reads as a smudge on the lenses).
And the shields on it (vr.visor_effects), from the
player's unit as the game has it (halo_vr_visor_state): the rim flares
as the shields are hit, pulses red while they are low or gone, the
recharge rises up the rim as they fill, and the HUD flickers as they break
and comes up as the player takes control.
*/

#ifdef HALO_VR
#include "platform.h"
#include "port_config.h"
#include "vr.h"
#include "vr_host.h"
#include "vr_visor.h"

#include <math.h>
#include <string.h>

/* the HUD's turn behind the head's (vr.hud_lag), at most (radians) */
#define FOLLOW_LIMIT (1.5f * (float)M_PI / 180.0f)
/* the visor's image is this far before the eyes (metres), as wide as the
eyes' fields of view together, and this much more (tangents: the eyes are
beside the middle of the head) */
#define VISOR_DISTANCE 1.0f
#define VISOR_MARGIN 0.08f
/* the HUD's height for its width (the 640x480 screen) */
#define HUD_ASPECT 0.75f
/* the shields' effects: the flare as they are hit, the flash as they are
full again, and the body's (red) as it is hurt, each fading over this time
constant (seconds); the low shields' warning pulse (a second); the flicker
as they break, and the HUD coming up as the player takes control (seconds) */
#define HIT_FADE 0.18f
#define FULL_FADE 0.35f
#define WOUND_FADE 0.30f
#define WARNING_PULSE 1.6f
#define WARNING_EASE 0.30f
#define CHARGE_EASE 0.25f
#define FLICKER_SECONDS 0.45f
#define BOOT_SECONDS 0.40f
#define PRESENCE_EASE 0.25f
/* the shields are gone at this (the game's 0, or a frame into recharging) */
#define SHIELD_GONE 0.03f

static struct
{
	unsigned long read_at;
	int visor;
	float lag;
	float frame;
	int effects;
	float glow;
} settings = { (unsigned long)-1 };

static struct
{
	int cylinder;
	int logged;
	int menus;
	/* the frame's head and eyes' fields of view, its seconds, and the HUD's
	distance and half angles (radians) */
	struct vr_host_pose head;
	float fov[2][4];
	float seconds;
	float distance;
	float half_angles[2];
	/* the HUD's orientation, following the head's */
	int orientation_valid;
	float orientation[4];
	/* the player's unit, as last told */
	int active;
	int known;
	int has_shield;
	float shield, body;
	/* the effects: the visor there (0 to 1, eased in and out), the flares,
	the warning and the recharge (0 to 1), the warning's pulse (seconds),
	the flicker's and the HUD's coming up (seconds from their start) */
	float presence;
	float hit, full, wound;
	float warning, charge;
	float pulse;
	float flicker;
	float boot;
} visor;

static float clamp(float value, float low, float high)
{
	return value < low ? low : value > high ? high : value;
}

static void settings_read(void)
{
	if (settings.read_at == config_changes())
		return;
	settings.read_at = config_changes();
	settings.visor = strcmp(config_string("vr.hud"), "flat") != 0;
	settings.lag = clamp((float)config_real("vr.hud_lag"), 0.0f, 0.2f);
	settings.frame = clamp((float)config_real("vr.visor_frame"), 0.0f, 1.0f);
	settings.effects = config_boolean("vr.visor_effects");
	settings.glow = clamp((float)config_real("vr.hud_glow"), 0.0f, 1.0f);
}

int vr_visor_enabled(void)
{
	settings_read();
	return settings.visor;
}

void vr_visor_initialize(const struct vr_host_info *info)
{
	settings_read();
	visor.cylinder = info->cylinder;
	/* (no flicker, the HUD up, until the game says otherwise) */
	visor.flicker = FLICKER_SECONDS;
	visor.boot = BOOT_SECONDS;
	visor.distance = 2.0f;
	visor.half_angles[0] = 0.5f;
	visor.half_angles[1] = 0.5f * HUD_ASPECT;
	if (!settings.visor)
		platform_log("vr: the HUD on a flat quad (vr.hud = \"flat\")");
	else if (visor.cylinder)
		platform_log("vr: the HUD on the visor: a cylinder layer (XR_KHR_composition_layer_cylinder)%s",
			info->visor_width ? ", its rim a quad layer" : "; no rim (no image for it)");
	else
		platform_log("vr: the HUD on the visor: a quad layer, curved in its image (no "
			"XR_KHR_composition_layer_cylinder)%s", info->visor_width ? ", its rim a quad layer" : "; no rim");
}

/* ---------- maths: OpenXR's axes (x right, y up, z backwards) */

static void quaternion_rotate(const float q[4], const float v[3], float out[3])
{
	/* v + 2w(q x v) + 2 q x (q x v) */
	float tx = 2.0f * (q[1] * v[2] - q[2] * v[1]);
	float ty = 2.0f * (q[2] * v[0] - q[0] * v[2]);
	float tz = 2.0f * (q[0] * v[1] - q[1] * v[0]);

	out[0] = v[0] + q[3] * tx + (q[1] * tz - q[2] * ty);
	out[1] = v[1] + q[3] * ty + (q[2] * tx - q[0] * tz);
	out[2] = v[2] + q[3] * tz + (q[0] * ty - q[1] * tx);
}

static void quaternion_multiply(const float a[4], const float b[4], float out[4])
{
	float r[4];

	r[0] = a[3] * b[0] + a[0] * b[3] + a[1] * b[2] - a[2] * b[1];
	r[1] = a[3] * b[1] - a[0] * b[2] + a[1] * b[3] + a[2] * b[0];
	r[2] = a[3] * b[2] + a[0] * b[1] - a[1] * b[0] + a[2] * b[3];
	r[3] = a[3] * b[3] - a[0] * b[0] - a[1] * b[1] - a[2] * b[2];
	memcpy(out, r, sizeof(r));
}

static float quaternion_dot(const float a[4], const float b[4])
{
	return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
}

/* the angle (radians) from one orientation to the other */
static float quaternion_angle(const float a[4], const float b[4])
{
	return 2.0f * acosf(clamp(fabsf(quaternion_dot(a, b)), 0.0f, 1.0f));
}

/* q turned toward target by a fraction t of the way (the angles are small:
along the chord, normalized) */
static void quaternion_towards(float q[4], const float target[4], float t)
{
	float sign = quaternion_dot(q, target) < 0.0f ? -1.0f : 1.0f;
	float length = 0.0f;
	int i;

	for (i = 0; i < 4; i++)
	{
		q[i] += (sign * target[i] - q[i]) * t;
		length += q[i] * q[i];
	}
	length = sqrtf(length);
	for (i = 0; i < 4; i++)
		q[i] /= length;
}

/* ---------- the HUD following the head */

void vr_visor_follow(const struct vr_host_pose *head, const float fov[2][4], float seconds, float distance,
	float size)
{
	settings_read();
	visor.head = *head;
	memcpy(visor.fov, fov, sizeof(visor.fov));
	visor.seconds = clamp(seconds, 0.0f, 0.1f);
	visor.distance = distance;
	/* (a quad's tangents grow without bound toward 90 degrees) */
	visor.half_angles[0] = clamp(size, 20.0f, 120.0f) * 0.5f * (float)M_PI / 180.0f;
	visor.half_angles[1] = visor.half_angles[0] * HUD_ASPECT;
	if (!head->valid)
		return;
	if (!visor.orientation_valid || settings.lag <= 0.0f || visor.seconds <= 0.0f)
	{
		memcpy(visor.orientation, head->orientation, sizeof(visor.orientation));
		visor.orientation_valid = 1;
		return;
	}
	{
		float angle;

		/* smoothed, as a first-order lag (no overshoot), and held within
		FOLLOW_LIMIT of the head */
		quaternion_towards(visor.orientation, head->orientation, 1.0f - expf(-visor.seconds / settings.lag));
		angle = quaternion_angle(visor.orientation, head->orientation);
		if (angle > FOLLOW_LIMIT)
			quaternion_towards(visor.orientation, head->orientation, 1.0f - FOLLOW_LIMIT / angle);
	}
}

int vr_visor_orientation(float orientation[4])
{
	if (!visor.orientation_valid || !visor.head.valid)
	{
		orientation[0] = orientation[1] = orientation[2] = 0.0f;
		orientation[3] = 1.0f;
		return 0;
	}
	memcpy(orientation, visor.orientation, sizeof(visor.orientation));
	return settings.lag > 0.0f;
}

/* the eyes' fields of view together, and the visor's image's edges about
them (tangents: left, right, up, down) */
static void visor_extent(float fov[4], float extent[4])
{
	fov[0] = visor.fov[0][0] < visor.fov[1][0] ? visor.fov[0][0] : visor.fov[1][0];
	fov[1] = visor.fov[0][1] > visor.fov[1][1] ? visor.fov[0][1] : visor.fov[1][1];
	fov[2] = visor.fov[0][2] > visor.fov[1][2] ? visor.fov[0][2] : visor.fov[1][2];
	fov[3] = visor.fov[0][3] < visor.fov[1][3] ? visor.fov[0][3] : visor.fov[1][3];
	extent[0] = fov[0] * 1.04f - VISOR_MARGIN;
	extent[1] = fov[1] * 1.04f + VISOR_MARGIN;
	extent[2] = fov[2] * 1.04f + VISOR_MARGIN;
	extent[3] = fov[3] * 1.04f - VISOR_MARGIN;
}

void vr_visor_layers(struct vr_host_layers *layers, int visor_drawn)
{
	static const float forward[3] = { 0.0f, 0.0f, -1.0f };
	float d = visor.distance, tangent_up = tanf(visor.half_angles[1]);
	int follows = settings.lag > 0.0f && visor.orientation_valid && visor.head.valid;
	int axis;

	if (!visor.logged)
	{
		platform_log("vr: the HUD's layer: %s, %.2f m, %.0f by %.0f degrees, %s", visor.cylinder ? "a cylinder" :
			"a quad", d, visor.half_angles[0] * 2.0f * 180.0f / (float)M_PI,
			visor.half_angles[1] * 2.0f * 180.0f / (float)M_PI, follows ? "following the head" : "on the head");
		visor.logged = 1;
	}
	/* before the head, turned as it was a moment ago (vr.hud_lag), else on
	it (the view space: exactly with it, whatever the frame's prediction) */
	layers->hud_head_locked = !follows;
	memset(&layers->hud_pose, 0, sizeof(layers->hud_pose));
	layers->hud_pose.orientation[3] = 1.0f;
	if (follows)
	{
		memcpy(layers->hud_pose.orientation, visor.orientation, sizeof(visor.orientation));
		memcpy(layers->hud_pose.position, visor.head.position, sizeof(visor.head.position));
	}
	if (visor.cylinder)
	{
		/* about the middle of the head */
		layers->hud_cylinder = 1;
		layers->hud_radius = d;
		layers->hud_angle = 2.0f * visor.half_angles[0];
		layers->hud_size[0] = layers->hud_angle * d;
		layers->hud_size[1] = 2.0f * d * tangent_up;
	}
	else
	{
		/* tall enough for the curve's corners, which are higher on the quad
		than its middle's top (d3d8_gl.c, vr_copy_hud) */
		float offset[3];

		quaternion_rotate(layers->hud_pose.orientation, forward, offset);
		for (axis = 0; axis < 3; axis++)
			layers->hud_pose.position[axis] += offset[axis] * d;
		layers->hud_size[0] = 2.0f * d * tanf(visor.half_angles[0]);
		layers->hud_size[1] = 2.0f * d * tangent_up / cosf(visor.half_angles[0]);
	}
	/* the visor: on the head, over the eyes' whole view */
	layers->visor = visor_drawn;
	if (visor_drawn)
	{
		float fov[4], extent[4];

		visor_extent(fov, extent);
		memset(&layers->visor_pose, 0, sizeof(layers->visor_pose));
		layers->visor_pose.orientation[3] = 1.0f;
		layers->visor_pose.position[0] = (extent[0] + extent[1]) * 0.5f * VISOR_DISTANCE;
		layers->visor_pose.position[1] = (extent[2] + extent[3]) * 0.5f * VISOR_DISTANCE;
		layers->visor_pose.position[2] = -VISOR_DISTANCE;
		layers->visor_size[0] = (extent[1] - extent[0]) * VISOR_DISTANCE;
		layers->visor_size[1] = (extent[2] - extent[3]) * VISOR_DISTANCE;
	}
}

/* ---------- the shields on the visor */

/* toward target over a time constant */
static float ease(float value, float target, float seconds, float time_constant)
{
	return value + (target - value) * (1.0f - expf(-seconds / time_constant));
}

void halo_vr_visor_state(const struct halo_vr_visor_state *state)
{
	float seconds = visor.seconds;

	settings_read();
	visor.presence = ease(visor.presence, state->active ? 1.0f : 0.0f, seconds, PRESENCE_EASE);
	visor.hit *= expf(-seconds / HIT_FADE);
	visor.full *= expf(-seconds / FULL_FADE);
	visor.wound *= expf(-seconds / WOUND_FADE);
	visor.flicker += seconds;
	visor.boot += seconds;
	visor.pulse += seconds;
	if (!state->active)
	{
		visor.known = 0;
		visor.active = 0;
		visor.warning = ease(visor.warning, 0.0f, seconds, WARNING_EASE);
		visor.charge = ease(visor.charge, 0.0f, seconds, CHARGE_EASE);
		return;
	}
	if (!visor.active)
	{
		/* the player takes control: the HUD comes up */
		visor.boot = 0.0f;
		visor.active = 1;
	}
	if (visor.known && state->has_shield)
	{
		float drop = visor.shield - state->shield;

		/* hit: the flare as bright as the hit is hard */
		if (drop > 0.001f)
			visor.hit = clamp(visor.hit + 0.35f + 3.0f * drop, 0.0f, 1.0f);
		/* broken (or as good as: a frame later they may be recharging) */
		if (visor.shield > SHIELD_GONE && state->shield <= SHIELD_GONE)
		{
			platform_log("vr: visor: the shields are down (from %.2f)", visor.shield);
			visor.flicker = 0.0f;
			visor.hit = 1.0f;
		}
		/* full again, after recharging */
		if (visor.shield < 1.0f && state->shield >= 1.0f && visor.charge > 0.1f)
		{
			platform_log("vr: visor: the shields are full");
			visor.full = 1.0f;
		}
	}
	if (visor.known && visor.body - state->body > 0.001f)
		visor.wound = clamp(visor.wound + 0.4f + 3.0f * (visor.body - state->body), 0.0f, 1.0f);
	{
		/* the warning: the shields low (more as they fall), or gone, or the
		body's health low without them */
		float warning = 0.0f;

		if (state->has_shield && state->shield < 0.25f)
			warning = state->shield <= SHIELD_GONE ? 1.0f : 0.4f + 0.6f * (0.25f - state->shield) / 0.25f;
		if (state->body < 0.25f && (!state->has_shield || state->shield <= SHIELD_GONE))
			warning = 1.0f;
		if (warning > 0.0f && visor.warning < 0.05f)
			visor.pulse = 0.0f;
		visor.warning = ease(visor.warning, warning, seconds, WARNING_EASE);
	}
	visor.charge = ease(visor.charge, state->has_shield && state->charging && state->shield < 1.0f ? 1.0f : 0.0f,
		seconds, CHARGE_EASE);
	visor.has_shield = state->has_shield;
	visor.shield = state->shield;
	visor.body = state->body;
	visor.known = 1;
}

/* the HUD's opacity as the shields break: three dips, each shallower, then
none (three in under half a second, then still: not a strobe) */
static float flicker_opacity(float t)
{
	static const float centres[3] = { 0.03f, 0.16f, 0.32f };
	static const float depths[3] = { 0.65f, 0.45f, 0.25f };
	float opacity = 1.0f;
	int dip;

	if (t >= FLICKER_SECONDS)
		return 1.0f;
	for (dip = 0; dip < 3; dip++)
	{
		float x = (t - centres[dip]) / 0.045f;

		if (x > -1.0f && x < 1.0f)
			opacity -= depths[dip] * (1.0f - x * x);
	}
	return clamp(opacity, 0.0f, 1.0f);
}

void vr_visor_menus(int active)
{
	visor.menus = active;
}

void vr_visor_hud_image(struct vr_visor_hud_image *image)
{
	settings_read();
	memset(image, 0, sizeof(*image));
	image->opacity = 1.0f;
	if (!settings.visor || visor.menus)
		return;
	image->curve = visor.cylinder ? 2 : 1;
	image->half_angles[0] = visor.half_angles[0];
	image->half_angles[1] = visor.half_angles[1];
	if (visor.cylinder)
	{
		image->extent[0] = visor.half_angles[0];
		image->extent[1] = tanf(visor.half_angles[1]);
	}
	else
	{
		image->extent[0] = tanf(visor.half_angles[0]);
		image->extent[1] = tanf(visor.half_angles[1]) / cosf(visor.half_angles[0]);
	}
	/* its own light on the glass, brighter as the shields recharge */
	image->glow = settings.glow * (1.0f + 0.5f * visor.charge * (settings.effects ? 1.0f : 0.0f));
	if (settings.effects && visor.active)
	{
		/* coming up: from nothing, with a flicker, as the projector warms */
		if (visor.boot < BOOT_SECONDS)
		{
			float t = visor.boot / BOOT_SECONDS;

			image->opacity = t * t * (3.0f - 2.0f * t) * (t < 0.3f || t > 0.42f ? 1.0f : 0.55f);
		}
		image->opacity *= flicker_opacity(visor.flicker);
	}
}

void vr_visor_image(struct vr_visor_image *image)
{
	static const float shield_colour[3] = { 0.30f, 0.70f, 1.00f };
	static const float full_colour[3] = { 0.45f, 0.80f, 1.00f };
	static const float red[3] = { 1.00f, 0.10f, 0.05f };
	float effects, warning, glow = 0.0f;
	int channel;

	settings_read();
	memset(image, 0, sizeof(*image));
	image->charge_level = -1.0f;
	if (!settings.visor || visor.menus || visor.presence < 0.01f)
		return;
	visor_extent(image->fov, image->extent);
	image->frame = settings.frame * visor.presence;
	effects = settings.effects ? visor.presence : 0.0f;
	/* the warning's pulse: a sine, never off while it lasts */
	warning = visor.warning * (0.55f + 0.45f * sinf(2.0f * (float)M_PI * WARNING_PULSE * visor.pulse));
	for (channel = 0; channel < 3; channel++)
	{
		image->glow[channel] = effects * (shield_colour[channel] * visor.hit * 0.25f +
			full_colour[channel] * visor.full * 0.15f + red[channel] * (visor.wound * 0.30f + warning * 0.22f));
		image->charge[channel] = effects * shield_colour[channel] * visor.charge * 0.12f;
		glow += image->glow[channel] + image->charge[channel];
	}
	/* (an overshield charges from 1 to 3) */
	if (visor.charge > 0.01f && effects > 0.0f)
		image->charge_level = clamp(visor.shield <= 1.0f ? visor.shield : (visor.shield - 1.0f) * 0.5f, 0.0f, 1.0f);
	image->shown = image->frame > 0.001f || glow > 0.001f;
}

/* ---------- debug.screenshot_every: what the headset shows */

/* a layer's pose in the play space: those on the head are from its pose */
static void layer_pose(const struct vr_host_pose *pose, int on_head, const struct vr_host_pose *head,
	float position[3], float orientation[4])
{
	int axis;

	if (!on_head)
	{
		memcpy(position, pose->position, sizeof(pose->position));
		memcpy(orientation, pose->orientation, sizeof(pose->orientation));
		return;
	}
	quaternion_rotate(head->orientation, pose->position, position);
	for (axis = 0; axis < 3; axis++)
		position[axis] += head->position[axis];
	quaternion_multiply(head->orientation, pose->orientation, orientation);
}

/* where a ray (play space) meets a quad (size[0] by size[1] about its
pose's origin, facing +z) or a cylinder (about its pose's y, radius, angle
around): the image's coordinates (0 to 1, v up); 0 if it misses */
static int layer_hit(const float origin[3], const float direction[3], const float position[3],
	const float orientation[4], int cylinder, float radius, float angle, const float size[2], float uv[2])
{
	float inverse[4] = { -orientation[0], -orientation[1], -orientation[2], orientation[3] };
	float o[3], d[3], v[3], t;
	int axis;

	for (axis = 0; axis < 3; axis++)
		v[axis] = origin[axis] - position[axis];
	quaternion_rotate(inverse, v, o);
	quaternion_rotate(inverse, direction, d);
	if (!cylinder)
	{
		if (fabsf(d[2]) < 1e-6f)
			return 0;
		t = -o[2] / d[2];
		if (t <= 0.0f)
			return 0;
		uv[0] = (o[0] + d[0] * t) / size[0] + 0.5f;
		uv[1] = (o[1] + d[1] * t) / size[1] + 0.5f;
	}
	else
	{
		/* (from inside: the far root) */
		float a = d[0] * d[0] + d[2] * d[2], b = 2.0f * (o[0] * d[0] + o[2] * d[2]);
		float c = o[0] * o[0] + o[2] * o[2] - radius * radius, discriminant = b * b - 4.0f * a * c;
		float x, z;

		if (a < 1e-9f || discriminant < 0.0f)
			return 0;
		t = (-b + sqrtf(discriminant)) / (2.0f * a);
		if (t <= 0.0f)
			return 0;
		x = o[0] + d[0] * t;
		z = o[2] + d[2] * t;
		uv[0] = atan2f(x, -z) / angle + 0.5f;
		uv[1] = (o[1] + d[1] * t) / size[1] + 0.5f;
	}
	return uv[0] >= 0.0f && uv[0] < 1.0f && uv[1] >= 0.0f && uv[1] < 1.0f;
}

/* an sRGB value's light, and back (near enough for a picture) */
static float linear(float value)
{
	return powf(value, 2.2f);
}

static void layer_over(unsigned char *pixel, const unsigned char *image, int width, int height, const float uv[2])
{
	const unsigned char *texel = image + ((size_t)(int)(uv[1] * height) * width + (int)(uv[0] * width)) * 4;
	float alpha = texel[3] / 255.0f;
	int channel;

	for (channel = 0; channel < 3; channel++)
	{
		float light = linear(texel[channel] / 255.0f) + linear(pixel[channel] / 255.0f) * (1.0f - alpha);

		pixel[channel] = (unsigned char)(powf(clamp(light, 0.0f, 1.0f), 1.0f / 2.2f) * 255.0f + 0.5f);
	}
}

void vr_visor_composite(unsigned char *eyes[2], int width, int height, const struct vr_host_layers *layers,
	const struct vr_host_pose *head, const unsigned char *hud, int hud_width, int hud_height,
	const unsigned char *visor_pixels, int visor_width, int visor_height)
{
	float hud_position[3], hud_orientation[4], visor_position[3], visor_orientation[4];
	int eye, x, y;

	layer_pose(&layers->hud_pose, layers->hud_head_locked, head, hud_position, hud_orientation);
	layer_pose(&layers->visor_pose, 1, head, visor_position, visor_orientation);
	for (eye = 0; eye < 2; eye++)
	{
		const float *fov = layers->fov[eye];

		for (y = 0; y < height; y++)
		{
			for (x = 0; x < width; x++)
			{
				unsigned char *pixel = eyes[eye] + ((size_t)y * width + x) * 4;
				float local[3], direction[3], uv[2];

				local[0] = fov[0] + (fov[1] - fov[0]) * ((float)x + 0.5f) / (float)width;
				local[1] = fov[3] + (fov[2] - fov[3]) * ((float)y + 0.5f) / (float)height;
				local[2] = -1.0f;
				quaternion_rotate(layers->eye[eye].orientation, local, direction);
				if (layers->visor && visor_pixels && layer_hit(layers->eye[eye].position, direction, visor_position,
					visor_orientation, 0, 0.0f, 0.0f, layers->visor_size, uv))
				{
					layer_over(pixel, visor_pixels, visor_width, visor_height, uv);
				}
				if (layers->hud && hud && layer_hit(layers->eye[eye].position, direction, hud_position,
					hud_orientation, layers->hud_cylinder, layers->hud_radius, layers->hud_angle, layers->hud_size,
					uv))
				{
					layer_over(pixel, hud, hud_width, hud_height, uv);
				}
			}
		}
	}
}
#endif
