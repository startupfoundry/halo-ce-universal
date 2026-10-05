/*
VOICE_FILES.C

New announcer lines (tools/halo_voice.py makes and installs them): the voice
folder of the game's data (the folder holding maps/) has voice.json,

	{ "lines": [ { "name": "overtime", "file": "overtime.wav", ... }, ... ] }

and the WAVs it names, 16-bit PCM at 22050 Hz, mono or stereo. They are read
the first time a map asks for them (port/linux/game/voice_lines.c, which
makes the game's sound tags of them) and encoded as Xbox ADPCM in stereo, as
the announcer's own sounds are: the sound manager plays nothing else.

audio.voice_lines = false reads none. Nothing in the folder is part of the
port: the lines are the player's own.
*/

#include "platform.h"
#include "port_config.h"

#include <SDL3/SDL.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAXIMUM_VOICE_FILES 64
#define VOICE_RATE 22050
#define VOICE_CHANNELS 2
#define ADPCM_BLOCK_BYTES 36
#define ADPCM_BLOCK_SAMPLES 64
/* the most a line can be: the sound cache's blocks are 4 MB in all */
#define MAXIMUM_VOICE_SECONDS 20

long voice_files_count(void);
char const *voice_file_name(long index);
unsigned char const *voice_file_samples(long index, unsigned long *size, unsigned long *frames);

/* ---------- the files */

struct voice_file
{
	char name[32];
	unsigned char *adpcm;
	unsigned long size;
	unsigned long frames;
};

static struct voice_file voice_files[MAXIMUM_VOICE_FILES];
static long voice_file_count;
static int voice_files_read;

static unsigned char *file_read(const char *path, unsigned long *size)
{
#ifdef HALO_ARM64_GUEST
	FILE *file = fopen(path, "rb");
	unsigned char *data = NULL;
	long length;

	if (!file)
		return NULL;
	if (fseek(file, 0, SEEK_END) == 0 && (length = ftell(file)) >= 0 && fseek(file, 0, SEEK_SET) == 0)
	{
		data = malloc((size_t)length + 1);
		if (data && fread(data, 1, (size_t)length, file) == (size_t)length)
		{
			data[length] = 0;
			*size = (unsigned long)length;
		}
		else
		{
			free(data);
			data = NULL;
		}
	}
	fclose(file);
	return data;
#else
	size_t length = 0;
	void *loaded = SDL_LoadFile(path, &length);
	unsigned char *data;

	if (!loaded)
		return NULL;
	data = malloc(length + 1);
	if (data)
	{
		memcpy(data, loaded, length);
		data[length] = 0;
	}
	SDL_free(loaded);
	*size = (unsigned long)length;
	return data;
#endif
}

/* ---------- voice.json: only the lines' names and files are read */

struct json
{
	const char *at;
	const char *end;
	int failed;
};

static void json_space(struct json *json)
{
	while (json->at < json->end && (*json->at == ' ' || *json->at == '\t' || *json->at == '\r' || *json->at == '\n'))
		json->at++;
}

static int json_expect(struct json *json, char character)
{
	json_space(json);
	if (json->at < json->end && *json->at == character)
	{
		json->at++;
		return 1;
	}
	json->failed = 1;
	return 0;
}

static int json_peek(struct json *json, char character)
{
	json_space(json);
	return json->at < json->end && *json->at == character;
}

/* a string into text (ASCII escapes kept, others dropped), up to size - 1 */
static void json_string(struct json *json, char *text, unsigned long size)
{
	unsigned long length = 0;

	if (!json_expect(json, '"'))
		return;
	while (json->at < json->end && *json->at != '"')
	{
		char character = *json->at++;

		if (character == '\\' && json->at < json->end)
		{
			character = *json->at++;
			if (character == 'u')
			{
				json->at += json->end - json->at >= 4 ? 4 : json->end - json->at;
				character = '?';
			}
			else if (character == 'n' || character == 't' || character == 'r' || character == 'b' || character == 'f')
			{
				character = ' ';
			}
		}
		if (text && length + 1 < size)
			text[length++] = character;
	}
	if (text && size)
		text[length] = 0;
	json_expect(json, '"');
}

