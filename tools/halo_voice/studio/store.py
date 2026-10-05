"""The studio's state: one JSON file (studio.json) under the studio folder,
written whole (to a temporary file, then renamed) after every change, and
guarded by one lock, shared by the web server's threads and the worker.

  lines     name -> the line's text and style, its takes (each with its seed,
            the text and style it was made with, its mastering and QA scores,
            and the ratings the user and Claude gave it), the pick, the
            comment thread, the mastering options and the in-game tests
  jobs      the worker's queue and history (generate, master, qa, ingame)
  activity  everything anyone did, with who ("user", "claude" or "worker")
            and when, for catching up

The first start migrates the pipeline's job folder (tools/halo_voice.py
prepare/master/qa): its lines, takes, picks and QA results, its audio copied
into the studio's own job folder, which the worker then generates, masters
and checks in (the original job folder is left as it was).
"""

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = "halo-voice-studio-v1"
JOB_SCHEMA = "halo-voice-job-v1"
LINE_NAME = re.compile(r"^[a-z0-9_]{1,31}$")
STYLES = ("calm", "hype")
ACTORS = ("user", "claude")
RATINGS = ("good", "bad")
DEFAULT_MASTERING = {"gain_db": 0.0, "eq": True, "stereo": True}
ACTIVITY_KEEP = 5000


class StudioError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    """An ISO 8601 time (Z or an offset) or Unix seconds, as Unix seconds."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except ValueError as e:
        raise StudioError(f"since: {value!r} is not an ISO time or Unix seconds") from e


def seed_of(output, take, salt=""):
    """generate_halo.py's seed for a take."""
    key = f"{output}.{take}" + (f".{salt}" if salt else "")
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def take_number(name):
    match = re.fullmatch(r"take(\d+)\.wav", name)
    return int(match.group(1)) if match else None


