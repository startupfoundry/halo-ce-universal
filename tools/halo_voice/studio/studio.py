#!/usr/bin/env python3
"""Voice studio: a small web app for iterating on new multiplayer announcer
lines (tools/halo_voice.py), for a person in a browser and for Claude over
HTTP and JSON, together or apart. Standard library only.

  python3 tools/halo_voice/studio/studio.py --host 100.83.137.21 --port 8765 \\
      --data ~/model-workloads/halo-voice

The data folder holds extracted/ (the announcer's clips), job/ (the
pipeline's job, migrated on the first start) and studio/ (the studio's own
state, studio.json, its job folder, job logs and in-game clips). Everything
in it is the user's own audio and stays there. The page is /, the API's
help /api (API.md).
"""

import argparse
import json
import mimetypes
import re
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import halo_voice  # noqa: E402 (tools/halo_voice.py: the clips' names and styles)
from store import (DEFAULT_MASTERING, LINE_NAME, RATINGS, Store, StudioError, check_actor,  # noqa: E402
                   check_style, check_text, new_line, now, parse_time, wait_for_change)
from worker import Worker  # noqa: E402


JOBS_KEEP = 300
JOB_KINDS = ("generate", "master", "qa", "ingame")
MAX_TAKES_PER_JOB = 8


class Config:
    def __init__(self, args):
        self.data = Path(args.data).expanduser().resolve()
        self.extracted = self.data / "extracted"
        self.tools = Path(args.tools).expanduser().resolve()
        self.game_run = Path(args.game_run).expanduser().resolve()
        self.run_test = Path(args.run_test).expanduser().resolve()
        self.game_seconds = args.game_seconds
        self.gen_image = args.gen_image
        self.gen_volume = args.gen_volume
        self.tools_image = args.tools_image
        self.models_volume = args.models_volume