static void json_skip(struct json *json, int depth)
{
	json_space(json);
	if (json->at >= json->end || depth > 32)
	{
		json->failed = 1;
		return;
	}
	if (*json->at == '"')
	{
		json_string(json, NULL, 0);
	}
	else if (*json->at == '{' || *json->at == '[')
	{
		char close = *json->at == '{' ? '}' : ']';
		int object = close == '}';

		json->at++;
		if (json_peek(json, close))
		{
			json->at++;
			return;
		}
		while (!json->failed)
		{
			if (object)
			{
				json_string(json, NULL, 0);
				json_expect(json, ':');
			}
			json_skip(json, depth + 1);
			if (json_peek(json, ','))
				json->at++;
			else
				break;
		}
		json_expect(json, close);
	}
	else
	{
		/* a number, true, false or null */
		while (json->at < json->end && !strchr(",}] \t\r\n", *json->at))
			json->at++;
	}
}

/* each line of the "lines" list: its name and file */
static long json_lines(const char *text, unsigned long size, char (*names)[32], char (*files)[256], long maximum)
{
	struct json json = { text, text + size, 0 };
	long count = 0;

	if (!json_expect(&json, '{'))
		return -1;
	while (!json.failed && !json_peek(&json, '}'))
	{
		char key[32];

		json_string(&json, key, sizeof(key));
		json_expect(&json, ':');
		if (!strcmp(key, "lines") && json_peek(&json, '['))
		{
			json.at++;
			while (!json.failed && !json_peek(&json, ']'))
			{
				char name[32] = "", file[256] = "";

				json_expect(&json, '{');
				while (!json.failed && !json_peek(&json, '}'))
				{
					char field[32];

					json_string(&json, field, sizeof(field));
					json_expect(&json, ':');
					if (!strcmp(field, "name") && json_peek(&json, '"'))
						json_string(&json, name, sizeof(name));
					else if (!strcmp(field, "file") && json_peek(&json, '"'))
						json_string(&json, file, sizeof(file));
					else
						json_skip(&json, 1);
					if (json_peek(&json, ','))
						json.at++;
				}
				json_expect(&json, '}');
				if (!json.failed && name[0] && file[0] && count < maximum)
				{
					strcpy(names[count], name);
					strcpy(files[count], file);
					count++;
				}
				if (json_peek(&json, ','))
					json.at++;
			}
			json_expect(&json, ']');
		}
		else
		{
			json_skip(&json, 0);
		}
		if (json_peek(&json, ','))
			json.at++;
	}
	return json.failed ? -1 : count;
}

/* ---------- WAV */

static unsigned long little32(const unsigned char *bytes)
{
	return bytes[0] | (bytes[1] << 8) | (bytes[2] << 16) | ((unsigned long)bytes[3] << 24);
}

static unsigned little16(const unsigned char *bytes)
{
	return bytes[0] | (bytes[1] << 8);
}

