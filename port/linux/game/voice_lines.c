/*
VOICE_LINES.C

New multiplayer announcer lines (port/linux/src/voice_files.c reads them
from the data folder's voice/), made into sound tags ('snd!') when a map's
tags load (scenario_tags_load) and added to its tag table, as the menus'
tags are (menu_tags.c). Each is a copy of the announcer's own slayer tag
with one pitch range and the line's samples: the same sound class, gain and
format (Xbox ADPCM, stereo, 22050 Hz), so it plays as the announcer does.

A tag's samples are not in the map file: sound_cache_start_loading_sound
copies them out of memory (voice_line_samples) where it would read the map.

The game plays line N as multiplayer sound
NUMBER_OF_MULTIPLAYER_INFORMATION_SOUNDS + N (game_engine_play_voice_line,
game_engine_multiplayer_sounds.c), queued with the announcer's others.
debug.voice_line plays a line debug.voice_line_seconds into each game.

The tags go in scenario_tags_unload, after the sound cache has let go of
their samples.
*/

#include "cseries.h"
#include "game/game.h"
#include "sound/sound_definitions.h"
#include "tag_files/tag_groups.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* the platform layer's (port/linux/src) */
void platform_log(char const *format, ...);
char const *config_string(char const *name);
double config_real(char const *name);
long voice_files_count(void);
char const *voice_file_name(long index);
unsigned char const *voice_file_samples(long index, unsigned long *size, unsigned long *frames);

/* cache_files.c's (port) */
void *cache_files_tag_instances(long *count);
void cache_files_set_tag_instances(void *instances, long count);

/* game_engine_multiplayer_sounds.c's (port) */
void game_engine_play_voice_line(long line);

void voice_lines_loaded(char const *map_name);
void voice_lines_unloaded(void);
long voice_line_sound(long line);
long voice_line_named(char const *name);
void const *voice_line_samples(struct sound_permutation const *permutation);
void voice_lines_update(void);

/* ---------- constants */

#define VOICE_LINE_TAG_PREFIX "sound\\dialog\\voice_lines\\"
#define VOICE_LINE_TEMPLATE "sound\\dialog\\multiplayer1\\slayer"
#define MAXIMUM_VOICE_LINES 64
#define VOICE_LINE_RATE 22050

/* ---------- structures */

struct cache_file_tag_instance
{
	long group_tag;
	long parent_group_tags[2];
	long tag_index;
	char *name;
	void *base_address;
	unsigned long unused[2];
};

typedef char verify_voice_cache_file_tag_instance_size[
	sizeof(struct cache_file_tag_instance) == 0x20 ? 1 : -1];
typedef char verify_voice_sound_permutation_size[
	sizeof(struct sound_permutation) == 0x7C ? 1 : -1];
typedef char verify_voice_sound_pitch_range_size[
	sizeof(struct sound_pitch_range) == 0x48 ? 1 : -1];
typedef char verify_voice_sound_definition_pitch_ranges_offset[
	offsetof(struct sound_definition, pitch_ranges) == 0x98 ? 1 : -1];

struct voice_line
{
	long tag_index;
	char name[32];
	char tag_name[64];
	struct sound_definition definition;
	struct sound_pitch_range pitch_range;
	struct sound_permutation permutation;
	unsigned char const *samples;
	unsigned long frames;
};

/* ---------- globals */

static struct
{
	boolean loaded;
	struct voice_line *lines;
	long count;
	struct cache_file_tag_instance *instances;
	struct cache_file_tag_instance *original_instances;
	long original_count;
	boolean debug_played;
} voice_lines;

/* ---------- private code */

static void voice_lines_release(void)
{
	if (voice_lines.original_instances)
		cache_files_set_tag_instances(voice_lines.original_instances, voice_lines.original_count);
	if (voice_lines.lines)
		free(voice_lines.lines);
	if (voice_lines.instances)
		free(voice_lines.instances);
	memset(&voice_lines, 0, sizeof(voice_lines));
}

