"""Persistent memory (main design §7.3.4): recipes (L1), playbooks (L2), skills (L3), lessons, episode traces.

Stored as JSON files under a memory directory so a run can be snapshotted (copied) and restored.
"""
import json, os, shutil, time, threading, re


class Memory:
    def __init__(self, root, enabled=("recipes", "playbooks", "skills", "lessons")):
        self.root = os.path.abspath(root); self.enabled = set(enabled); self.lock = threading.Lock()
        os.makedirs(os.path.join(root, "skills"), exist_ok=True)
        os.makedirs(os.path.join(root, "traces"), exist_ok=True)
        self._data = {k: self._load(k) for k in ("recipes", "playbooks", "skills", "lessons")}

    def _p(self, k): return os.path.join(self.root, f"{k}.json")

    def _load(self, k):
        try:
            with open(self._p(k)) as f: return json.load(f)
        except FileNotFoundError:
            return {} if k in ("recipes", "playbooks", "skills") else []

    def _save(self, k):
        tmp = self._p(k) + ".tmp"
        with open(tmp, "w") as f: json.dump(self._data[k], f, indent=1)
        os.replace(tmp, self._p(k))

    # ---------------- recipes: keyed by pattern_id, versioned
    def recipe_for(self, pattern_id):
        if "recipes" not in self.enabled: return None
        e = self._data["recipes"].get(pattern_id)
        return e["versions"][-1] if e and e["state"] == "active" else None

    def put_recipe(self, pattern_id, recipe, state="active", note=""):
        with self.lock:
            e = self._data["recipes"].setdefault(pattern_id, {"state": state, "versions": [], "history": []})
            e["versions"].append(recipe); e["state"] = state
            e["history"].append({"t": time.time(), "state": state, "recipe_id": recipe.get("id"), "note": note})
            self._save("recipes")

    def set_recipe_state(self, pattern_id, state, note=""):
        with self.lock:
            e = self._data["recipes"].get(pattern_id)
            if not e: return
            e["state"] = state; e["history"].append({"t": time.time(), "state": state, "note": note})
            self._save("recipes")

    def all_recipes(self):
        return {k: v for k, v in self._data["recipes"].items()}

    # ---------------- playbooks
    def playbooks(self):
        if "playbooks" not in self.enabled: return []
        return [p for p in self._data["playbooks"].values() if p.get("state", "active") == "active"]

    def put_playbook(self, pb):
        with self.lock:
            pb.setdefault("state", "active"); pb.setdefault("stats", {"tried": 0, "won": 0}); pb.setdefault("created", time.time())
            self._data["playbooks"][pb["id"]] = pb; self._save("playbooks")

    def playbook_stat(self, pb_id, won):
        with self.lock:
            pb = self._data["playbooks"].get(pb_id)
            if not pb: return
            pb["stats"]["tried"] += 1; pb["stats"]["won"] += int(won)
            if pb["stats"]["tried"] >= 4 and pb["stats"]["won"] / pb["stats"]["tried"] < 0.25: pb["state"] = "retired"
            self._save("playbooks")

    # ---------------- skills (python plug-ins)
    def skills(self):
        if "skills" not in self.enabled: return []
        return [s for s in self._data["skills"].values() if s.get("state") == "active"]

    def put_skill(self, skill_id, code, meta):
        with self.lock:
            path = os.path.join(self.root, "skills", f"{skill_id}.py")
            with open(path, "w") as f: f.write(code)
            self._data["skills"][skill_id] = {"id": skill_id, "path": path, "state": meta.pop("state", "active"),
                                              "created": time.time(), **meta}
            self._save("skills")
            return path

    def set_skill_state(self, skill_id, state):
        with self.lock:
            if skill_id in self._data["skills"]:
                self._data["skills"][skill_id]["state"] = state; self._save("skills")

    # ---------------- lessons
    def add_lesson(self, lesson):
        if "lessons" not in self.enabled: return
        with self.lock:
            lesson["t"] = time.time(); self._data["lessons"].append(lesson); self._save("lessons")

    def search_lessons(self, query, k=6):
        if "lessons" not in self.enabled: return []
        q = set(re.findall(r"[a-z0-9_]+", (query or "").lower()))
        scored = []
        for l in self._data["lessons"]:
            txt = json.dumps(l).lower(); words = set(re.findall(r"[a-z0-9_]+", txt))
            scored.append((len(q & words), l))
        scored.sort(key=lambda x: -x[0])
        return [l for s, l in scored[:k] if s > 0]

    def lessons(self):
        return list(self._data["lessons"]) if "lessons" in self.enabled else []

    # ---------------- traces
    def write_trace(self, episode_id, trace):
        with open(os.path.join(self.root, "traces", f"{episode_id}.json"), "w") as f: json.dump(trace, f)

    def traces(self):
        d = os.path.join(self.root, "traces"); out = []
        for fn in sorted(os.listdir(d)):
            try:
                with open(os.path.join(d, fn)) as f: out.append(json.load(f))
            except Exception: pass
        return out

    # ---------------- snapshot
    def snapshot(self, dest, keep=("playbooks", "skills", "lessons", "recipes")):
        os.makedirs(dest, exist_ok=True)
        for k in keep:
            if os.path.exists(self._p(k)): shutil.copy(self._p(k), os.path.join(dest, f"{k}.json"))
        if "skills" in keep:
            shutil.copytree(os.path.join(self.root, "skills"), os.path.join(dest, "skills"), dirs_exist_ok=True)
            # rewrite skill paths
            p = os.path.join(dest, "skills.json")
            if os.path.exists(p):
                d = json.load(open(p))
                for s in d.values(): s["path"] = os.path.join(dest, "skills", os.path.basename(s["path"]))
                json.dump(d, open(p, "w"), indent=1)
        os.makedirs(os.path.join(dest, "traces"), exist_ok=True)
