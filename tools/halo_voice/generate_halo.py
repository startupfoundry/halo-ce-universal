#!/usr/bin/env python3
"""Generates a prepared announcer job (tools/halo_voice.py prepare) with
Qwen3-TTS voice cloning: in-context, from each style's reference montage and
its transcript, no fine-tuning. Each line gets the job's number of takes,
take k seeded from a hash of "<line>.<k>", so a take is the same on every
run and a line's takes don't change when other lines are added.

It runs in a container with CUDA PyTorch and qwen-tts (the Blitz roster
audio image, blitz-roster-audio:qwen3-tts, has them), the job mounted at
/job, this file at /halo:

  docker run --rm --gpus all --ipc host --ulimit memlock=-1 --ulimit stack=67108864 \\
      -v "$JOB:/job" -v "$PWD/tools/halo_voice:/halo:ro" \\
      -v blitz-roster-audio-models:/root/.cache/huggingface \\
      --entrypoint python blitz-roster-audio:qwen3-tts /halo/generate_halo.py

Takes already written are kept (--overwrite replaces them); --limit N
generates only the first N lines, --only some lines by name. --take-numbers
names the takes to make (5,6: more takes after four, each with its own
seed), and --salt gives every take a different seed again ("<line>.<k>.<salt>").
Beside each take, takeN.json records its seed, text, style and model.
"""

import argparse
import hashlib
import json
import os
import random
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel


SCHEMA = "halo-voice-job-v1"


def trim(wav, rate):
    """Trims the model's padding, keeping 20 ms around what is audible."""
    values = np.asarray(wav, dtype=np.float32).reshape(-1)
    if not len(values):
        return values
    peak = float(np.max(np.abs(values)))
    if peak <= 1e-7:
        return values
    threshold = max(peak * 10 ** (-48 / 20), 1e-5)
    audible = np.flatnonzero(np.abs(values) >= threshold)
    if not len(audible):
        return values
    pad = round(rate * 0.020)
    return values[max(0, int(audible[0]) - pad):min(len(values), int(audible[-1]) + pad + 1)]


def seed_of(output, take, salt=""):
    key = f"{output}.{take}" + (f".{salt}" if salt else "")
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", default="/job")
    ap.add_argument("--device", default="cuda:0" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--limit", type=int, help="generate only the first N lines")
    ap.add_argument("--takes", type=int, help="takes per line (default: the job's)")
    ap.add_argument("--only", help="comma-separated line names")
    ap.add_argument("--take-numbers", help="comma-separated take numbers to make (default 1 to --takes)")
    ap.add_argument("--salt", default="", help="seeds from <line>.<take>.<salt>")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    job = Path(args.job)
    manifest = json.loads((job / "manifest.json").read_text())
    if manifest.get("schema") != SCHEMA:
        raise SystemExit(f"manifest is not {SCHEMA}")
    takes = args.takes or manifest.get("takes", 4)
    lines = manifest["lines"][:args.limit] if args.limit else manifest["lines"]
    if args.only:
        names = set(args.only.split(","))
        lines = [line for line in lines if line["name"] in names]
    numbers = ([int(n) for n in args.take_numbers.split(",")] if args.take_numbers
               else list(range(1, takes + 1)))

    dtype = torch.bfloat16 if args.device.startswith("cuda") else torch.float32
    model_name = os.environ.get("QWEN_TTS_MODEL", manifest["model"])
    print(f"loading {model_name} on {args.device}", flush=True)
    model = Qwen3TTSModel.from_pretrained(model_name, device_map=args.device, dtype=dtype)

    prompts = {}
    for style, ref in manifest["references"].items():
        prompts[style] = model.create_voice_clone_prompt(
            ref_audio=str(job / ref["audio"]), ref_text=ref["text"], x_vector_only_mode=False)

    done = 0
    for index, line in enumerate(lines, 1):
        folder = job / "outputs" / line["output"]
        folder.mkdir(parents=True, exist_ok=True)
        for take in numbers:
            path = folder / f"take{take}.wav"
            if path.exists() and not args.overwrite:
                print(f"[{index}/{len(lines)}] keep {line['output']}/{path.name}", flush=True)
                continue
            seed = seed_of(line["output"], take, args.salt)
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            print(f"[{index}/{len(lines)}] {line['output']} take {take} (seed {seed}): {line['text']!r} "
                  f"({line['style']})", flush=True)
            wavs, rate = model.generate_voice_clone(
                text=line["text"], language=manifest.get("language", "English"),
                voice_clone_prompt=prompts[line["style"]], non_streaming_mode=True)
            wav = trim(wavs[0], rate)
            if len(wav) < rate // 10:
                print(f"  empty or too short ({len(wav)} samples); not written", flush=True)
                continue
            sf.write(path, wav, rate, subtype="PCM_16")
            path.with_suffix(".json").write_text(json.dumps({
                "seed": seed, "salt": args.salt, "text": line["text"], "style": line["style"],
                "model": model_name}, indent=2) + "\n")
            done += 1
    print(f"wrote {done} takes in {job / 'outputs'}", flush=True)


if __name__ == "__main__":
    main()