/* the line's tag: the template's definition and first pitch range, with one
permutation, the line's samples (an impulse sound plays one permutation:
only looping sounds follow a permutation's next_permutation_index into the
next piece) */
static boolean voice_line_build(struct voice_line *line, long file_index,
	struct sound_definition const *template_definition, long tag_index)
{
	struct sound_pitch_range const *template_range =
		TAG_BLOCK_GET_ELEMENT(&template_definition->pitch_ranges, 0, struct sound_pitch_range);
	struct sound_permutation *permutation = &line->permutation;
	unsigned long size = 0;

	if (!template_range || template_range->permutations.count < 1)
		return FALSE;
	line->samples = voice_file_samples(file_index, &size, &line->frames);
	if (!line->samples || !size)
		return FALSE;
	*permutation = *TAG_BLOCK_GET_ELEMENT(&template_range->permutations, 0, struct sound_permutation);
	memset(permutation->name, 0, sizeof(permutation->name));
	strncpy(permutation->name, line->name, sizeof(permutation->name) - 1);
	permutation->skip_fraction = 0.0f;
	permutation->gain = 1.0f;
	permutation->next_permutation_index = NONE;
	/* (the sound cache's: no block yet; the tag it is of) */
	permutation->unknown0 = NONE;
	permutation->unknown1 = 0;
	permutation->unknown2 = (unsigned long)tag_index;
	permutation->unknown3 = (unsigned long)tag_index;
	memset(&permutation->samples, 0, sizeof(permutation->samples));
	permutation->samples.size = (long)size;
	permutation->samples.address = (void *)line->samples;
	memset(&permutation->mouth_data, 0, sizeof(permutation->mouth_data));
	memset(&permutation->subtitle_data, 0, sizeof(permutation->subtitle_data));
	line->pitch_range = *template_range;
	line->pitch_range.actual_permutation_count = 1;
	line->pitch_range.played_permutation_mask = 0;
	line->pitch_range.previous_permutation_index = NONE;
	line->pitch_range.forced_permutation_index = NONE;
	line->pitch_range.permutations.count = 1;
	line->pitch_range.permutations.address = permutation;
	line->definition = *template_definition;
	line->definition.pitch_ranges.count = 1;
	line->definition.pitch_ranges.address = &line->pitch_range;
	/* as the maps' announcer tags have it: the samples' bytes over 22050, in
	milliseconds (the length and an eighth, which the announcer's queue waits) */
	line->definition.longest_permutation_length = (long)((unsigned long long)size * 1000 / VOICE_LINE_RATE);
	line->tag_index = tag_index;
	return TRUE;
}

/* ---------- public code */