/* a PCM WAV's samples as stereo frames (mono doubled); NULL with why */
static short *wav_stereo(const unsigned char *data, unsigned long size, unsigned long *frame_count, const char **problem)
{
	unsigned long offset = 12, channels = 0, rate = 0, bits = 0, format = 0;
	const unsigned char *samples = NULL;
	unsigned long sample_bytes = 0, frames, frame;
	short *stereo;

	*problem = "not a RIFF WAVE file";
	if (size < 12 || memcmp(data, "RIFF", 4) || memcmp(data + 8, "WAVE", 4))
		return NULL;
	while (offset + 8 <= size)
	{
		unsigned long chunk = little32(data + offset + 4);
		const unsigned char *body = data + offset + 8;

		if (chunk > size - offset - 8)
			chunk = size - offset - 8;
		if (!memcmp(data + offset, "fmt ", 4) && chunk >= 16)
		{
			format = little16(body);
			channels = little16(body + 2);
			rate = little32(body + 4);
			bits = little16(body + 14);
			/* WAVE_FORMAT_EXTENSIBLE: the subformat's first word */
			if (format == 0xFFFE && chunk >= 26)
				format = little16(body + 24);
		}
		else if (!memcmp(data + offset, "data", 4))
		{
			samples = body;
			sample_bytes = chunk;
		}
		offset += 8 + chunk + (chunk & 1);
	}
	if (format != 1 || bits != 16 || (channels != 1 && channels != 2) || !samples)
	{
		*problem = "not 16-bit PCM, mono or stereo";
		return NULL;
	}
	if (rate != VOICE_RATE)
	{
		*problem = "not at 22050 Hz (tools/halo_voice.py master resamples)";
		return NULL;
	}
	frames = sample_bytes / (2 * channels);
	if (!frames || frames > (unsigned long)VOICE_RATE * MAXIMUM_VOICE_SECONDS)
	{
		*problem = "empty, or longer than 20 seconds";
		return NULL;
	}
	stereo = malloc(frames * VOICE_CHANNELS * sizeof(short));
	if (!stereo)
	{
		*problem = "out of memory";
		return NULL;
	}
	for (frame = 0; frame < frames; frame++)
	{
		const unsigned char *at = samples + frame * 2 * channels;

		stereo[frame * 2] = (short)little16(at);
		stereo[frame * 2 + 1] = (short)little16(at + (channels == 2 ? 2 : 0));
	}
	*frame_count = frames;
	return stereo;
}

/* ---------- Xbox ADPCM (dsound_sdl.c's decode_adpcm, the other way) */

static const int ima_index_table[16] =
{
	-1, -1, -1, -1, 2, 4, 6, 8,
	-1, -1, -1, -1, 2, 4, 6, 8,
};

static const int ima_step_table[89] =
{
	7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
	50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
	253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
	1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
	3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
	11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
	32767,
};

/* the nibble nearest sample from the decoder's state, which it then moves
on exactly as the decoder will */
static int ima_encode(int sample, int *predictor, int *index)
{
	int step = ima_step_table[*index];
	int difference = sample - *predictor;
	int nibble = 0;
	int delta;

	if (difference < 0)
	{
		nibble = 8;
		difference = -difference;
	}
	if (difference >= step)
	{
		nibble |= 4;
		difference -= step;
	}
	if (difference >= step >> 1)
	{
		nibble |= 2;
		difference -= step >> 1;
	}
	if (difference >= step >> 2)
		nibble |= 1;
	delta = step >> 3;
	if (nibble & 1) delta += step >> 2;
	if (nibble & 2) delta += step >> 1;
	if (nibble & 4) delta += step;
	if (nibble & 8) delta = -delta;
	*predictor += delta;
	if (*predictor > 32767) *predictor = 32767;
	if (*predictor < -32768) *predictor = -32768;
	*index += ima_index_table[nibble];
	if (*index < 0) *index = 0;
	if (*index > 88) *index = 88;
	return nibble;
}

