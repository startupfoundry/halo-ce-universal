/*
VR_VISOR.H

The HUD on the helmet's visor (HALO_VR, vr.hud = "visor"; vr_visor.c): what
vr.c and the renderer (d3d8_gl.c) ask of it. The game's side is vr.h's
halo_vr_visor_state.
*/

#ifndef __HALO_VR_VISOR_H
#define __HALO_VR_VISOR_H

#include "vr_host.h"

/* the visor's image (vr_host.h's VR_SWAPCHAIN_VISOR): soft gradients, so
small */
#define VR_VISOR_IMAGE_SIZE 640

/* how the HUD's layer shows the game's HUD image (d3d8_gl.c draws it so) */
struct vr_visor_hud_image
{
	/* 0: as it is (vr.hud = "flat", the menus); 1: curved about the eyes, in
	a quad's image; 2: in a cylinder's */
	int curve;
	/* the HUD's half width and half height on the visor (radians) */
	float half_angles[2];
	/* the layer's half extents: a quad's in tangents across and up, a
	cylinder's in radians around and the tangent up */
	float extent[2];
	/* the HUD's opacity (its flicker as the shields break) and its glow (0
	none) */
	float opacity;
	float glow;
};

/* the visor's image: its rim and the glows on it (d3d8_gl.c draws it) */
struct vr_visor_image
{
	int shown;
	/* the image's edges and the eyes' fields of view together, from the
	middle of the head (tangents: left, right, up, down) */
	float extent[4];
	float fov[4];
	/* the frame about the visor (vr.visor_frame, 0 to 1) */
	float frame;
	/* the glow on the rim (linear, premultiplied: added to the view) */
	float glow[3];
	/* the shields' recharge rising up the rim: its glow, and how far up it
	is (0 to 1; less than 0 none) */
	float charge[3];
	float charge_level;
};

/* vr.hud = "visor" */
int vr_visor_enabled(void);
/* the runtime's: whether the HUD can be a cylinder layer */
void vr_visor_initialize(const struct vr_host_info *info);
/* once a frame, the head and the eyes' fields of view located for the
frame's draws: the HUD's orientation follows the head (vr.hud_lag); the
frame's seconds, and the HUD's distance (metres) and width (degrees) */
void vr_visor_follow(const struct vr_host_pose *head, const float fov[2][4], float seconds, float distance,
	float size);
/* the HUD's orientation (play space; the head's without a lag), and
whether it follows the head (not locked to it) */
int vr_visor_orientation(float orientation[4]);
/* the HUD's and the visor's layers for a frame of the game (not the
menus), before the head followed; the visor's image drawn this frame */
void vr_visor_layers(struct vr_host_layers *layers, int visor_drawn);
/* the menus are up (halo_vr_menus): their layer is as it is, and the
visor is not there */
void vr_visor_menus(int active);
/* what the renderer draws for the frame */
void vr_visor_hud_image(struct vr_visor_hud_image *image);
void vr_visor_image(struct vr_visor_image *image);
/* debug.screenshot_every: the frame as the headset shows it, the layers
over each eye's image (BGRA, rows from the bottom), from the layers as
submitted; the HUD's and the visor's images as BGRA, rows from the bottom */
void vr_visor_composite(unsigned char *eyes[2], int width, int height, const struct vr_host_layers *layers,
	const struct vr_host_pose *head, const unsigned char *hud, int hud_width, int hud_height,
	const unsigned char *visor, int visor_width, int visor_height);

/* vr.c's: the layers of the frame presented last, and the head they were
placed before; 0 if there is none */
int halo_vr_submitted(struct vr_host_layers *layers, struct vr_host_pose *head);

#endif
