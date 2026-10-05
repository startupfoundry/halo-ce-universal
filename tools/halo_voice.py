#!/usr/bin/env python3
"""New multiplayer announcer lines in the announcer's own voice, for the port
to play (port/linux/game/voice_lines.c).

The steps, each a subcommand; every file they make is the user's own data
(clips of the user's maps, and lines cloned from them), kept out of the
repository and out of port/assets:

  extract  the announcer's clips (the map's snd! tags under
           sound\\dialog\\multiplayer1\\, Xbox ADPCM) as 22050 Hz WAVs named
           after their tags, with a manifest whose transcripts come from the
           tags' names (the maps carry none):
             python3 tools/halo_voice.py extract --map maps/bloodgulch.map --out extracted
  prepare  a synthesis job: reference montages of the clips with their
           transcripts ("calm": the game types' names, "hype": the multikills)
           and the new lines to say (--line name=Text[@style], or --lines a JSON
           list of {"name", "text", "style"}):
             python3 tools/halo_voice.py prepare --extracted extracted --out job \\
                 --line "overtime=Overtime!@hype"
  check    validates a job, and once there are takes, each take's length,
           clipping and level against the clips'
  (generate: Qwen3-TTS voice cloning in a container, tools/halo_voice/
           generate_halo.py, several takes per line, each seeded from its
           name; see that file)
  master   each take trimmed, resampled to 22050 Hz, matched to the clips'
           tone (--eq, a smoothed long-term spectrum match), to their median
           speaking level (the RMS of the 50 ms blocks louder than a tenth of
           the loudest, as the Blitz roster audio measures) and to their
           stereo width (--stereo: the clips are stereo, with reverb); --only
           masters some lines (the tone is still matched from every take),
           --gain-db trims their level
  qa       Whisper (faster-whisper, CPU) transcribes the clips and the
           mastered takes: each take's character and word error rates against
           its text, and a speaker embedding (Resemblyzer) how alike its voice
           is to the clips'; picks the best take per line (selection.json) and
           writes qa/index.html, an A/B listening page of clips and takes;
           --only checks some lines, keeping the others' results
  install  the picked takes (or --take name=N) into <data>/voice, with
           voice.json, where the game loads them (data: the folder holding
           maps/)
  locate   finds clips in a recording of the game's mix (debug.audio_capture):
           where each plays, how alike (correlation) and at what gain, so a
           new line can be checked against the announcer's own in the game:
             HALO_VOICE_LINE=overtime HALO_AUDIO_CAPTURE=capture.wav ./halo
             python3 tools/halo_voice.py locate --capture capture.wav \\
                 voice/overtime.wav extracted/sound/dialog/multiplayer1/slayer.wav
           (a local Slayer game on Blood Gulch, with no menus: init.txt beside
           the game holding "game_variant slayer" and
           "map_name levels\\test\\bloodgulch\\bloodgulch")

master, qa and locate need NumPy, SciPy and soundfile (qa also
faster-whisper and Resemblyzer): the container tools/halo_voice/Dockerfile
has them. The rest need only Python.
"""

import argparse
import array
import hashlib
import html
import json
import math
import re
import shutil
import struct
import sys
import wave
import zlib
from pathlib import Path


EXTRACT_SCHEMA = "halo-voice-extract-v1"
JOB_SCHEMA = "halo-voice-job-v1"
INSTALL_SCHEMA = "halo-voice-lines-v1"
DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
ANNOUNCER_PREFIX = "sound\\dialog\\multiplayer1\\"
RATE = 22050
REFERENCE_SECONDS = 7.5
REFERENCE_GAP_SECONDS = 0.15
XBOX_TAG_BASE = 0x803A6000
ADPCM_BLOCK_BYTES = 36
ADPCM_BLOCK_SAMPLES = 64

# What the announcer says in each clip: its tag's name, spelled out (checked
# with Whisper: qa transcribes the clips too)
TRANSCRIPTS = {
    "play_ball": "Play ball!",
    "game_over": "Game over.",
    "one_minute_to_win": "One minute to win.",
    "30_seconds_to_win": "Thirty seconds to win.",
    "red_team_minute_to_win": "Red team, one minute to win.",
    "red_team_30_to_win": "Red team, thirty seconds to win.",
    "blue_team_minute_to_win": "Blue team, one minute to win.",
    "blue_team_30_to_win": "Blue team, thirty seconds to win.",
    "blue_team_has_the_flag": "Blue team has the flag.",
    "blue_team_flag_returned": "Blue team, flag returned.",
    "blue_team_score": "Blue team, score.",
    "red_team_has_the_flag": "Red team has the flag.",
    "red_team_flag_returned": "Red team, flag returned.",
    "red_team_score": "Red team, score.",
    "double_kill": "Double kill!",
    "triple_kill": "Triple kill!",
    "killtacular": "Killtacular!",
    "running_riot": "Running riot!",
    "killing_spree": "Killing spree!",
    "oddball": "Oddball.",
    "race": "Race.",
    "slayer": "Slayer.",
    "capture_the_flag": "Capture the flag.",
    "warthog": "Warthog.",
    "ghost": "Ghost.",
    "scorpion": "Scorpion.",
    "team_king_of_the_hill": "Team king of the hill.",
    "team_oddball": "Team oddball.",
    "team_race": "Team race.",
    "team_slayer": "Team slayer.",
    "king_of_the_hill": "King of the hill.",
    "blue_team_ctf": "Blue team, CTF.",
    "red_team_ctf": "Red team, CTF.",
    "hill_contested": "Hill contested.",
    "hill_controlled": "Hill controlled.",
    "hill_occupied": "Hill occupied.",
}