class Store:
    def __init__(self, data_dir):
        self.data = Path(data_dir)
        self.root = self.data / "studio"
        self.job_dir = self.root / "job"
        self.path = self.root / "studio.json"
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(exist_ok=True)
        (self.root / "game").mkdir(exist_ok=True)
        if self.path.is_file():
            self.state = json.loads(self.path.read_text())
            if self.state.get("schema") != SCHEMA:
                raise SystemExit(f"{self.path} is not {SCHEMA}")
        else:
            self.state = self.migrate()
            self.save()

    # ---------- persistence

    def save(self):
        with self.lock:
            self.state["version"] = self.state.get("version", 0) + 1
            self.state["updated_at"] = now()
            if len(self.state["activity"]) > ACTIVITY_KEEP:
                del self.state["activity"][:-ACTIVITY_KEEP]
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.state, indent=1) + "\n")
            os.replace(temporary, self.path)
            self.changed.notify_all()

    def next_id(self, kind):
        ids = self.state.setdefault("next_ids", {})
        ids[kind] = ids.get(kind, 0) + 1
        return ids[kind]

    def log(self, by, action, line=None, take=None, text=None, **detail):
        entry = {"id": self.next_id("activity"), "at": now(), "by": by, "action": action}
        if line is not None:
            entry["line"] = line
        if take is not None:
            entry["take"] = take
        if text is not None:
            entry["text"] = text
        if detail:
            entry["detail"] = detail
        self.state["activity"].append(entry)
        return entry

    # ---------- migration from the pipeline's job folder

    def migrate(self):
        state = {"schema": SCHEMA, "version": 0, "created_at": now(), "next_ids": {},
                 "lines": {}, "jobs": [], "activity": []}
        source = self.data / "job"
        manifest_path = source / "manifest.json"
        if not manifest_path.is_file():
            raise SystemExit(f"no {manifest_path} to start from (tools/halo_voice.py prepare makes one)")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema") != JOB_SCHEMA:
            raise SystemExit(f"{manifest_path} is not {JOB_SCHEMA}")
        self.job_dir.mkdir(parents=True, exist_ok=True)
        for folder in ("references", "outputs", "mastered"):
            if (source / folder).is_dir() and not (self.job_dir / folder).exists():
                shutil.copytree(source / folder, self.job_dir / folder)
        (self.job_dir / "qa").mkdir(exist_ok=True)
        for name in ("qa/qa.json", "selection.json"):
            if (source / name).is_file():
                shutil.copy2(source / name, self.job_dir / name)
        base = {key: value for key, value in manifest.items() if key != "lines"}
        (self.root / "job_base.json").write_text(json.dumps(base, indent=2) + "\n")

        qa = {}
        if (source / "qa" / "qa.json").is_file():
            qa = json.loads((source / "qa" / "qa.json").read_text())
        mastered = {}
        if (source / "mastered" / "master.json").is_file():
            mastered = json.loads((source / "mastered" / "master.json").read_text()).get("takes", {})
        selection = {}
        if (source / "selection.json").is_file():
            selection = json.loads((source / "selection.json").read_text())
        created = datetime.fromtimestamp(manifest_path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.state = state
        for item in manifest["lines"]:
            name = item["name"]
            line = new_line(name, item["text"], item["style"], "user", created)
            scores = {t["take"]: t for t in qa.get("lines", {}).get(name, {}).get("takes", [])}
            for path in sorted((self.job_dir / "outputs" / item["output"]).glob("take*.wav")):
                number = take_number(path.name)
                if number is None:
                    continue
                take = new_take(number, seed_of(item["output"], number), "", item["text"], item["style"], created)
                info = mastered.get(f"mastered/{item['output']}/{path.name}")
                if info:
                    take["master"] = dict(info, eq=True, stereo=True, trim_db=0.0, at=created)
                if path.name in scores:
                    take["qa"] = qa_scores(scores[path.name])
                line["takes"][str(number)] = take
            best = qa.get("lines", {}).get(name, {}).get("best")
            if best:
                line["qa_best"] = take_number(best)
            if name in selection and take_number(selection[name]) is not None:
                # (qa's picks, the ones installed)
                line["pick"] = {"take": take_number(selection[name]), "by": "worker", "at": created}
            state["lines"][name] = line
        self.log("worker", "migrate", text=f"{len(state['lines'])} lines from {source}")
        return state

    # ---------- the job folder the tools work in

    def write_manifest(self):
        """The studio's lines as a tools/halo_voice.py job manifest."""
        with self.lock:
            base = json.loads((self.root / "job_base.json").read_text())
            lines = [{"name": l["name"], "text": l["text"], "style": l["style"], "output": l["name"]}
                     for l in self.state["lines"].values() if not l["archived"]]
            manifest = dict(base, lines=lines)
            temporary = self.job_dir / "manifest.json.tmp"
            temporary.write_text(json.dumps(manifest, indent=2) + "\n")
            os.replace(temporary, self.job_dir / "manifest.json")
            return manifest

    # ---------- lookups

    def line(self, name):
        line = self.state["lines"].get(name)
        if line is None:
            raise StudioError(f"no line {name!r}", 404)
        return line

    def take(self, line, number):
        try:
            take = line["takes"].get(str(int(number)))
        except (TypeError, ValueError):
            take = None
        if take is None:
            raise StudioError(f"line {line['name']} has no take {number!r}", 404)
        return take

    def job(self, job_id):
        for job in self.state["jobs"]:
            if str(job["id"]) == str(job_id):
                return job
        raise StudioError(f"no job {job_id!r}", 404)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)


def new_line(name, text, style, by, at):
    return {"name": name, "text": text, "style": style, "archived": False, "created_at": at, "created_by": by,
            "mastering": dict(DEFAULT_MASTERING), "pick": None, "qa_best": None, "takes": {},
            "comments": [], "ingame": []}


def new_take(number, seed, salt, text, style, at, job=None):
    return {"n": number, "seed": seed, "salt": salt, "text": text, "style": style, "created_at": at,
            "job": job, "master": None, "qa": None, "ratings": {}}


def qa_scores(item):
    return {key: item.get(key) for key in ("heard", "wer", "cer", "similarity", "seconds", "limited_samples")}


def check_actor(by):
    if by not in ACTORS:
        raise StudioError(f"by: {by!r} is not one of {', '.join(ACTORS)}")
    return by


def check_text(text):
    if not isinstance(text, str) or not text.strip():
        raise StudioError("text: give the words to say")
    text = " ".join(text.split())
    if len(text) > 200:
        raise StudioError("text: at most 200 characters")
    return text


def check_style(style):
    if style not in STYLES:
        raise StudioError(f"style: {style!r} is not one of {', '.join(STYLES)}")
    return style


def wait_for_change(store, version, timeout):
    """Blocks until the state's version passes version (or timeout s)."""
    deadline = time.monotonic() + timeout
    with store.lock:
        while store.state["version"] <= version:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            store.changed.wait(left)
        return store.state["version"]