/* stereo frames as Xbox ADPCM blocks: per block, each channel's 4-byte header
(the decoder's predictor and step index going in), then 4-byte groups of
eight nibbles, low nibble first, alternating between the channels; the last
block filled out with silence */
static unsigned char *adpcm_encode(const short *stereo, unsigned long frames, unsigned long *size)
{
	unsigned long blocks = (frames + ADPCM_BLOCK_SAMPLES - 1) / ADPCM_BLOCK_SAMPLES;
	unsigned long block_bytes = ADPCM_BLOCK_BYTES * VOICE_CHANNELS;
	unsigned char *out = calloc(blocks ? blocks : 1, block_bytes);
	int predictor[VOICE_CHANNELS] = { 0 }, index[VOICE_CHANNELS] = { 0 };
	unsigned long block, channel;

	if (!out)
		return NULL;
	for (block = 0; block < blocks; block++)
	{
		unsigned char *data = out + block * block_bytes;

		for (channel = 0; channel < VOICE_CHANNELS; channel++)
		{
			unsigned long group, byte;

			data[channel * 4] = (unsigned char)(predictor[channel] & 0xFF);
			data[channel * 4 + 1] = (unsigned char)((predictor[channel] >> 8) & 0xFF);
			data[channel * 4 + 2] = (unsigned char)index[channel];
			data[channel * 4 + 3] = 0;
			for (group = 0; group < 8; group++)
			{
				unsigned char *nibbles = data + 4 * VOICE_CHANNELS + (group * VOICE_CHANNELS + channel) * 4;

				for (byte = 0; byte < 4; byte++)
				{
					unsigned long frame = block * ADPCM_BLOCK_SAMPLES + group * 8 + byte * 2;
					int low = frame < frames ? stereo[frame * 2 + channel] : 0;
					int high = frame + 1 < frames ? stereo[(frame + 1) * 2 + channel] : 0;
					int first = ima_encode(low, &predictor[channel], &index[channel]);
					int second = ima_encode(high, &predictor[channel], &index[channel]);

					nibbles[byte] = (unsigned char)(first | (second << 4));
				}
			}
		}
	}
	*size = blocks * block_bytes;
	return out;
}

/* ---------- reading */

static void voice_files_read_all(void)
{
	char folder[1100], path[1400];
	char (*names)[32] = NULL;
	char (*files)[256] = NULL;
	unsigned char *json;
	unsigned long json_size = 0;
	long count, line;

	voice_files_read = 1;
	if (!config_boolean("audio.voice_lines"))
		return;
	snprintf(folder, sizeof(folder), "%s/voice", platform_data_root());
	snprintf(path, sizeof(path), "%s/voice.json", folder);
	json = file_read(path, &json_size);
	if (!json)
		return;
	names = malloc(MAXIMUM_VOICE_FILES * sizeof(*names));
	files = malloc(MAXIMUM_VOICE_FILES * sizeof(*files));
	count = names && files ? json_lines((const char *)json, json_size, names, files, MAXIMUM_VOICE_FILES) : -1;
	if (count < 0)
		platform_log("voice: %s is not a list of lines (tools/halo_voice.py install writes it)", path);
	for (line = 0; line < count; line++)
	{
		struct voice_file *voice = &voice_files[voice_file_count];
		unsigned long size = 0, frames = 0;
		unsigned char *wav;
		short *stereo;
		const char *problem = "cannot be read";

		if (strchr(files[line], '/') || strchr(files[line], '\\'))
		{
			platform_log("voice: %s: the file must be in the voice folder", names[line]);
			continue;
		}
		snprintf(path, sizeof(path), "%s/%s", folder, files[line]);
		wav = file_read(path, &size);
		stereo = wav ? wav_stereo(wav, size, &frames, &problem) : NULL;
		free(wav);
		if (!stereo)
		{
			platform_log("voice: %s: %s %s; left out", names[line], path, problem);
			continue;
		}
		voice->adpcm = adpcm_encode(stereo, frames, &voice->size);
		free(stereo);
		if (!voice->adpcm)
			continue;
		snprintf(voice->name, sizeof(voice->name), "%s", names[line]);
		voice->frames = frames;
		voice_file_count++;
		platform_log("voice: %s (%s): %lu samples, %.2f s, %lu bytes of Xbox ADPCM", voice->name, files[line],
			frames, (double)frames / VOICE_RATE, voice->size);
	}
	free(names);
	free(files);
	free(json);
}

/* ---------- public code */

long voice_files_count(void)
{
	if (!voice_files_read)
		voice_files_read_all();
	return voice_file_count;
}

char const *voice_file_name(long index)
{
	return index >= 0 && index < voice_files_count() ? voice_files[index].name : NULL;
}

/* the line's Xbox ADPCM, stereo at 22050 Hz */
unsigned char const *voice_file_samples(long index, unsigned long *size, unsigned long *frames)
{
	if (index < 0 || index >= voice_files_count())
		return NULL;
	*size = voice_files[index].size;
	*frames = voice_files[index].frames;
	return voice_files[index].adpcm;
}