# The reference montages: delivery styles, from the clips that have them
STYLES = {
    "calm": ["slayer", "team_slayer", "capture_the_flag", "king_of_the_hill", "oddball",
             "team_race", "game_over", "race", "team_oddball"],
    "hype": ["double_kill", "triple_kill", "killtacular", "running_riot", "killing_spree", "play_ball"],
}


class VoiceError(Exception):
    pass


# ---------- the map

class XboxMap:
    """An Xbox cache file's tags (its body after the 2 KB header is zlib
    compressed on the disc; tools/hud_assets.py reads it the same way)."""

    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.data = raw[:0x800] + zlib.decompressobj().decompress(raw[0x800:])
        offset, size = struct.unpack_from("<ii", self.data, 0x10)
        self.tags_data = self.data[offset:offset + size]
        instances, = struct.unpack_from("<I", self.tags_data, 0)
        count, = struct.unpack_from("<I", self.tags_data, 0xC)
        self.tags = {}
        for index in range(count):
            group, _, _, _, name, address, _, _ = struct.unpack("<4sIIIIIII", self.read(instances + index * 0x20, 0x20))
            name = self.read(name, 256).split(b"\0")[0].decode("latin-1")
            self.tags[(group[::-1].decode("latin-1"), name)] = address

    def read(self, address, size):
        offset = address - XBOX_TAG_BASE
        return self.tags_data[offset:offset + size]

    def sound(self, name):
        """A snd! tag's format and its permutations' sample data, each
        permutation's chain of pieces joined (sound_definitions.h)."""
        definition = self.read(self.tags[("snd!", name)], 0xA4)
        flags, sound_class, sample_rate = struct.unpack_from("<Ihh", definition, 0)
        encoding, compression = struct.unpack_from("<hh", definition, 0x6C)
        range_count, ranges = struct.unpack_from("<iI", definition, 0x98)
        out = {"class": sound_class, "rate": 44100 if sample_rate == 1 else 22050,
               "channels": 2 if encoding == 1 else 1, "compression": compression, "permutations": []}
        for range_index in range(range_count):
            pitch_range = self.read(ranges + range_index * 0x48, 0x48)
            actual, = struct.unpack_from("<h", pitch_range, 0x2C)
            count, permutations = struct.unpack_from("<iI", pitch_range, 0x3C)
            pieces = []
            for index in range(count):
                permutation = self.read(permutations + index * 0x7C, 0x7C)
                next_index, = struct.unpack_from("<h", permutation, 0x2A)
                size, _, file_offset = struct.unpack_from("<iIi", permutation, 0x40)
                pieces.append((next_index, self.data[file_offset:file_offset + size]))
            for index in range(min(actual, count)):
                data, seen = b"", set()
                while index != -1 and index not in seen and index < count:
                    seen.add(index)
                    data += pieces[index][1]
                    index = pieces[index][0]
                out["permutations"].append(data)
        return out


# ---------- Xbox ADPCM (port/linux/src/dsound_sdl.c's decode_adpcm)

IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
IMA_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
]


def decode_adpcm(data, channels):
    """Xbox ADPCM blocks (per channel a 4-byte header, then 4-byte groups of
    eight nibbles alternating between the channels) as interleaved 16-bit
    samples."""
    block_bytes = ADPCM_BLOCK_BYTES * channels
    blocks = len(data) // block_bytes
    out = array.array("h", bytes(blocks * ADPCM_BLOCK_SAMPLES * channels * 2))
    for block in range(blocks):
        base = block * block_bytes
        first = block * ADPCM_BLOCK_SAMPLES * channels
        for channel in range(channels):
            header = base + channel * 4
            predictor = struct.unpack_from("<h", data, header)[0]
            index = min(data[header + 2], 88)
            for group in range(8):
                nibbles = base + 4 * channels + (group * channels + channel) * 4
                for byte in range(4):
                    value = data[nibbles + byte]
                    for half, nibble in enumerate((value & 0xF, value >> 4)):
                        step = IMA_STEP[index]
                        difference = step >> 3
                        if nibble & 1:
                            difference += step >> 2
                        if nibble & 2:
                            difference += step >> 1
                        if nibble & 4:
                            difference += step
                        if nibble & 8:
                            difference = -difference
                        predictor = max(-32768, min(32767, predictor + difference))
                        index = max(0, min(88, index + IMA_INDEX[nibble]))
                        sample = group * 8 + byte * 2 + half
                        out[first + sample * channels + channel] = predictor
    return out


# ---------- WAVs and levels

def read_wav(path):
    """(rate, channels, interleaved 16-bit samples) of a PCM WAV."""
    try:
        with wave.open(str(path), "rb") as f:
            if f.getsampwidth() != 2:
                raise VoiceError(f"{path}: not 16-bit PCM")
            samples = array.array("h", f.readframes(f.getnframes()))
            if sys.byteorder == "big":
                samples.byteswap()
            return f.getframerate(), f.getnchannels(), samples
    except (wave.Error, OSError, EOFError) as e:
        raise VoiceError(f"cannot read {path}: {e}") from e


