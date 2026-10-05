"""The studio's worker: one thread taking the queued jobs one at a time (the
GPU is shared with other work, so never two at once), each step a container:

  generate  new takes of a line: Qwen3-TTS in the generator's image
            (generate_halo.py --only <line> --take-numbers <after the last>),
            then master and qa as below
  master    the line's takes again with its mastering options (gain trim, EQ
            match, stereo width) in the tools image (halo_voice.py master
            --only), then qa
  qa        Whisper and the speaker embedding (halo_voice.py qa --only); the
            line's best take becomes its pick if it has none
  ingame    the headless game test (run_test.sh): a local Slayer game on
            Blood Gulch playing the line 10 s in, with only that take in its
            voice folder (mounted over the data folder's), the mix captured;
            halo_voice.py locate finds the line and the announcer's "Slayer"
            in it, and the stretch from just before the one to after the
            other is kept as the test's clip

Each job's output goes to jobs/<id>.log. A job running when the studio
stops is queued again when it starts.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import wave
from pathlib import Path

from store import new_take, now, qa_scores, take_number


GAME_CONTAINER = "halo-voice-game"
INGAME_KEEP = 4
LOCATE = re.compile(r"^(?P<path>\S+): at (?P<at>[\d.]+) s, correlation (?P<corr>-?[\d.]+), gain (?P<gain>[-+][\d.]+|-inf) dB,"
                    r" speech (?P<speech>-?[\d.]+|-inf) dBFS in the mix, (?P<seconds>[\d.]+) s")


class JobCancelled(Exception):
    pass


class JobFailed(Exception):
    pass


class Worker(threading.Thread):
    def __init__(self, store, config):
        super().__init__(name="worker", daemon=True)
        self.store = store
        self.config = config
        self.current = None
        self.process = None
        self.cancelled = False
        self.uid = f"{os.getuid()}:{os.getgid()}"
        with store.lock:
            for job in store.state["jobs"]:
                if job["status"] == "running":
                    if job["kind"] == "ingame":
                        self.stop_game(job)
                    job["status"] = "queued"
                    job["stage"] = None
                    job["note"] = "interrupted by a restart; queued again"
            store.save()

    # ---------- the queue

    def run(self):
        while True:
            with self.store.lock:
                job = next((j for j in self.store.state["jobs"] if j["status"] == "queued"), None)
                if job is None:
                    self.store.changed.wait(5)
                    continue
                job["status"] = "running"
                job["started_at"] = now()
                self.current = job
                self.cancelled = False
                self.store.save()
            status, error = "done", None
            try:
                self.execute(job)
            except JobCancelled:
                status, error = "cancelled", "cancelled"
            except JobFailed as e:
                status, error = "failed", str(e)
            except Exception as e:  # (a bug: keep the worker going)
                status, error = "failed", f"{type(e).__name__}: {e}"
                self.write_log(job, traceback.format_exc())
            if self.cancelled:
                status, error = "cancelled", "cancelled"
            with self.store.lock:
                job["status"] = status
                job["error"] = error
                job["stage"] = None
                job["finished_at"] = now()
                self.current = None
                detail = {"job": job["id"], "kind": job["kind"], "status": status}
                if error:
                    detail["error"] = error
                self.store.log("worker", "job_" + status, line=job.get("line"), **detail)
                self.store.save()

    def cancel(self, job):
        """Stops a running job (its container)."""
        if self.current is not job:
            return
        self.cancelled = True
        for name in job.get("containers", []):
            subprocess.run(["docker", "kill", name], capture_output=True)
        if job["kind"] == "ingame":
            self.stop_game(job)
        if self.process is not None:
            try:
                self.process.terminate()
            except OSError:
                pass

    # ---------- running things

    def log_path(self, job):
        return self.store.root / "jobs" / f"{job['id']}.log"

    def write_log(self, job, text):
        with open(self.log_path(job), "a") as f:
            f.write(text if text.endswith("\n") else text + "\n")

    def stage(self, job, stage):
        if self.cancelled:
            raise JobCancelled()
        with self.store.lock:
            job["stage"] = stage
            self.store.save()
        self.write_log(job, f"\n== {stage} ({now()})")

    def run_command(self, job, command, name=None):
        if self.cancelled:
            raise JobCancelled()
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            with self.store.lock:
                job.setdefault("containers", []).append(name)
        self.write_log(job, "$ " + " ".join(command))
        with open(self.log_path(job), "a") as log:
            self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            code = self.process.wait()
            self.process = None
        if self.cancelled:
            raise JobCancelled()
        if code != 0:
            raise JobFailed(f"{Path(command[0]).name} {command[1] if len(command) > 1 else ''} exited with {code}"
                            f" (see the log)")

    def tools(self, job, step, *args, mounts=()):
        c = self.config
        name = f"halo-voice-studio-{job['id']}-{step}"
        command = ["docker", "run", "--rm", "--name", name, "-u", self.uid, "-e", "HOME=/tmp",
                   "-v", f"{c.tools}:/tools:ro", "-v", f"{self.store.job_dir}:/job",
                   "-v", f"{c.extracted}:/extracted:ro", "-v", f"{c.models_volume}:/models"]
        for mount in mounts:
            command += ["-v", mount]
        command += [c.tools_image, "/tools/halo_voice.py", *args]
        self.run_command(job, command, name)

    def execute(self, job):
        kind = job["kind"]
        self.write_log(job, f"job {job['id']}: {kind} {job.get('line') or ''} {json.dumps(job.get('params', {}))}"
                            f" (by {job['by']}, {now()})")
        if kind == "generate":
            self.generate(job)
            self.master(job)
            self.qa(job)
        elif kind == "master":
            self.master(job)
            self.qa(job)
        elif kind == "qa":
            self.qa(job)
        elif kind == "ingame":
            self.ingame(job)
        else:
            raise JobFailed(f"unknown job kind {kind!r}")

    def live_line(self, job):
        with self.store.lock:
            line = self.store.state["lines"].get(job["line"])
            if line is None or line["archived"]:
                raise JobFailed(f"line {job['line']} is gone or archived")
            return line

    # ---------- generate

    def generate(self, job):
        c = self.config
        line = self.live_line(job)
        params = job.get("params", {})
        with self.store.lock:
            self.store.write_manifest()
            folder = self.store.job_dir / "outputs" / line["name"]
            on_disk = [take_number(p.name) for p in folder.glob("take*.wav")] if folder.is_dir() else []
            known = [int(n) for n in line["takes"]] + [n for n in on_disk if n]
            numbers = params.get("numbers")
            if not numbers:
                first = max(known, default=0) + 1
                numbers = list(range(first, first + int(params.get("count", 2))))
                params["numbers"] = numbers
                self.store.save()
        self.stage(job, f"generate takes {', '.join(map(str, numbers))}")
        name = f"halo-voice-studio-{job['id']}-generate"
        args = ["--only", line["name"], "--take-numbers", ",".join(map(str, numbers))]
        if params.get("salt"):
            args += ["--salt", str(params["salt"])]
        script = ('python /halo/generate_halo.py "$@"; status=$?; '
                  f'chown -R {self.uid} /job/outputs; exit $status')
        command = ["docker", "run", "--rm", "--name", name, "--gpus", "all", "--ipc", "host",
                   "--ulimit", "memlock=-1", "--ulimit", "stack=67108864",
                   "-v", f"{self.store.job_dir}:/job", "-v", f"{c.tools / 'halo_voice'}:/halo:ro",
                   "-v", f"{c.gen_volume}:/root/.cache/huggingface",
                   "--entrypoint", "sh", c.gen_image, "-c", script, "generate", *args]
        self.run_command(job, command, name)
        made = self.register_takes(line["name"], job)
        missing = [n for n in numbers if n not in made]
        if not made:
            raise JobFailed("the generator wrote no takes (see the log)")
        if missing:
            self.write_log(job, f"note: takes {missing} were not written (too short?)")

    def register_takes(self, name, job=None):
        """Takes on disk that the state doesn't have yet (with the seed and
        text their sidecar records)."""
        made = []
        with self.store.lock:
            line = self.store.state["lines"][name]
            for path in sorted((self.store.job_dir / "outputs" / name).glob("take*.wav")):
                number = take_number(path.name)
                if number is None or str(number) in line["takes"]:
                    continue
                info = {}
                if path.with_suffix(".json").is_file():
                    try:
                        info = json.loads(path.with_suffix(".json").read_text())
                    except json.JSONDecodeError:
                        pass
                line["takes"][str(number)] = new_take(
                    number, info.get("seed"), info.get("salt", ""), info.get("text", line["text"]),
                    info.get("style", line["style"]), now(), job["id"] if job else None)
                made.append(number)
            if made:
                self.store.log("worker", "takes_added", line=name, takes=made, job=job["id"] if job else None)
                self.store.save()
        return made

    # ---------- master and qa

    def master(self, job):
        line = self.live_line(job)
        options = dict(line["mastering"])
        self.stage(job, "master")
        with self.store.lock:
            self.store.write_manifest()
        self.register_takes(line["name"], job)
        args = ["master", "--job", "/job", "--originals", "/extracted", "--only", line["name"],
                "--gain-db", str(float(options.get("gain_db", 0.0))),
                "--eq" if options.get("eq", True) else "--no-eq",
                "--stereo" if options.get("stereo", True) else "--no-stereo"]
        self.tools(job, "master", *args)
        report = json.loads((self.store.job_dir / "mastered" / "master.json").read_text())
        with self.store.lock:
            line = self.store.state["lines"][line["name"]]
            for number, take in line["takes"].items():
                info = report.get("takes", {}).get(f"mastered/{line['name']}/take{number}.wav")
                if info:
                    take["master"] = dict(info, at=now())
            self.store.save()

    def qa(self, job):
        line = self.live_line(job)
        self.stage(job, "qa")
        with self.store.lock:
            self.store.write_manifest()
        self.tools(job, "qa", "qa", "--job", "/job", "--originals", "/extracted", "--models", "/models",
                   "--only", line["name"])
        results = json.loads((self.store.job_dir / "qa" / "qa.json").read_text())
        info = results.get("lines", {}).get(line["name"])
        if not info:
            raise JobFailed("qa wrote no results for the line")
        with self.store.lock:
            line = self.store.state["lines"][line["name"]]
            for item in info["takes"]:
                number = take_number(item["take"])
                if number is not None and str(number) in line["takes"]:
                    line["takes"][str(number)]["qa"] = qa_scores(item)
            line["qa_best"] = take_number(info["best"])
            if line["pick"] is None and line["qa_best"] is not None:
                line["pick"] = {"take": line["qa_best"], "by": "worker", "at": now()}
                self.store.log("worker", "pick", line=line["name"], take=line["qa_best"],
                               text="the best take by QA (the line had no pick)")
            self.store.save()

    # ---------- in the game

    def ingame(self, job):
        c = self.config
        line = self.live_line(job)
        number = job.get("params", {}).get("take") or (line["pick"] or {}).get("take")
        if number is None:
            raise JobFailed("no take given and the line has no pick")
        take = self.store.job_dir / "mastered" / line["name"] / f"take{number}.wav"
        if not take.is_file():
            raise JobFailed(f"take {number} is not mastered")
        with self.store.lock:
            job["params"]["take"] = int(number)
            self.store.save()

        self.stage(job, "wait for the game")
        deadline = time.monotonic() + 600
        while self.container_exists(GAME_CONTAINER):
            if time.monotonic() > deadline:
                raise JobFailed(f"another game test ({GAME_CONTAINER}) has been running for 10 minutes")
            if self.cancelled:
                raise JobCancelled()
            time.sleep(3)

        game = self.store.root / "game"
        voice = game / f"voice-{job['id']}"
        capture = game / f"capture-{job['id']}.wav"
        try:
            shutil.rmtree(voice, ignore_errors=True)
            voice.mkdir(parents=True)
            shutil.copy2(take, voice / f"{line['name']}.wav")
            (voice / "voice.json").write_text(json.dumps({"schema": "halo-voice-lines-v1", "lines": [
                {"name": line["name"], "file": f"{line['name']}.wav", "text": line["text"],
                 "take": f"take{number}.wav"}]}, indent=2) + "\n")

            seconds = int(job["params"].get("seconds") or c.game_seconds)
            self.stage(job, f"play the game ({seconds} s)")
            self.run_command(job, [str(c.run_test), str(seconds), line["name"], "-v", f"{voice}:/game/voice:ro"])
            stdout = c.game_run / "stdout.txt"
            if stdout.is_file():
                self.write_log(job, "game: " + "game: ".join(
                    l for l in stdout.read_text(errors="replace").splitlines(True) if "voice" in l or "error" in l.lower()))
            if not (c.game_run / "capture.wav").is_file():
                raise JobFailed("the game wrote no capture.wav")
            shutil.copy2(c.game_run / "capture.wav", capture)

            self.stage(job, "find the line in the mix")
            slayer = "/extracted/sound/dialog/multiplayer1/slayer.wav"
            before = self.log_path(job).stat().st_size
            self.tools(job, "locate", "locate", "--capture", f"/game/{capture.name}",
                       f"/job/mastered/{line['name']}/take{number}.wav", slayer, mounts=[f"{game}:/game"])
            found = {}
            with open(self.log_path(job)) as f:
                f.seek(before)
                for text in f:
                    match = LOCATE.match(text.strip())
                    if match:
                        found[match["path"]] = {
                            "at": float(match["at"]), "correlation": float(match["corr"]),
                            "gain_db": float(match["gain"]), "speech_dbfs": float(match["speech"]),
                            "seconds": float(match["seconds"])}
            mine = found.get(f"/job/mastered/{line['name']}/take{number}.wav")
            stock = found.get(slayer)
            if not mine:
                raise JobFailed("locate found nothing (see the log)")

            # the clip: from a second before the earlier of the two to 1.5 s
            # after the later one ends
            spans = [mine] + ([stock] if stock and stock["correlation"] >= 0.5 else [])
            start = max(0.0, min(s["at"] for s in spans) - 1.0)
            end = max(s["at"] + s["seconds"] for s in spans) + 1.5
            clip = game / f"ingame-{job['id']}.wav"
            with wave.open(str(capture), "rb") as src:
                rate, width, channels = src.getframerate(), src.getsampwidth(), src.getnchannels()
                total = src.getnframes()
                first, last = int(start * rate), min(total, int(end * rate))
                src.setpos(min(first, total))
                frames = src.readframes(max(0, last - first))
            with wave.open(str(clip), "wb") as out:
                out.setnchannels(channels)
                out.setsampwidth(width)
                out.setframerate(rate)
                out.writeframes(frames)
            result = {"job": job["id"], "take": int(number), "at": now(), "clip": clip.name,
                      "clip_start": round(start, 3), "clip_seconds": round((last - first) / rate, 3),
                      "line": mine, "stock": stock, "stock_clip": "slayer",
                      "heard": mine["correlation"] >= 0.8}
            self.write_log(job, f"clip {clip.name}: {start:.2f} s to {end:.2f} s of the capture; line at"
                                f" {mine['at']:.2f} s (correlation {mine['correlation']:.3f},"
                                f" {mine['gain_db']:+.1f} dB)")
            with self.store.lock:
                line = self.store.state["lines"][line["name"]]
                line["ingame"].insert(0, result)
                for old in line["ingame"][INGAME_KEEP:]:
                    (game / old["clip"]).unlink(missing_ok=True)
                del line["ingame"][INGAME_KEEP:]
                job["result"] = result
                self.store.save()
            if not result["heard"]:
                raise JobFailed(f"the line was not clearly found in the mix (correlation {mine['correlation']:.2f})")
        finally:
            shutil.rmtree(voice, ignore_errors=True)
            capture.unlink(missing_ok=True)

    @staticmethod
    def stop_game(job):
        """Stops the game container if it is this job's (by its voice folder)."""
        out = subprocess.run(["docker", "inspect", GAME_CONTAINER, "--format",
                              "{{range .Mounts}}{{.Source}} {{end}}"], capture_output=True, text=True)
        if f"/voice-{job['id']} " in out.stdout + " ":
            subprocess.run(["docker", "rm", "-f", GAME_CONTAINER], capture_output=True)

    @staticmethod
    def container_exists(name):
        out = subprocess.run(["docker", "ps", "-a", "-q", "--filter", f"name=^{name}$"],
                             capture_output=True, text=True)
        return bool(out.stdout.strip())