class Studio:
    """The actions, shared by the page and the API."""

    def __init__(self, config):
        self.config = config
        self.store = Store(config.data)
        self.worker = Worker(self.store, config)
        self.originals = self.load_originals()
        self.install_lock = threading.Lock()

    # ---------- views

    def load_originals(self):
        doc = json.loads((self.config.extracted / "manifest.json").read_text())
        group = {name: style for style, names in halo_voice.STYLES.items() for name in names}
        return [{"name": clip["name"], "text": clip["text"], "seconds": clip["seconds"],
                 "group": group.get(clip["name"], "other"), "file": clip["file"],
                 "url": f"/audio/original/{clip['name']}.wav"} for clip in doc["lines"]]

    def originals_view(self):
        qa = {}
        path = self.store.job_dir / "qa" / "qa.json"
        if path.is_file():
            try:
                qa = json.loads(path.read_text())
            except json.JSONDecodeError:
                qa = {}
        scores = {o["name"]: o for o in qa.get("originals", [])}
        base = json.loads((self.store.root / "job_base.json").read_text())
        clips = []
        for clip in self.originals:
            item = {key: clip[key] for key in ("name", "text", "seconds", "group", "url")}
            if clip["name"] in scores:
                item["heard"] = scores[clip["name"]].get("heard")
                item["similarity"] = scores[clip["name"]].get("similarity")
            clips.append(item)
        order = {"calm": 0, "hype": 1, "other": 2}
        clips.sort(key=lambda c: (order[c["group"]], c["name"]))
        return {"clips": clips, "similarity": qa.get("original_similarity"),
                "references": {style: {"text": ref["text"], "seconds": ref["seconds"],
                                       "url": f"/audio/reference/{style}.wav"}
                               for style, ref in base.get("references", {}).items()}}

    def line_view(self, line):
        view = {key: line[key] for key in ("name", "text", "style", "archived", "created_at", "created_by",
                                           "mastering", "pick", "qa_best", "comments")}
        takes = []
        for number in sorted(line["takes"], key=int):
            take = dict(line["takes"][number])
            name = line["name"]
            take["mastered_url"] = (f"/audio/take/{name}/{number}.wav"
                                    if (self.store.job_dir / "mastered" / name / f"take{number}.wav").is_file()
                                    else None)
            take["raw_url"] = f"/audio/raw/{name}/{number}.wav"
            take["stale"] = take["text"] != line["text"] or take["style"] != line["style"]
            take["picked"] = bool(line["pick"]) and line["pick"]["take"] == take["n"]
            take["best"] = line["qa_best"] == take["n"]
            takes.append(take)
        view["takes"] = takes
        view["ingame"] = [dict(test, url=f"/audio/game/{test['clip']}") for test in line["ingame"]]
        view["jobs"] = [job["id"] for job in self.store.state["jobs"]
                        if job.get("line") == line["name"] and job["status"] in ("queued", "running")]
        return view

    def job_view(self, job, tail=0):
        view = {key: value for key, value in job.items() if key != "containers"}
        if tail:
            view["log_tail"] = self.log_tail(job, tail)
        return view

    def log_tail(self, job, count):
        path = self.store.root / "jobs" / f"{job['id']}.log"
        if not path.is_file():
            return []
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = [segment.split("\r")[-1] for segment in text.split("\n")]
        return [line for line in lines if line.strip()][-count:]

    def state_view(self, archived=True):
        with self.store.lock:
            lines = [self.line_view(line) for line in self.store.state["lines"].values()
                     if archived or not line["archived"]]
            jobs = [self.job_view(job) for job in self.store.state["jobs"][-40:]][::-1]
            return {"version": self.store.state["version"], "updated_at": self.store.state.get("updated_at"),
                    "lines": lines, "jobs": jobs, "worker": self.worker_view(),
                    "activity": self.store.state["activity"][-40:][::-1]}

    def worker_view(self):
        current = self.worker.current
        queued = sum(1 for job in self.store.state["jobs"] if job["status"] == "queued")
        return {"alive": self.worker.is_alive(), "running": current["id"] if current else None, "queued": queued}

    # ---------- line actions

    def add_line(self, body):
        by = check_actor(body.get("by", "claude"))
        name = body.get("name", "")
        if not isinstance(name, str) or not LINE_NAME.match(name):
            raise StudioError("name: lower case letters, digits and _, up to 31 (it is the game's name for the line)")
        text = check_text(body.get("text"))
        style = check_style(body.get("style", "calm"))
        with self.store.lock:
            if name in self.store.state["lines"]:
                raise StudioError(f"line {name} exists" + (" (archived)" if self.store.state["lines"][name]["archived"]
                                                           else ""), 409)
            line = new_line(name, text, style, by, now())
            self.store.state["lines"][name] = line
            self.store.log(by, "add_line", line=name, text=text, style=style)
            job = None
            if body.get("generate"):
                job = self.queue(by, "generate", name, {"count": self.count(body.get("generate"))})
            self.store.save()
            return {"line": self.line_view(line), "job": job}

    def edit_line(self, name, body):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            line = self.store.line(name)
            changes = {}
            if "text" in body and check_text(body["text"]) != line["text"]:
                changes["text"] = {"from": line["text"], "to": check_text(body["text"])}
                line["text"] = changes["text"]["to"]
            if "style" in body and check_style(body["style"]) != line["style"]:
                changes["style"] = {"from": line["style"], "to": body["style"]}
                line["style"] = body["style"]
            if changes:
                self.store.log(by, "edit_line", line=name, text=line["text"], changes=changes)
                self.store.save()
            return {"line": self.line_view(line), "changed": bool(changes)}

    def set_mastering(self, name, body):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            line = self.store.line(name)
            options = dict(DEFAULT_MASTERING, **line["mastering"])
            if "gain_db" in body:
                try:
                    gain = float(body["gain_db"])
                except (TypeError, ValueError) as e:
                    raise StudioError("gain_db: a number of dB") from e
                if not -12.0 <= gain <= 6.0:
                    raise StudioError("gain_db: between -12 and +6 dB")
                options["gain_db"] = round(gain, 2)
            for key in ("eq", "stereo"):
                if key in body:
                    if not isinstance(body[key], bool):
                        raise StudioError(f"{key}: true or false")
                    options[key] = body[key]
            changed = options != line["mastering"]
            line["mastering"] = options
            job = None
            if changed:
                self.store.log(by, "mastering", line=name, **options)
            if (body.get("remaster", True) and (changed or body.get("remaster")) and line["takes"]
                    and not line["archived"]):
                job = self.queue(by, "master", name, {})
            self.store.save()
            return {"line": self.line_view(line), "job": job}

    def archive(self, name, body, archived):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            line = self.store.line(name)
            if line["archived"] != archived:
                line["archived"] = archived
                self.store.log(by, "archive" if archived else "unarchive", line=name)
                self.store.save()
            return {"line": self.line_view(line)}

    def delete(self, name, body):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            line = self.store.line(name)
            if not line["archived"]:
                raise StudioError("archive the line before deleting it", 409)
            if any(job.get("line") == name and job["status"] in ("queued", "running")
                   for job in self.store.state["jobs"]):
                raise StudioError("the line has jobs queued or running", 409)
            for folder in ("outputs", "mastered"):
                path = self.store.job_dir / folder / name
                if path.is_dir():
                    for item in path.iterdir():
                        item.unlink()
                    path.rmdir()
            for test in line["ingame"]:
                (self.store.root / "game" / test["clip"]).unlink(missing_ok=True)
            del self.store.state["lines"][name]
            self.store.log(by, "delete_line", line=name, text=line["text"])
            self.store.save()
            self.store.write_manifest()
            return {"deleted": name}

    def pick(self, name, body):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            line = self.store.line(name)
            take = self.store.take(line, body.get("take"))
            line["pick"] = {"take": take["n"], "by": by, "at": now()}
            self.store.log(by, "pick", line=name, take=take["n"], text=body.get("note"))
            self.store.save()
            return {"line": self.line_view(line)}

    def rate(self, name, number, body):
        by = check_actor(body.get("by", "claude"))
        rating = body.get("rating")
        if rating not in RATINGS + (None,):
            raise StudioError(f"rating: one of {', '.join(RATINGS)}, or null to clear")
        note = body.get("note") or None
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise StudioError("note: text up to 2000 characters")
        with self.store.lock:
            line = self.store.line(name)
            take = self.store.take(line, number)
            if rating is None and note is None:
                take["ratings"].pop(by, None)
            else:
                take["ratings"][by] = {"rating": rating, "note": note, "at": now()}
            self.store.log(by, "rate", line=name, take=take["n"], text=note, rating=rating)
            self.store.save()
            return {"line": self.line_view(line)}

    def comment(self, name, body):
        by = check_actor(body.get("by", "claude"))
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise StudioError("text: the comment, up to 5000 characters")
        with self.store.lock:
            line = self.store.line(name)
            reply_to = body.get("reply_to")
            if reply_to is not None and not any(c["id"] == reply_to for c in line["comments"]):
                raise StudioError(f"reply_to: line {name} has no comment {reply_to}")
            comment = {"id": self.store.next_id("comment"), "at": now(), "by": by, "text": text.strip(),
                       "reply_to": reply_to}
            line["comments"].append(comment)
            self.store.log(by, "comment", line=name, text=comment["text"], comment=comment["id"],
                           reply_to=reply_to)
            self.store.save()
            return {"comment": comment}

    def delete_comment(self, name, comment_id, body):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            line = self.store.line(name)
            comment = next((c for c in line["comments"] if str(c["id"]) == str(comment_id)), None)
            if comment is None:
                raise StudioError(f"line {name} has no comment {comment_id}", 404)
            if comment["by"] != by:
                raise StudioError("only its author can delete a comment", 403)
            line["comments"] = [c for c in line["comments"] if c is not comment and c.get("reply_to") != comment["id"]]
            self.store.log(by, "delete_comment", line=name, comment=comment["id"])
            self.store.save()
            return {"deleted": comment["id"]}

    # ---------- jobs

    @staticmethod
    def count(value):
        try:
            count = int(value if value is not True else 2)
        except (TypeError, ValueError) as e:
            raise StudioError("count: a number of takes") from e
        if not 1 <= count <= MAX_TAKES_PER_JOB:
            raise StudioError(f"count: 1 to {MAX_TAKES_PER_JOB} takes a job")
        return count

    def queue(self, by, kind, line, params):
        job = {"id": self.store.next_id("job"), "kind": kind, "line": line, "params": params, "by": by,
               "status": "queued", "stage": None, "created_at": now(), "started_at": None,
               "finished_at": None, "error": None, "result": None}
        self.store.state["jobs"].append(job)
        done = [j for j in self.store.state["jobs"] if j["status"] not in ("queued", "running")]
        while len(self.store.state["jobs"]) > JOBS_KEEP and done:
            old = done.pop(0)
            self.store.state["jobs"].remove(old)
            (self.store.root / "jobs" / f"{old['id']}.log").unlink(missing_ok=True)
        self.store.log(by, "queue_" + kind, line=line, job=job["id"], **params)
        return self.job_view(job)

    def request_job(self, name, kind, body):
        by = check_actor(body.get("by", "claude"))
        params = {}
        with self.store.lock:
            line = self.store.line(name)
            if line["archived"]:
                raise StudioError(f"line {name} is archived", 409)
            if kind == "generate":
                params["count"] = self.count(body.get("count", 2))
                if body.get("salt"):
                    salt = str(body["salt"])
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", salt):
                        raise StudioError("salt: letters, digits, _ . -, up to 32")
                    params["salt"] = salt
            elif kind == "ingame":
                if body.get("take") is not None:
                    params["take"] = self.store.take(line, body["take"])["n"]
                elif not line["pick"]:
                    raise StudioError("the line has no pick; give a take")
                if body.get("seconds"):
                    params["seconds"] = max(16, min(60, int(body["seconds"])))
            elif kind in ("master", "qa"):
                if not line["takes"]:
                    raise StudioError("the line has no takes yet; generate some", 409)
            job = self.queue(by, kind, name, params)
            self.store.save()
            return {"job": job}

    def cancel(self, job_id, body):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            job = self.store.job(job_id)
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["error"] = "cancelled"
                job["finished_at"] = now()
            elif job["status"] != "running":
                raise StudioError(f"job {job_id} is {job['status']}", 409)
            self.store.log(by, "cancel", line=job.get("line"), job=job["id"])
            self.store.save()
        if job["status"] == "running":
            self.worker.cancel(job)
        return {"job": self.job_view(job)}

    def retry(self, job_id, body):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            job = self.store.job(job_id)
            if job["status"] not in ("failed", "cancelled"):
                raise StudioError(f"job {job_id} is {job['status']}", 409)
            params = {key: value for key, value in job["params"].items() if key != "numbers"}
            if job["kind"] == "generate" and job["params"].get("numbers"):
                params["numbers"] = job["params"]["numbers"]
            new = self.queue(by, job["kind"], job["line"], params)
            self.store.save()
            return {"job": new}

    # ---------- install

    def install(self, body):
        by = check_actor(body.get("by", "claude"))
        with self.store.lock:
            wanted = body.get("lines")
            lines = [line for line in self.store.state["lines"].values()
                     if not line["archived"] and line["pick"] and (not wanted or line["name"] in wanted)]
            missing = [name for name in wanted or [] if name not in {line["name"] for line in lines}]
            picks = {line["name"]: line["pick"]["take"] for line in lines
                     if (self.store.job_dir / "mastered" / line["name"] / f"take{line['pick']['take']}.wav").is_file()}
            if not picks:
                raise StudioError("no picked, mastered takes to install")
            self.store.write_manifest()
        command = [sys.executable, str(self.config.tools / "halo_voice.py"), "install", "--job", str(self.store.job_dir),
                   "--data", str(self.config.game_run), "--only", ",".join(picks)]
        for name, number in picks.items():
            command += ["--take", f"{name}={number}"]
        with self.install_lock:
            out = subprocess.run(command, capture_output=True, text=True)
        output = (out.stdout + out.stderr).strip()
        with self.store.lock:
            self.store.log(by, "install", text=f"{len(picks)} lines into {self.config.game_run / 'voice'}",
                           picks=picks, ok=out.returncode == 0)
            self.store.save()
        if out.returncode != 0:
            raise StudioError(f"install failed: {output}", 500)
        return {"installed": picks, "skipped": missing, "folder": str(self.config.game_run / "voice"),
                "output": output.splitlines()}

    # ---------- activity

    def activity(self, query):
        since = parse_time(first(query, "since"))
        since_id = first(query, "since_id")
        by = first(query, "by")
        name = first(query, "line")
        limit = min(int(first(query, "limit") or 200), 2000)
        with self.store.lock:
            items = self.store.state["activity"]
            if since_id:
                items = [a for a in items if a["id"] > int(since_id)]
            if since is not None:
                items = [a for a in items if parse_time(a["at"]) > since]
            if by:
                wanted = set(by.split(","))
                items = [a for a in items if a["by"] in wanted]
            if name:
                items = [a for a in items if a.get("line") == name]
            items = items[-limit:]
            return {"now": now(), "last_id": self.store.state["activity"][-1]["id"]
                    if self.store.state["activity"] else 0, "activity": items}