def write_wav(path, rate, channels, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = array.array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(data.tobytes())


def mono(channels, samples):
    if channels == 1:
        return list(samples)
    return [sum(samples[i:i + channels]) / channels for i in range(0, len(samples), channels)]


def speech_rms(values, rate):
    """RMS over the 50 ms blocks whose mean square is at least a hundredth of
    the loudest block's (an RMS within 20 dB of it): the words, not the gaps.
    Full scale is 1.0 (the Blitz roster audio's measure())."""
    block = max(1, rate // 20)
    powers = []
    for start in range(0, len(values) - block + 1, block):
        chunk = values[start:start + block]
        powers.append(sum(v * v for v in chunk) / len(chunk) / (32768.0 * 32768.0))
    if not powers:
        return 0.0
    top = max(powers)
    loud = [p for p in powers if p >= top / 100]
    return math.sqrt(sum(loud) / len(loud)) if loud and top > 0 else 0.0


def trim_bounds(values, rate, floor_db=-40.0, pad_seconds=0.03):
    """The first and last audible samples (above floor_db of the peak), padded."""
    peak = max((abs(v) for v in values), default=0)
    if peak <= 0:
        return 0, len(values)
    threshold = peak * 10 ** (floor_db / 20)
    audible = [i for i, v in enumerate(values) if abs(v) >= threshold]
    pad = round(rate * pad_seconds)
    return max(0, audible[0] - pad), min(len(values), audible[-1] + pad + 1)


def dbfs(value):
    return 20 * math.log10(value) if value > 0 else -math.inf


# ---------- extract

def announcer_name(tag):
    return tag[len(ANNOUNCER_PREFIX):]


def extract(args):
    game_map = XboxMap(args.map)
    out = Path(args.out)
    names = sorted(name for group, name in game_map.tags if group == "snd!" and name.startswith(ANNOUNCER_PREFIX))
    if not names:
        raise VoiceError(f"{args.map} has no {ANNOUNCER_PREFIX} sounds")
    lines = []
    for tag in names:
        sound = game_map.sound(tag)
        if sound["compression"] != 1:
            print(f"skip {tag}: compression {sound['compression']} is not Xbox ADPCM")
            continue
        short = announcer_name(tag)
        for index, data in enumerate(sound["permutations"]):
            samples = decode_adpcm(data, sound["channels"])
            relative = Path(*tag.split("\\")).with_suffix("" if index == 0 else f".{index}")
            relative = relative.with_name(relative.name + ".wav")
            write_wav(out / relative, sound["rate"], sound["channels"], samples)
            values = mono(sound["channels"], samples)
            frames = len(samples) // sound["channels"]
            same = sound["channels"] == 2 and all(samples[i] == samples[i + 1] for i in range(0, len(samples), 2))
            text = TRANSCRIPTS.get(short)
            if text is None:
                text = short.replace("_", " ").capitalize() + "."
                print(f"note: no transcript for {short}; using {text!r} (check it)")
            lines.append({
                "tag": tag, "name": short if index == 0 else f"{short}.{index}", "file": str(relative),
                "text": text, "rate": sound["rate"], "channels": sound["channels"],
                "seconds": round(frames / sound["rate"], 3), "identical_channels": same,
                "speech_rms": round(speech_rms(values, sound["rate"]), 5),
                "peak": round(max((abs(v) for v in values), default=0) / 32768, 4),
            })
            print(f"{tag}: {frames / sound['rate']:.2f} s, {sound['channels']} channels,"
                  f" speech {dbfs(lines[-1]['speech_rms']):.1f} dBFS")
    levels = sorted(line["speech_rms"] for line in lines)
    manifest = {"schema": EXTRACT_SCHEMA, "map": Path(args.map).name, "lines": lines,
                "median_speech_rms": levels[len(levels) // 2]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{len(lines)} clips -> {out}; median speech level {dbfs(manifest['median_speech_rms']):.1f} dBFS")


def load_extracted(path):
    path = Path(path)
    try:
        doc = json.loads((path / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise VoiceError(f"cannot read {path / 'manifest.json'}: {e}") from e
    if doc.get("schema") != EXTRACT_SCHEMA:
        raise VoiceError(f"{path} is not an extract ({EXTRACT_SCHEMA})")
    return doc


# ---------- prepare

def write_reference(extracted, lines, names, output):
    by_name = {line["name"]: line for line in lines}
    pcm, texts, sources, seconds = [], [], [], 0.0
    gap = [0] * round(RATE * REFERENCE_GAP_SECONDS)
    for name in names:
        line = by_name.get(name)
        if not line or line["rate"] != RATE:
            continue
        rate, channels, samples = read_wav(extracted / line["file"])
        values = mono(channels, samples)
        start, end = trim_bounds(values, rate)
        clip = [int(round(v)) for v in values[start:end]]
        extra = len(clip) / RATE + (REFERENCE_GAP_SECONDS if pcm else 0)
        if pcm and seconds + extra > REFERENCE_SECONDS:
            continue
        if pcm:
            pcm += gap
        pcm += clip
        seconds += extra
        texts.append(line["text"])
        sources.append(line["tag"])
    if not pcm:
        raise VoiceError(f"no clips for the {output.stem} reference")
    write_wav(output, RATE, 1, pcm)
    return {"audio": str(Path("references") / output.name), "text": " ".join(texts),
            "seconds": round(len(pcm) / RATE, 3), "sources": sources}


LINE_NAME = re.compile(r"^[a-z0-9_]{1,31}$")


def parse_lines(args):
    lines = []
    if args.lines:
        try:
            for item in json.loads(Path(args.lines).read_text()):
                lines.append({"name": item["name"], "text": item["text"], "style": item.get("style", "calm")})
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            raise VoiceError(f"cannot read {args.lines}: {e}") from e
    for spec in args.line or []:
        name, sep, rest = spec.partition("=")
        if not sep:
            raise VoiceError(f"--line {spec!r}: expected name=Text[@style]")
        text, _, style = rest.rpartition("@") if "@" in rest else (rest, "", "calm")
        lines.append({"name": name, "text": text, "style": style or "calm"})
    if not lines:
        raise VoiceError("no lines: give --line name=Text[@style] or --lines file.json")
    return lines


def prepare(args):
    extracted = Path(args.extracted)
    doc = load_extracted(extracted)
    lines = parse_lines(args)
    for line in lines:
        if not LINE_NAME.match(line["name"]):
            raise VoiceError(f"line name {line['name']!r}: lower case letters, digits and _, up to 31")
        if line["style"] not in STYLES:
            raise VoiceError(f"line {line['name']}: style {line['style']!r} is not one of {', '.join(STYLES)}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    refs = {style: write_reference(extracted, doc["lines"], names, out / "references" / f"{style}.wav")
            for style, names in STYLES.items()}
    manifest = {
        "schema": JOB_SCHEMA,
        "model": args.model,
        "language": "English",
        "takes": args.takes,
        "references": refs,
        "target_speech_rms": doc["median_speech_rms"],
        "rate": RATE,
        "originals": str(extracted.resolve()),
        "lines": [dict(line, output=line["name"]) for line in lines],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "outputs").mkdir(exist_ok=True)
    print(f"{len(lines)} lines x {args.takes} takes -> {out / 'manifest.json'}")
    for style, ref in refs.items():
        print(f"  {style}: {ref['seconds']:.2f} s reference, {len(ref['sources'])} clips: {ref['text']}")


def load_job(job):
    path = Path(job) / "manifest.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise VoiceError(f"cannot read {path}: {e}") from e
    if doc.get("schema") != JOB_SCHEMA or not isinstance(doc.get("lines"), list):
        raise VoiceError(f"{path} is not a {JOB_SCHEMA} manifest")
    return doc


def takes_of(job, folder, line):
    return sorted((Path(job) / folder / line["output"]).glob("take*.wav"))


# ---------- check

def check(args):
    job = Path(args.job)
    manifest = load_job(job)
    problems, notes = [], []
    for style, ref in manifest["references"].items():
        if not (job / ref["audio"]).is_file():
            problems.append(f"missing {style} reference {ref['audio']}")
        elif not 3.0 <= ref["seconds"] <= 10.0:
            notes.append(f"{style} reference is {ref['seconds']:.1f} s (3-10 s clones best)")
    seen = set()
    target = manifest["target_speech_rms"]
    for line in manifest["lines"]:
        if line["name"] in seen:
            problems.append(f"line {line['name']} twice")
        seen.add(line["name"])
        if line.get("style") not in manifest["references"]:
            problems.append(f"line {line['name']}: no {line.get('style')} reference")
        for folder in ("outputs", "mastered"):
            for path in takes_of(job, folder, line):
                rate, channels, samples = read_wav(path)
                values = mono(channels, samples)
                seconds = len(values) / rate
                peak = max((abs(v) for v in samples), default=0)
                level = speech_rms(values, rate)
                clipped = sum(1 for v in samples if abs(v) >= 32767)
                words = len(line["text"].split())
                status = []
                if not 0.25 <= seconds <= 1.2 + 0.6 * words:
                    status.append(f"length {seconds:.2f} s")
                if clipped and folder == "outputs":
                    # (master levels and limits the take, so only a note)
                    notes.append(f"{path.relative_to(job)}: {clipped} clipped samples from the model")
                elif clipped:
                    status.append(f"{clipped} clipped samples")
                if folder == "mastered":
                    if rate != RATE or channels != 2:
                        status.append(f"{rate} Hz {channels} channels")
                    if abs(dbfs(level) - dbfs(target)) > 1.0:
                        status.append(f"level {dbfs(level):.1f} dBFS, target {dbfs(target):.1f}")
                rel = path.relative_to(job)
                summary = (f"{rel}: {seconds:.2f} s, peak {dbfs(peak / 32768):.1f} dBFS,"
                           f" speech {dbfs(level):.1f} dBFS")
                if status:
                    problems.append(summary + " -- " + "; ".join(status))
                elif args.verbose:
                    print(summary)
    for note in notes:
        print(f"note: {note}")
    for problem in problems:
        print(f"problem: {problem}")
    print(f"{len(manifest['lines'])} lines, {len(problems)} problem(s)")
    return 1 if problems else 0


# ---------- master (NumPy, SciPy, soundfile)

def ltas(values, rate, size=2048):
    """Long-term average power spectrum of the speech (frames within 30 dB of
    the loudest)."""
    import numpy as np
    window = np.hanning(size)
    frames = [values[i:i + size] * window for i in range(0, len(values) - size, size // 2)]
    if not frames:
        return None, 0
    spectra = np.abs(np.fft.rfft(np.array(frames), axis=1)) ** 2
    energy = spectra.sum(axis=1)
    keep = energy >= energy.max() / 1000
    return spectra[keep].sum(axis=0), int(keep.sum())


def band_smooth(freqs, power, fraction=3):
    """Power in 1/fraction-octave bands around each bin, in dB."""
    import numpy as np
    out = np.empty_like(power)
    for i, f in enumerate(freqs):
        if f <= 0:
            out[i] = power[i]
            continue
        lo, hi = f * 2 ** (-0.5 / fraction), f * 2 ** (0.5 / fraction)
        sel = (freqs >= lo) & (freqs <= hi)
        out[i] = power[sel].mean()
    return 10 * np.log10(out + 1e-20)


def eq_filter(original_power, generated_power, rate, limit_db=9.0, taps=511):
    """A linear-phase FIR taking the generated takes' long-term spectrum to the
    clips' (smoothed in third octaves, 100 Hz to 9 kHz, at most +-limit_db,
    levels compared at their 1 kHz-4 kHz average)."""
    import numpy as np
    from scipy.signal import firwin2
    freqs = np.fft.rfftfreq(2 * (len(original_power) - 1), 1 / rate)
    a = band_smooth(freqs, original_power)
    b = band_smooth(freqs, generated_power)
    mid = (freqs >= 1000) & (freqs <= 4000)
    diff = (a - a[mid].mean()) - (b - b[mid].mean())
    diff = np.clip(diff, -limit_db, limit_db)
    edge_lo, edge_hi = np.searchsorted(freqs, 100), np.searchsorted(freqs, 9000)
    diff[:edge_lo] = diff[edge_lo]
    diff[edge_hi:] = diff[edge_hi - 1] if edge_hi < len(diff) else diff[-1]
    gains = 10 ** (diff / 20)
    nyquist = rate / 2
    return firwin2(taps, freqs / nyquist, gains), diff, freqs


def master(args):
    import numpy as np
    import soundfile as sf
    from scipy.signal import fftconvolve, lfilter, resample_poly

    job = Path(args.job)
    manifest = load_job(job)
    originals = Path(args.originals or manifest["originals"])
    extract_doc = load_extracted(originals)
    target = manifest["target_speech_rms"]

    def load(path):
        data, rate = sf.read(str(path), dtype="float64", always_2d=True)
        return data.mean(axis=1), rate

    only = set(args.only.split(",")) if args.only else None
    takes = []
    for line in manifest["lines"]:
        for path in takes_of(job, "outputs", line):
            values, rate = load(path)
            if rate != RATE:
                g = math.gcd(RATE, rate)
                values = resample_poly(values, RATE // g, rate // g)
            start, end = trim_bounds(values.tolist(), RATE)
            takes.append((line, path, values[start:end]))
    if not takes:
        raise VoiceError(f"no takes in {job / 'outputs'}; run the generator first")

    eq = None
    report = {"target_speech_rms": target, "eq": None, "takes": {}}
    report_path = job / "mastered" / "master.json"
    if only is not None and report_path.is_file():
        try:
            report["takes"] = json.loads(report_path.read_text()).get("takes", {})
        except json.JSONDecodeError:
            pass
    if args.eq:
        original_power = None
        for clip in extract_doc["lines"]:
            values, rate = load(originals / clip["file"])
            if rate != RATE:
                continue
            power, _ = ltas(values, rate)
            if power is not None:
                original_power = power if original_power is None else original_power + power
        generated_power = None
        for _line, _path, values in takes:
            power, _ = ltas(values, RATE)
            if power is not None:
                generated_power = power if generated_power is None else generated_power + power
        eq, diff, freqs = eq_filter(original_power, generated_power, RATE, args.eq_limit)
        bands = [125, 250, 500, 1000, 2000, 4000, 8000]
        report["eq"] = {str(f): round(float(diff[np.searchsorted(freqs, f)]), 1) for f in bands}
        print("EQ (dB at " + ", ".join(f"{f} Hz {report['eq'][str(f)]:+.1f}" for f in bands) + ")")

    # the clips' stereo: a reverberant side, their side-to-mid energy ratio
    # (median); a take's side is its mid through a short decaying noise burst
    side_ir, side_ratio = None, 0.0
    if args.stereo:
        ratios = []
        for clip in extract_doc["lines"]:
            data, _rate = sf.read(str(originals / clip["file"]), dtype="float64", always_2d=True)
            if data.shape[1] == 2:
                m, d = (data[:, 0] + data[:, 1]) / 2, (data[:, 0] - data[:, 1]) / 2
                if np.sum(m ** 2) > 0:
                    ratios.append(float(np.sum(d ** 2) / np.sum(m ** 2)))
        if ratios:
            side_ratio = sorted(ratios)[len(ratios) // 2]
            rng = np.random.default_rng(2001)
            t = np.arange(round(RATE * 0.15)) / RATE
            side_ir = rng.standard_normal(len(t)) * np.exp(-t / 0.045)
            side_ir[:round(RATE * 0.004)] = 0.0
            side_ir /= math.sqrt(float(np.sum(side_ir ** 2)))
            report["side_to_mid_db"] = round(10 * math.log10(side_ratio), 1)
            print(f"stereo: side {10 * math.log10(side_ratio):.1f} dB under mid, as the clips' median")

    trim = 10 ** (args.gain_db / 20)
    for line, path, values in takes:
        if only is not None and line["name"] not in only:
            continue
        if eq is not None:
            delay = (len(eq) - 1) // 2
            padded = np.concatenate([values, np.zeros(delay)])
            values = lfilter(eq, [1.0], padded)[delay:]
        level = speech_rms((values * 32768).tolist(), RATE)
        gain = (target / level if level > 0 else 1.0) * trim
        mid = values * gain
        side = np.zeros_like(mid)
        if side_ir is not None and len(mid):
            side = fftconvolve(mid, side_ir)[:len(mid)]
            energy = float(np.sum(side ** 2))
            if energy > 0:
                side *= math.sqrt(side_ratio * float(np.sum(mid ** 2)) / energy)
        stereo = np.stack([mid + side, mid - side], axis=1)
        peak = float(np.max(np.abs(stereo))) if len(stereo) else 0.0
        limited = 0
        if peak > args.ceiling:
            # a soft knee over the ceiling rather than clipping
            knee = args.ceiling * 0.9
            over = np.abs(stereo) > knee
            limited = int(over.sum())
            excess = np.abs(stereo[over]) - knee
            stereo[over] = np.sign(stereo[over]) * (knee + (args.ceiling - knee) * np.tanh(excess / (args.ceiling - knee)))
        fade = min(len(stereo) // 4, round(RATE * 0.01))
        if fade:
            ramp = np.linspace(0.0, 1.0, fade)[:, None]
            stereo[:fade] *= ramp
            stereo[-fade:] *= ramp[::-1]
        pcm = np.clip(np.round(stereo * 32767), -32768, 32767).astype(np.int16)
        out = job / "mastered" / line["output"] / path.name
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), pcm, RATE, subtype="PCM_16")
        report["takes"][str(out.relative_to(job))] = {
            "seconds": round(len(pcm) / RATE, 3), "gain_db": round(dbfs(gain), 2),
            "peak_dbfs": round(dbfs(float(np.max(np.abs(pcm))) / 32768), 2) if len(pcm) else None,
            "limited_samples": limited, "eq": eq is not None, "stereo": side_ir is not None,
            "trim_db": args.gain_db}
        print(f"{out.relative_to(job)}: {len(pcm) / RATE:.2f} s, gain {dbfs(gain):+.1f} dB"
              + (f", {limited} samples limited" if limited else ""))
    report_path.write_text(json.dumps(report, indent=2) + "\n")


# ---------- qa (faster-whisper)

NUMBER_WORDS = {"1": "one", "2": "two", "3": "three", "5": "five", "10": "ten", "30": "thirty", "60": "sixty"}


def normalize(text):
    words = re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()
    return [NUMBER_WORDS.get(w, w) for w in words]


def edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        previous, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (x != y))
    return row[-1]


def error_rates(expected, heard):
    want, got = normalize(expected), normalize(heard)
    wer = edit_distance(want, got) / max(1, len(want))
    want_c, got_c = "".join(want), "".join(got)
    cer = edit_distance(want_c, got_c) / max(1, len(want_c))
    return round(wer, 3), round(cer, 3)


def qa(args):
    import numpy as np
    import soundfile as sf
    from faster_whisper import WhisperModel
    from scipy.signal import resample_poly

    job = Path(args.job)
    manifest = load_job(job)
    originals = Path(args.originals or manifest["originals"])
    extract_doc = load_extracted(originals)
    model = WhisperModel(args.whisper, device="cpu", compute_type="int8", download_root=args.models)
    only = set(args.only.split(",")) if args.only else None
    previous = {}
    if only is not None and (job / "qa" / "qa.json").is_file():
        try:
            previous = json.loads((job / "qa" / "qa.json").read_text())
        except json.JSONDecodeError:
            previous = {}

    def transcribe(path, prompt):
        # (16 kHz mono, as Whisper hears; read here rather than by PyAV)
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
        g = math.gcd(16000, rate)
        audio = resample_poly(data.mean(axis=1), 16000 // g, rate // g).astype(np.float32)
        segments, _info = model.transcribe(audio, language="en", beam_size=5, initial_prompt=prompt,
                                           vad_filter=False, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()

    # how alike a voice is to the announcer's: the cosine of its speaker
    # embedding (Resemblyzer, GE2E) and the clips' mean one; for scale, each
    # clip's own against the mean of the others
    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
        encoder = VoiceEncoder("cpu", verbose=False)
    except ImportError:
        encoder = None

    def embedding(path):
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
        return encoder.embed_utterance(preprocess_wav(data.mean(axis=1), source_sr=rate))

    voice, clip_embeddings = None, {}
    if encoder is not None:
        clip_embeddings = {clip["name"]: embedding(originals / clip["file"]) for clip in extract_doc["lines"]}
        voice = np.mean(list(clip_embeddings.values()), axis=0)
        voice /= np.linalg.norm(voice)

    def similarity(vector, without=None):
        reference = voice
        if without is not None:
            others = [v for name, v in clip_embeddings.items() if name != without]
            reference = np.mean(others, axis=0)
            reference /= np.linalg.norm(reference)
        return round(float(np.dot(vector, reference) / np.linalg.norm(vector)), 3)

    # (the game's words, so the model spells them as the game does)
    prompt = "Halo announcer: Killtacular. Running riot. Slayer. Oddball. Warthog."
    results = {"whisper": args.whisper, "originals": [], "lines": {}}
    if previous.get("originals") and previous.get("whisper") == args.whisper:
        # (the clips were checked before; their scores don't change)
        results["originals"] = previous["originals"]
        if "original_similarity" in previous:
            results["original_similarity"] = previous["original_similarity"]
    for clip in extract_doc["lines"] if not results["originals"] else []:
        heard = transcribe(originals / clip["file"], prompt)
        wer, cer = error_rates(clip["text"], heard)
        alike = similarity(clip_embeddings[clip["name"]], clip["name"]) if voice is not None else None
        results["originals"].append({"name": clip["name"], "text": clip["text"], "heard": heard, "wer": wer, "cer": cer,
                                     "similarity": alike})
        print(f"original {clip['name']}: {heard!r} (WER {wer:.2f}, CER {cer:.2f}, voice {alike})")
    if voice is not None and "original_similarity" not in results:
        values = sorted(o["similarity"] for o in results["originals"])
        results["original_similarity"] = {"min": values[0], "median": values[len(values) // 2], "max": values[-1]}

    master_doc = {}
    master_path = job / "mastered" / "master.json"
    if master_path.is_file():
        master_doc = json.loads(master_path.read_text()).get("takes", {})
    selection = {}
    if only is not None:
        names = {line["name"] for line in manifest["lines"]}
        results["lines"] = {name: info for name, info in previous.get("lines", {}).items() if name in names}
        selection = {name: info["best"] for name, info in results["lines"].items()}
    for line in manifest["lines"]:
        if only is not None and line["name"] not in only:
            continue
        scored = []
        for path in takes_of(job, "mastered", line):
            heard = transcribe(path, prompt)
            wer, cer = error_rates(line["text"], heard)
            rate, channels, samples = read_wav(path)
            seconds = len(samples) / channels / rate
            info = master_doc.get(str(path.relative_to(job)), {})
            alike = similarity(embedding(path)) if voice is not None else None
            scored.append({"take": path.name, "heard": heard, "wer": wer, "cer": cer, "seconds": round(seconds, 3),
                           "limited_samples": info.get("limited_samples", 0), "similarity": alike})
            print(f"{line['name']} {path.name}: {heard!r} (WER {wer:.2f}, CER {cer:.2f}, voice {alike},"
                  f" {seconds:.2f} s)")
        if not scored:
            continue
        lengths = sorted(t["seconds"] for t in scored)
        median = lengths[len(lengths) // 2]
        # the best: the fewest errors, then the voice most like the clips',
        # then the length nearest the takes' median (not cut short)
        best = min(scored, key=lambda t: (t["cer"], t["wer"], -(t["similarity"] or 0), abs(t["seconds"] - median)))
        results["lines"][line["name"]] = {"text": line["text"], "takes": scored, "best": best["take"]}
        selection[line["name"]] = best["take"]
    (job / "qa").mkdir(exist_ok=True)
    (job / "qa" / "qa.json").write_text(json.dumps(results, indent=2) + "\n")
    (job / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    write_listening_page(job, manifest, extract_doc, originals, results)
    print(f"selection -> {job / 'selection.json'}; listening page -> {job / 'qa' / 'index.html'}")


def write_listening_page(job, manifest, extract_doc, originals, results):
    """qa/index.html: the clips and every take, side by side, with the
    transcripts; the WAVs it plays are copied beside it (qa/audio), so the
    folder can be copied and opened anywhere."""
    audio = job / "qa" / "audio"
    if audio.exists():
        shutil.rmtree(audio)
    (audio / "originals").mkdir(parents=True)
    for clip in extract_doc["lines"]:
        shutil.copy2(originals / clip["file"], audio / "originals" / f"{clip['name']}.wav")
    for line in manifest["lines"]:
        for folder in ("outputs", "mastered"):
            for path in takes_of(job, folder, line):
                dest = audio / folder / line["output"] / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
    for style, ref in manifest["references"].items():
        shutil.copy2(job / ref["audio"], audio / f"reference_{style}.wav")

    e = html.escape

    def fmt(value):
        return "" if value is None else f"{value:.3f}"

    span = results.get("original_similarity")
    voice_note = (f" Voice: the cosine of a take's speaker embedding (Resemblyzer) with the clips' mean; the game's own"
                  f" clips score {span['min']:.3f} to {span['max']:.3f} (median {span['median']:.3f}) against the"
                  f" others'." if span else "")
    rows = []
    for name, info in results["lines"].items():
        line = next(l for l in manifest["lines"] if l["name"] == name)
        rows.append(f"<h2>{e(name)}: &ldquo;{e(info['text'])}&rdquo; <small>({e(line['style'])} reference)</small></h2>")
        rows.append("<table><tr><th>take</th><th>mastered (as installed)</th><th>raw model output</th>"
                    "<th>Whisper heard</th><th>CER</th><th>WER</th><th>voice</th><th>length</th></tr>")
        for take in info["takes"]:
            mark = " class=best" if take["take"] == info["best"] else ""
            rows.append(
                f"<tr{mark}><td>{e(take['take'])}{' (picked)' if mark else ''}</td>"
                f"<td><audio controls preload=none src='audio/mastered/{e(name)}/{e(take['take'])}'></audio></td>"
                f"<td><audio controls preload=none src='audio/outputs/{e(name)}/{e(take['take'])}'></audio></td>"
                f"<td>{e(take['heard'])}</td><td>{take['cer']:.2f}</td><td>{take['wer']:.2f}</td>"
                f"<td>{fmt(take.get('similarity'))}</td><td>{take['seconds']:.2f} s</td></tr>")
        rows.append("</table>")
    refs = "".join(
        f"<p>{e(style)}: <audio controls preload=none src='audio/reference_{e(style)}.wav'></audio> "
        f"<small>{e(ref['text'])}</small></p>" for style, ref in manifest["references"].items())
    originals_rows = "".join(
        f"<tr><td>{e(o['name'])}</td><td><audio controls preload=none src='audio/originals/{e(o['name'])}.wav'></audio></td>"
        f"<td>{e(o['text'])}</td><td>{e(o['heard'])}</td><td>{o['cer']:.2f}</td><td>{fmt(o.get('similarity'))}</td></tr>"
        for o in results["originals"])
    page = f"""<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>Announcer takes</title>
<style>
:root {{ --bg: #fff; --fg: #1b1b1b; --muted: #666; --line: #ddd; --best: #e8f5e9; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161616; --fg: #eee; --muted: #aaa; --line: #333; --best: #1f3322; }} }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.4 system-ui, sans-serif; margin: 0 auto; max-width: 1200px; padding: 16px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
td, th {{ border-bottom: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: middle; }}
tr.best {{ background: var(--best); }}
small, .note {{ color: var(--muted); }}
audio {{ height: 32px; max-width: 260px; }}
.wrap {{ overflow-x: auto; }}
</style></head><body>
<h1>Announcer takes</h1>
<p class=note>Generated lines (Qwen3-TTS voice clone of the multiplayer announcer), each take mastered to the
clips' level ({dbfs(manifest['target_speech_rms']):.1f} dBFS speech RMS) and 22050 Hz stereo, next to the game's own
clips. Whisper: {e(results['whisper'])}.{e(voice_note)} The picked take (green) has the fewest transcription errors,
then the voice most like the clips'; pick another with <code>halo_voice.py install --take name=N</code>.</p>
<h2>References (what the model imitates)</h2>{refs}
<div class=wrap>{''.join(rows)}</div>
<h2>The game's own clips</h2>
<div class=wrap><table><tr><th>clip</th><th></th><th>transcript</th><th>Whisper heard</th><th>CER</th><th>voice</th></tr>{originals_rows}</table></div>
</body></html>
"""
    (job / "qa" / "index.html").write_text(page)


# ---------- locate (NumPy, SciPy, soundfile)

def locate(args):
    import numpy as np
    import soundfile as sf
    from scipy.signal import fftconvolve, resample_poly

    mix, rate = sf.read(args.capture, dtype="float64", always_2d=True)
    mono_mix = mix.mean(axis=1)
    print(f"{args.capture}: {len(mix) / rate:.1f} s at {rate} Hz, peak {dbfs(float(np.max(np.abs(mix)))):.1f} dBFS")
    for path in args.clips:
        clip, clip_rate = sf.read(path, dtype="float64", always_2d=True)
        g = math.gcd(rate, clip_rate)
        clip = resample_poly(clip.mean(axis=1), rate // g, clip_rate // g)
        if len(clip) > len(mono_mix):
            print(f"{path}: longer than the recording")
            continue
        # normalized cross-correlation: the clip's best match in the mix
        corr = fftconvolve(mono_mix, clip[::-1], mode="valid")
        energy = np.convolve(mono_mix ** 2, np.ones(len(clip)), mode="valid")
        score = corr / np.sqrt(np.maximum(energy, 1e-12) * np.sum(clip ** 2))
        lag = int(np.argmax(score))
        there = mono_mix[lag:lag + len(clip)]
        gain = float(np.dot(there, clip) / np.dot(clip, clip))
        level = speech_rms((there * 32768).tolist(), rate)
        print(f"{path}: at {lag / rate:.3f} s, correlation {score[lag]:.3f}, gain {dbfs(abs(gain)):+.1f} dB,"
              f" speech {dbfs(level):.1f} dBFS in the mix, {len(clip) / rate:.2f} s")


# ---------- install

def install(args):
    job = Path(args.job)
    manifest = load_job(job)
    selection = {}
    if (job / "selection.json").is_file():
        selection = json.loads((job / "selection.json").read_text())
    for spec in args.take or []:
        name, _, take = spec.partition("=")
        selection[name] = take if take.endswith(".wav") else f"take{take}.wav"
    out = Path(args.data).expanduser() / "voice"
    names = set(args.only.split(",")) if args.only else None
    installed = []
    for line in manifest["lines"]:
        if names is not None and line["name"] not in names:
            continue
        takes = takes_of(job, "mastered", line)
        if not takes:
            print(f"skip {line['name']}: not mastered")
            continue
        chosen = job / "mastered" / line["output"] / selection.get(line["name"], takes[0].name)
        if not chosen.is_file():
            raise VoiceError(f"{chosen} does not exist")
        rate, channels, samples = read_wav(chosen)
        if rate != RATE or channels not in (1, 2):
            raise VoiceError(f"{chosen}: {rate} Hz {channels} channels (the game takes {RATE} Hz)")
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chosen, out / f"{line['name']}.wav")
        installed.append({"name": line["name"], "file": f"{line['name']}.wav", "text": line["text"],
                          "take": chosen.name})
        print(f"{line['name']}: {chosen.relative_to(job)} -> {out / (line['name'] + '.wav')}")
    if not installed:
        raise VoiceError("nothing installed")
    index_path = out / "voice.json"
    existing = []
    if index_path.is_file():
        try:
            existing = json.loads(index_path.read_text()).get("lines", [])
        except json.JSONDecodeError:
            existing = []
    new_names = {line["name"] for line in installed}
    lines = [line for line in existing if line.get("name") not in new_names] + installed
    index_path.write_text(json.dumps({"schema": INSTALL_SCHEMA, "lines": lines}, indent=2) + "\n")
    print(f"{len(installed)} line(s) installed; {index_path} lists {len(lines)}")


# ---------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="the announcer's clips from a map, as WAVs with transcripts")
    p.add_argument("--map", required=True, help="a multiplayer map (bloodgulch.map)")
    p.add_argument("--out", required=True)
    p.set_defaults(func=extract)

    p = sub.add_parser("prepare", help="write a synthesis job")
    p.add_argument("--extracted", required=True, help="extract's folder")
    p.add_argument("--out", required=True, help="the job folder")
    p.add_argument("--line", action="append", help="name=Text[@calm|@hype] (repeatable)")
    p.add_argument("--lines", help="a JSON list of {name, text, style}")
    p.add_argument("--takes", type=int, default=4, help="takes (seeds) per line")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.set_defaults(func=prepare)

    p = sub.add_parser("check", help="validate a job and its takes")
    p.add_argument("--job", required=True)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=check)

    p = sub.add_parser("master", help="trim, resample, EQ and level-match the takes (container)")
    p.add_argument("--job", required=True)
    p.add_argument("--originals", help="extract's folder (default: the job's)")
    p.add_argument("--eq", action=argparse.BooleanOptionalAction, default=True,
                   help="match the clips' long-term spectrum (default on)")
    p.add_argument("--eq-limit", type=float, default=9.0, help="most boost or cut, dB")
    p.add_argument("--stereo", action=argparse.BooleanOptionalAction, default=True,
                   help="give the takes the clips' stereo width (default on; off: the same in both channels)")
    p.add_argument("--ceiling", type=float, default=0.97, help="peak ceiling (full scale 1.0)")
    p.add_argument("--only", help="comma-separated line names to master (the EQ still hears every take)")
    p.add_argument("--gain-db", type=float, default=0.0, help="level trim after matching the clips', dB")
    p.set_defaults(func=master)

    p = sub.add_parser("qa", help="Whisper the takes, pick the best, write the listening page (container)")
    p.add_argument("--job", required=True)
    p.add_argument("--originals")
    p.add_argument("--whisper", default="small.en", help="faster-whisper model")
    p.add_argument("--models", default=None, help="where faster-whisper keeps its models")
    p.add_argument("--only", help="comma-separated line names to check (the others' results are kept)")
    p.set_defaults(func=qa)

    p = sub.add_parser("install", help="copy the picked takes into the game's data folder")
    p.add_argument("--job", required=True)
    p.add_argument("--data", required=True, help="the folder holding maps/ (the takes go in its voice/)")
    p.add_argument("--take", action="append", help="name=N or name=takeN.wav: this take instead of the picked one")
    p.add_argument("--only", help="comma-separated line names")
    p.set_defaults(func=install)

    p = sub.add_parser("locate", help="find clips in a recording of the game's mix (container)")
    p.add_argument("--capture", required=True, help="debug.audio_capture's WAV")
    p.add_argument("clips", nargs="+", help="WAVs to find in it")
    p.set_defaults(func=locate)

    args = ap.parse_args()
    try:
        return args.func(args) or 0
    except VoiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