void voice_lines_loaded(
	char const *map_name)
{
	long template_index, count, existing, index, salt = 0;
	struct sound_definition const *template_definition;
	struct cache_file_tag_instance *instances;

	voice_lines_release();
	if (!strcmp(map_name, "ui") || (count = voice_files_count()) <= 0)
		return;
	template_index = tag_loaded(SOUND_DEFINITION_TAG, VOICE_LINE_TEMPLATE);
	if (template_index == NONE)
		return;
	template_definition = sound_definition_get(template_index);
	if (count > MAXIMUM_VOICE_LINES)
		count = MAXIMUM_VOICE_LINES;
	instances = cache_files_tag_instances(&existing);
	if (!instances)
		return;
	voice_lines.lines = calloc(count, sizeof(*voice_lines.lines));
	voice_lines.instances = malloc((existing + count) * sizeof(*voice_lines.instances));
	if (!voice_lines.lines || !voice_lines.instances)
	{
		voice_lines_release();
		return;
	}
	/* the table with ours after it, every existing tag where it was; a new
	tag's salt above every other's */
	memcpy(voice_lines.instances, instances, existing * sizeof(*instances));
	for (index = 0; index < existing; index++)
	{
		long instance_salt = (unsigned long)instances[index].tag_index >> 16;

		if (instance_salt > salt)
			salt = instance_salt;
	}
	voice_lines.original_instances = instances;
	voice_lines.original_count = existing;
	for (index = 0; index < count; index++)
	{
		struct voice_line *line = &voice_lines.lines[voice_lines.count];
		struct cache_file_tag_instance *instance = &voice_lines.instances[existing + voice_lines.count];
		long tag_index = ((salt + 1 + voice_lines.count) << 16) | (existing + voice_lines.count);

		snprintf(line->name, sizeof(line->name), "%s", voice_file_name(index));
		snprintf(line->tag_name, sizeof(line->tag_name), "%s%s", VOICE_LINE_TAG_PREFIX, line->name);
		if (!voice_line_build(line, index, template_definition, tag_index))
		{
			platform_log("voice: %s: no tag made", line->name);
			memset(line, 0, sizeof(*line));
			continue;
		}
		instance->group_tag = SOUND_DEFINITION_TAG;
		instance->parent_group_tags[0] = NONE;
		instance->parent_group_tags[1] = NONE;
		instance->tag_index = tag_index;
		instance->name = line->tag_name;
		instance->base_address = &line->definition;
		instance->unused[0] = 0;
		instance->unused[1] = 0;
		voice_lines.count++;
	}
	if (!voice_lines.count)
	{
		voice_lines_release();
		return;
	}
	cache_files_set_tag_instances(voice_lines.instances, existing + voice_lines.count);
	voice_lines.loaded = TRUE;
	for (index = 0; index < voice_lines.count; index++)
	{
		struct voice_line const *line = &voice_lines.lines[index];

		platform_log("voice: %s is tag %08lx (%s), %lu samples, %.2f s", line->name,
			(unsigned long)line->tag_index, line->tag_name, line->frames, (double)line->frames / VOICE_LINE_RATE);
	}
}

void voice_lines_unloaded(
	void)
{
	if (voice_lines.loaded || voice_lines.lines)
		voice_lines_release();
}

/* line's sound tag, NONE if there is none */
long voice_line_sound(
	long line)
{
	return voice_lines.loaded && line >= 0 && line < voice_lines.count ? voice_lines.lines[line].tag_index : NONE;
}

long voice_line_named(
	char const *name)
{
	long index;

	for (index = 0; voice_lines.loaded && index < voice_lines.count; index++)
	{
		if (!strcmp(voice_lines.lines[index].name, name))
			return index;
	}
	return NONE;
}

/* the samples of one of our permutations (in memory, not in the map), NULL
for any other */
void const *voice_line_samples(
	struct sound_permutation const *permutation)
{
	long index;

	for (index = 0; voice_lines.loaded && index < voice_lines.count; index++)
	{
		struct voice_line const *line = &voice_lines.lines[index];

		if (permutation == &line->permutation)
		{
			platform_log("voice: %s's samples (%ld bytes) into the sound cache", line->name,
				permutation->samples.size);
			return permutation->samples.address;
		}
	}
	return NULL;
}

/* debug.voice_line: that line, debug.voice_line_seconds into the game */
void voice_lines_update(
	void)
{
	char const *name;
	long line;

	if (voice_lines.debug_played || !voice_lines.loaded)
		return;
	name = config_string("debug.voice_line");
	if (!*name || game_time_get() < (long)(config_real("debug.voice_line_seconds") * TICKS_PER_SECOND))
		return;
	voice_lines.debug_played = TRUE;
	line = voice_line_named(name);
	if (line == NONE)
	{
		platform_log("voice: debug.voice_line: there is no line named %s", name);
		return;
	}
	platform_log("voice: debug.voice_line plays %s at tick %ld", name, game_time_get());
	game_engine_play_voice_line(line);
}