def first(query, key):
    values = query.get(key)
    return values[0] if values else None


# ---------- HTTP

AUDIO_ROUTES = {
    "take": lambda s, a, b: s.store.job_dir / "mastered" / a / f"take{b}",
    "raw": lambda s, a, b: s.store.job_dir / "outputs" / a / f"take{b}",
}
SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


class Handler(BaseHTTPRequestHandler):
    studio = None
    server_version = "HaloVoiceStudio/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if getattr(self, "command", None) == "POST" or (len(args) > 1 and str(args[1])[:1] in "45"):
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    # ---------- responses

    def send_json(self, value, status=200):
        body = (json.dumps(value, indent=1) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, text, content_type, status=200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path):
        """A file, with byte ranges (Safari plays audio only from ranges)."""
        if not path.is_file():
            return self.send_json({"error": "no such file"}, 404)
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if not match or (not match[1] and not match[2]):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        kind = "audio/wav" if path.suffix == ".wav" else mimetypes.guess_type(path.name)[0]
        self.send_header("Content-Type", kind or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---------- routing

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.handle_request(self.get)

    def do_POST(self):
        self.handle_request(self.post)

    def handle_request(self, method):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        try:
            result = method(parts, query)
            if result is not None:
                self.send_json(result)
        except StudioError as e:
            self.send_json({"error": str(e)}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            raise

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        if length > 1 << 20:
            raise StudioError("body too large", 413)
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            raise StudioError(f"body: not JSON ({e})") from e
        if not isinstance(value, dict):
            raise StudioError("body: a JSON object")
        return value

    def get(self, parts, query):
        s = self.studio
        if not parts or parts == ["index.html"]:
            self.send_text((HERE / "index.html").read_text(), "text/html; charset=utf-8")
            return None
        if parts[0] == "audio" and len(parts) >= 3:
            return self.audio(parts)
        if parts[0] != "api":
            raise StudioError("not found", 404)
        rest = parts[1:]
        if not rest or rest == ["help"]:
            self.send_text((HERE / "API.md").read_text(), "text/markdown; charset=utf-8")
            return None
        if rest == ["state"]:
            wait = first(query, "wait")
            if wait is not None:
                wait_for_change(s.store, int(wait), min(float(first(query, "timeout") or 25), 60))
            return s.state_view(archived=first(query, "archived") != "0")
        if rest == ["lines"]:
            archived = first(query, "archived") == "1"
            with s.store.lock:
                return {"lines": [s.line_view(line) for line in s.store.state["lines"].values()
                                  if archived or not line["archived"]]}
        if len(rest) == 2 and rest[0] == "lines":
            with s.store.lock:
                return {"line": s.line_view(s.store.line(rest[1]))}
        if rest == ["originals"]:
            return s.originals_view()
        if rest == ["jobs"]:
            status = first(query, "status")
            with s.store.lock:
                jobs = [s.job_view(job) for job in s.store.state["jobs"]
                        if not status or job["status"] in status.split(",")]
                return {"jobs": jobs[::-1][:int(first(query, "limit") or 50)], "worker": s.worker_view()}
        if len(rest) == 2 and rest[0] == "jobs":
            with s.store.lock:
                return {"job": s.job_view(s.store.job(rest[1]), int(first(query, "tail") or 60))}
        if rest == ["activity"]:
            return s.activity(query)
        raise StudioError("not found", 404)

    def audio(self, parts):
        s = self.studio
        kind = parts[1]
        if not all(SAFE.match(p) for p in parts[2:]) or not parts[-1].endswith(".wav"):
            raise StudioError("not found", 404)
        stem = parts[-1][:-4]
        if kind in AUDIO_ROUTES and len(parts) == 4:
            path = AUDIO_ROUTES[kind](s, parts[2], stem).with_suffix(".wav")
        elif kind == "original" and len(parts) == 3:
            clip = next((c for c in s.originals if c["name"] == stem), None)
            if clip is None:
                raise StudioError("not found", 404)
            path = s.config.extracted / clip["file"]
        elif kind == "reference" and len(parts) == 3:
            path = s.store.job_dir / "references" / f"{stem}.wav"
        elif kind == "game" and len(parts) == 3:
            path = s.store.root / "game" / f"{stem}.wav"
        else:
            raise StudioError("not found", 404)
        self.send_file(path)
        return None

    def post(self, parts, query):
        s = self.studio
        body = self.body()
        if not parts or parts[0] != "api":
            raise StudioError("not found", 404)
        rest = parts[1:]
        if rest == ["lines"]:
            return s.add_line(body)
        if rest == ["install"]:
            return s.install(body)
        if len(rest) == 3 and rest[0] == "lines":
            name, action = rest[1], rest[2]
            actions = {
                "edit": lambda: s.edit_line(name, body),
                "mastering": lambda: s.set_mastering(name, body),
                "archive": lambda: s.archive(name, body, True),
                "unarchive": lambda: s.archive(name, body, False),
                "delete": lambda: s.delete(name, body),
                "pick": lambda: s.pick(name, body),
                "comments": lambda: s.comment(name, body),
                "generate": lambda: s.request_job(name, "generate", body),
                "master": lambda: s.request_job(name, "master", body),
                "qa": lambda: s.request_job(name, "qa", body),
                "ingame": lambda: s.request_job(name, "ingame", body),
            }
            if action in actions:
                return actions[action]()
        if len(rest) == 5 and rest[0] == "lines" and rest[2] == "comments" and rest[4] == "delete":
            return s.delete_comment(rest[1], rest[3], body)
        if len(rest) == 5 and rest[0] == "lines" and rest[2] == "takes" and rest[4] == "rate":
            return s.rate(rest[1], rest[3], body)
        if len(rest) == 3 and rest[0] == "jobs" and rest[2] in ("cancel", "retry"):
            return s.cancel(rest[1], body) if rest[2] == "cancel" else s.retry(rest[1], body)
        raise StudioError("not found", 404)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="100.83.137.21", help="the address to listen on (the tailnet's)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default="~/model-workloads/halo-voice")
    ap.add_argument("--tools", default=str(HERE.parent.parent), help="the folder holding halo_voice.py")
    ap.add_argument("--game-run", default="~/halo-voice/run", help="the game's test data folder")
    ap.add_argument("--run-test", default="~/halo-voice/run_test.sh")
    ap.add_argument("--game-seconds", type=int, default=24, help="how long an in-game test runs")
    ap.add_argument("--gen-image", default="blitz-roster-audio:qwen3-tts")
    ap.add_argument("--gen-volume", default="blitz-roster-audio-models")
    ap.add_argument("--tools-image", default="halo-voice-tools")
    ap.add_argument("--models-volume", default="halo-voice-models")
    args = ap.parse_args()

    studio = Studio(Config(args))
    studio.worker.start()
    Handler.studio = studio
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"voice studio on http://{args.host}:{args.port}/ (data {studio.config.data})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
