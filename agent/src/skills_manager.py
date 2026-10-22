"""Skills manager for dynamic loading/unloading of agent skills at runtime.

Uses the Strands AgentSkills plugin to manage skills without restarting the agent.
Skills are loaded from the filesystem (./skills/ directory) and optionally from S3.
"""

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Optional

from strands import Agent, AgentSkills, Skill

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent / "skills"

# Reference-bundling: text file extensions whose content is inlined into a skill's instructions,
# and the per-skill byte budget for doing so. See SkillsManager._bundle_references.
_BUNDLE_TEXT_EXTS = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv", ".rst"}
_BUNDLE_MAX_BYTES = int(os.environ.get("SKILL_BUNDLE_REFERENCE_MAX_BYTES", str(24 * 1024)))
# Concurrent S3 reads while loading S3-authored skills (see SkillsManager._load_from_s3).
_S3_FETCH_PARALLEL = max(1, int(os.environ.get("SKILLS_S3_FETCH_PARALLEL", "8")))


class SkillsManager:
    """Manages the lifecycle of agent skills at runtime."""

    def __init__(self, skills_dir: Optional[Path] = None, s3_bucket: Optional[str] = None, s3_prefix: str = "skills/"):
        self.skills_dir = skills_dir or SKILLS_DIR
        self._bundled_skills_dir = SKILLS_DIR  # Original bundled skills (may be read-only)
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # AgentCore runtime has read-only /var/task; fall back to /tmp for writable skills
            logger.info("Skills dir %s is read-only, using /tmp/skills for writable skills", self.skills_dir)
            self.skills_dir = Path("/tmp/skills")
            self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.s3_bucket = s3_bucket or os.environ.get("SKILLS_S3_BUCKET", "")
        self.s3_prefix = s3_prefix or os.environ.get("SKILLS_S3_PREFIX", "skills/")
        if self.s3_prefix and not self.s3_prefix.endswith("/"):
            self.s3_prefix += "/"

        self._plugin: Optional[AgentSkills] = None
        self._agent: Optional[Agent] = None
        self._shared_skills: list[Skill] = []
        self._skill_tools: dict[str, list[str]] = {}
        self._dirty: set[str] = set()
        self._skill_source: dict[str, str] = {}  # skill_name -> "local" | "s3" | "memory"
        self._skill_package_root: dict[str, Path] = {}
        self._skill_s3_prefix: dict[str, str] = {}
        self._s3_client = None
        self._preloaded_disk_skills: Optional[list[Skill]] = None

    def preload_disk_skills(self) -> int:
        """Load the BUNDLED/on-disk skills ahead of :meth:`create_plugin`.

        For container warm-up (AgentCore Runtime V2 snapshots work done at start, so it is not
        repeated for every session): parsing every SKILL.md and bundling its references is
        user-independent and the disk is the deployed bundle. S3-authored skills are NOT
        preloaded — they change at runtime (``save_skill(target="s3")``), so they are still read
        when the plugin is created, exactly as before. Idempotent; returns the count loaded.
        """
        if self._preloaded_disk_skills is None and self._plugin is None:
            self._preloaded_disk_skills = self._load_from_disk()
        return len(self._preloaded_disk_skills or [])

    def _get_s3(self):
        """Lazy-init S3 client."""
        if not self._s3_client and self.s3_bucket:
            import boto3
            self._s3_client = boto3.client("s3")
        return self._s3_client

    def create_plugin(self, name_prefix: str = "") -> AgentSkills:
        """Create the AgentSkills plugin, loading skills from disk and S3.

        ``name_prefix`` scopes the plugin to skills whose name starts with it (default "" = all).
        The Render Agent passes ``"render-"`` so it exposes ONLY render skills (the consolidated
        render-media bundle + any S3 custom render-* skills) and not unrelated skills like
        skill-creator/video-processing -- while still going through the SAME SkillsManager path as
        every other agent (S3 discovery, lifecycle) instead of an ad-hoc AgentSkills construction.
        """
        initial_skills: list = []

        # Load from local disk -- or take what preload_disk_skills() already loaded at
        # container start (consumed once, so a later create_plugin re-reads the disk).
        preloaded, self._preloaded_disk_skills = self._preloaded_disk_skills, None
        initial_skills.extend(preloaded if preloaded is not None else self._load_from_disk())

        # Load from S3
        if self.s3_bucket:
            initial_skills.extend(self._load_from_s3())

        if name_prefix:
            initial_skills = [s for s in initial_skills
                              if str(getattr(s, "name", "")).startswith(name_prefix)]

        self._shared_skills = list(initial_skills)
        self._plugin = AgentSkills(skills=list(initial_skills))
        return self._plugin

    def _bundle_references(self, skill: Skill, package_root: Optional[Path]) -> Skill:
        """Inline a skill's text reference/supporting files INTO its instructions so they reach
        the model in EVERY execution context — including detached ``plan_and_execute`` (DAG) nodes.

        Why: strands loads a skill's SKILL.md ``instructions`` on activation but exposes
        ``references/`` only as progressive-disclosure resource files read ON DEMAND from the
        skill ``path``. A DAG plan node has no skill-filesystem read wired and does not reliably
        perform those on-demand reads, so a reference the report step depends on (e.g.
        content-moderation's rating table) never arrives and the model improvises. Inlining rides
        the ONE channel that always reaches the model when a skill is active: the instructions.

        All-or-nothing per skill by a byte budget (``SKILL_BUNDLE_REFERENCE_MAX_BYTES``, default
        24KB): a skill whose text references FIT the budget has them ALL bundled (the common
        "authoritative reference" case); a skill whose references EXCEED it (e.g. video-processing's
        ~100KB of per-tool docs) keeps pure progressive disclosure (nothing bundled) so context
        never blows up. The ORIGINAL SKILL.md body is preserved for CRUD by :meth:`get_skill`
        (which re-parses the file), so a save round-trip never persists the bundle back into
        SKILL.md. Never raises — bundling must not break skill loading.
        """
        try:
            if package_root is None:
                return skill
            root = Path(package_root)
            if not root.exists():
                return skill
            files: list[tuple[str, Path]] = []
            total = 0
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                if p.name in ("SKILL.md", "tools.json") or "__pycache__" in p.parts:
                    continue
                if p.suffix.lower() not in _BUNDLE_TEXT_EXTS:
                    continue
                files.append((p.relative_to(root).as_posix(), p))
                total += p.stat().st_size
            if not files:
                return skill
            if total > _BUNDLE_MAX_BYTES:
                logger.info(
                    "skill '%s': %d bytes of references exceed bundle budget %d; keeping "
                    "progressive disclosure (references read on demand)",
                    skill.name, total, _BUNDLE_MAX_BYTES,
                )
                return skill
            blocks: list[str] = []
            for rel, p in files:
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001 - skip unreadable/binary-ish files
                    continue
                blocks.append(f"\n\n---\n### Bundled skill reference: `{rel}`\n\n{content}")
            if not blocks:
                return skill
            import dataclasses
            bundled = (
                "\n\n---\n\n## Bundled references\n\n"
                "This skill's `references/` files are inlined below so they are available in every "
                "execution context, including DAG plan nodes that cannot read them on demand."
                + "".join(blocks)
            )
            return dataclasses.replace(skill, instructions=(skill.instructions or "") + bundled)
        except Exception:  # noqa: BLE001 - bundling must never break skill loading
            logger.warning("skill '%s': reference bundling failed; loaded without bundle",
                           getattr(skill, "name", "?"), exc_info=True)
            return skill

    def _load_from_disk(self) -> list[Skill]:
        """Load skills from the local skills directory and bundled skills."""
        skills = []
        seen_dirs = set()

        # Check both the writable skills dir and the bundled (possibly read-only) dir
        for search_dir in [self.skills_dir, self._bundled_skills_dir]:
            if not search_dir.exists() or search_dir in seen_dirs:
                continue
            seen_dirs.add(search_dir)

            for skill_dir in search_dir.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    try:
                        skill = Skill.from_file(str(skill_dir))
                        # Skip if already loaded (writable dir takes precedence)
                        if skill.name in self._skill_source:
                            continue
                        skill = self._bundle_references(skill, skill_dir)
                        skills.append(skill)
                        self._skill_source[skill.name] = "local"
                        self._skill_package_root[skill.name] = skill_dir
                        tools_file = skill_dir / "tools.json"
                        if tools_file.exists():
                            self._skill_tools[skill.name] = json.loads(tools_file.read_text())
                        logger.info("Loaded skill from disk: %s", skill.name)
                    except Exception as e:
                        logger.warning("Failed to load skill from %s: %s", skill_dir, e)
        return skills

    def _load_from_s3(self) -> list[Skill]:
        """Load skills from S3 bucket.

        The S3 round trips (one SKILL.md per prefix, then each package's objects and its
        ``tools.json``) run on a small thread pool; every DECISION — local precedence, the
        first-prefix-wins rule for duplicate names, materialization and bookkeeping — is
        made sequentially in prefix order, so the result is the same as the one-at-a-time
        loop this replaced. Only the waiting overlaps (it was ~3s on a cold session).
        """
        skills = []
        s3 = self._get_s3()
        if not s3:
            return skills

        try:
            # List skill directories in S3
            response = s3.list_objects_v2(
                Bucket=self.s3_bucket,
                Prefix=self.s3_prefix,
                Delimiter="/",
            )

            prefixes = [p["Prefix"] for p in response.get("CommonPrefixes", [])]

            def _fetch_md(prefix: str):
                try:
                    obj = s3.get_object(Bucket=self.s3_bucket, Key=f"{prefix}SKILL.md")
                    return obj["Body"].read().decode("utf-8"), None
                except Exception as e:  # noqa: BLE001 - classified in prefix order below
                    return None, e

            def _fetch_package(prefix: str):
                """Every object under the prefix except SKILL.md (or the error), plus tools.json."""
                supporting: dict[str, bytes] = {}
                package_error = None
                try:
                    token = None
                    while True:
                        kwargs = {"Bucket": self.s3_bucket, "Prefix": prefix}
                        if token:
                            kwargs["ContinuationToken"] = token
                        page = s3.list_objects_v2(**kwargs)
                        for item in page.get("Contents", []):
                            key = item.get("Key", "")
                            rel = key[len(prefix):]
                            if not rel or rel == "SKILL.md" or rel.endswith("/"):
                                continue
                            supporting[rel] = s3.get_object(Bucket=self.s3_bucket, Key=key)["Body"].read()
                        if not page.get("IsTruncated"):
                            break
                        token = page.get("NextContinuationToken")
                except Exception as e:  # noqa: BLE001 - falls back to md-only below
                    package_error = e
                tools = None
                try:
                    tools_obj = s3.get_object(Bucket=self.s3_bucket, Key=f"{prefix}tools.json")
                    tools = json.loads(tools_obj["Body"].read().decode("utf-8"))
                except Exception:  # noqa: BLE001 - NoSuchKey or unreadable: no bindings
                    tools = None
                return supporting, package_error, tools

            from concurrent.futures import ThreadPoolExecutor

            workers = max(1, min(_S3_FETCH_PARALLEL, len(prefixes) or 1))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="skills-s3") as pool:
                md_results = list(pool.map(_fetch_md, prefixes))

                # Decide, in prefix order, which prefixes become skills.
                selected: list[tuple[str, str, str, str, str]] = []
                selected_names: set[str] = set()
                for prefix, (md_content, err) in zip(prefixes, md_results):
                    if err is not None:
                        no_such_key = getattr(getattr(s3, "exceptions", None), "NoSuchKey", None)
                        if isinstance(no_such_key, type) and isinstance(err, no_such_key):
                            continue
                        logger.warning("Failed to load skill from S3 %s: %s", prefix, err)
                        continue
                    try:
                        name, description, instructions = self._parse_skill_md(md_content)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Failed to load skill from S3 %s: %s", prefix, e)
                        continue
                    if not name:
                        continue
                    # Skip if already loaded (local takes precedence; first S3 prefix wins).
                    if name in self._skill_source or name in selected_names:
                        source = self._skill_source.get(name, "s3")
                        logger.info("Skipping S3 skill '%s' (already loaded from %s)", name, source)
                        continue
                    selected_names.add(name)
                    selected.append((prefix, md_content, name, description, instructions))

                packages = list(pool.map(_fetch_package, [sel[0] for sel in selected]))

            for (prefix, md_content, name, description, instructions), (supporting, package_error, tools) in zip(
                selected, packages
            ):
                # Download the WHOLE package (every object under the prefix except SKILL.md) so
                # references/supporting files come along, then materialize to a local package dir
                # (Skill.from_file → path + reference bundling), giving S3 skills PARITY with disk
                # skills. Fall back to a SKILL.md-only, path-less Skill if the package cannot be
                # assembled — S3 loading must never regress.
                skill = None
                package_root = None
                try:
                    if package_error is not None:
                        raise package_error
                    skill, package_root = self.materialize_runtime_skill_package(
                        name=name, skill_md=md_content, supporting_files=supporting, package_id=prefix,
                    )
                except Exception as e:  # noqa: BLE001 - never regress: fall back to md-only
                    logger.warning("S3 skill '%s': full-package load failed (%s); using SKILL.md only", name, e)
                    skill = Skill(name=name, description=description, instructions=instructions)
                    package_root = None

                skills.append(skill)
                self._skill_source[name] = "s3"
                self._skill_s3_prefix[name] = prefix
                if package_root is not None:
                    self._skill_package_root[name] = package_root
                if tools is not None:
                    self._skill_tools[name] = tools

                logger.info("Loaded skill from S3: %s (package=%s)", name, package_root is not None)

        except Exception as e:
            logger.error("Failed to list skills from S3: %s", e)

        return skills

    @staticmethod
    def _parse_skill_md(content: str) -> tuple[str, str, str]:
        """Parse a SKILL.md file with YAML frontmatter."""
        name, description, instructions = "", "", ""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()
                instructions = parts[2].strip()
                for line in frontmatter.split("\n"):
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip()
        return name, description, instructions

    def set_agent(self, agent: Agent) -> None:
        self._agent = agent

    @property
    def plugin(self) -> AgentSkills:
        if self._plugin is None:
            raise RuntimeError("Plugin not initialized. Call create_plugin() first.")
        return self._plugin

    def create_request_plugin(self, actor_skills: list[Skill]) -> AgentSkills:
        """Build an isolated plugin for one request without mutating global state."""
        actor_by_name: dict[str, Skill] = {}
        for skill in actor_skills:
            actor_by_name.setdefault(skill.name, skill)
        actor_names = set(actor_by_name)
        shared = [skill for skill in self._shared_skills if skill.name not in actor_names]
        return AgentSkills(skills=[*shared, *actor_by_name.values()])

    def load_bundled(self, name: str) -> Optional[Skill]:
        """Load one built-in skill from the bundled skills directory ONLY.

        Reads ``self._bundled_skills_dir`` and never the writable skills dir or S3, so a user
        skill registered under the same name cannot shadow the built-in copy (Req 1.17). The
        skill's SKILL.md body has :meth:`_bundle_references` applied exactly as on normal load,
        so the returned ``instructions`` are what the model would receive if the skill were
        activated. Returns ``None`` when the package is absent or cannot be parsed; never raises,
        so a searching turn can continue without the skill text.
        """
        try:
            skill_dir = self._bundled_skills_dir / name
            if not (skill_dir / "SKILL.md").exists():
                logger.info("load_bundled('%s'): no bundled package at %s", name, skill_dir)
                return None
            skill = Skill.from_file(str(skill_dir))
            return self._bundle_references(skill, skill_dir)
        except Exception:  # noqa: BLE001 - loading the built-in text must never break a turn
            logger.warning("load_bundled('%s') failed", name, exc_info=True)
            return None

    def list_skills(self) -> list[dict]:
        """Return metadata for all available skills."""
        skills = self.plugin.get_available_skills()
        activated = set()
        if self._agent:
            try:
                activated = set(self.plugin.get_activated_skills(self._agent))
            except Exception:
                pass

        return [
            {
                "name": s.name,
                "description": s.description,
                "activated": s.name in activated,
                "tools": self._skill_tools.get(s.name, []),
                "saved": self._is_saved(s.name),
                "dirty": s.name in self._dirty,
                "source": self._skill_source.get(s.name, "memory"),
            }
            for s in skills
        ]

    def _is_saved(self, skill_name: str) -> bool:
        """Check if a skill is persisted (locally or in S3)."""
        if (self.skills_dir / skill_name / "SKILL.md").exists():
            return True
        if self.s3_bucket and self._skill_source.get(skill_name) == "s3":
            return True
        return False

    def get_skill(self, skill_name: str) -> dict | None:
        for skill in self.plugin.get_available_skills():
            if skill.name != skill_name:
                continue

            source = self._skill_source.get(skill.name, "memory")
            skill_md = ""
            supporting_files: list[str] = []

            package_root = self._skill_package_root.get(skill.name)
            if package_root and package_root.exists():
                skill_file = package_root / "SKILL.md"
                if skill_file.exists():
                    skill_md = skill_file.read_text(encoding="utf-8")
                supporting_files = sorted(
                    path.relative_to(package_root).as_posix()
                    for path in package_root.rglob("*")
                    if path.is_file()
                    and path.name != "SKILL.md"
                    and "__pycache__" not in path.parts
                )
            elif source == "s3":
                prefix = self._skill_s3_prefix.get(skill.name)
                s3 = self._get_s3()
                if prefix and s3:
                    skill_key = f"{prefix}SKILL.md"
                    obj = s3.get_object(Bucket=self.s3_bucket, Key=skill_key)
                    skill_md = obj["Body"].read().decode("utf-8")
                    token = None
                    while True:
                        kwargs = {"Bucket": self.s3_bucket, "Prefix": prefix}
                        if token:
                            kwargs["ContinuationToken"] = token
                        page = s3.list_objects_v2(**kwargs)
                        for item in page.get("Contents", []):
                            key = item.get("Key", "")
                            if key and key != skill_key:
                                supporting_files.append(key[len(prefix):])
                        if not page.get("IsTruncated"):
                            break
                        token = page.get("NextContinuationToken")
                    supporting_files.sort()

            if not skill_md:
                description = json.dumps(skill.description or "")
                skill_md = (
                    f"---\nname: {skill.name}\ndescription: {description}\n---\n\n"
                    f"{skill.instructions}"
                )

            # Return the ORIGINAL SKILL.md body — NOT any bundled-reference augmentation applied to
            # skill.instructions at load time (see _bundle_references). skill_md is the source of
            # truth (read from disk/S3), so parsing it back guarantees save_skill round-trips
            # cleanly and the editor shows exactly what the author wrote.
            _, _, original_instructions = self._parse_skill_md(skill_md)
            instructions_out = original_instructions or skill.instructions

            return {
                "name": skill.name,
                "description": skill.description,
                "instructions": instructions_out,
                "skillMd": skill_md,
                "supportingFiles": supporting_files,
                "tools": self._skill_tools.get(skill.name, []),
                "source": source,
            }
        return None

    def save_skill(self, skill_name: str, target: str = "local") -> dict:
        """Persist a skill. Target can be 'local', 's3', or 'both'."""
        skill_data = self.get_skill(skill_name)
        if not skill_data:
            return {"error": f"Skill '{skill_name}' not found"}

        md = f"---\nname: {skill_data['name']}\ndescription: {skill_data['description']}\n---\n\n{skill_data['instructions']}"
        tools = skill_data.get("tools", [])
        results = {}

        if target in ("local", "both"):
            results["local"] = self._save_to_disk(skill_name, md, tools)

        if target in ("s3", "both"):
            results["s3"] = self._save_to_s3(skill_name, md, tools)

        self._dirty.discard(skill_name)
        if target == "s3" or target == "both":
            self._skill_source[skill_name] = "s3" if target == "s3" else self._skill_source.get(skill_name, "local")

        return {"status": "saved", "name": skill_name, "target": target, **results}

    def _save_to_disk(self, skill_name: str, md: str, tools: list) -> dict:
        skill_dir = self.skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(md)
        if tools:
            (skill_dir / "tools.json").write_text(json.dumps(tools))
        else:
            tools_file = skill_dir / "tools.json"
            if tools_file.exists():
                tools_file.unlink()
        self._skill_source[skill_name] = "local"
        self._skill_package_root[skill_name] = skill_dir
        self._skill_s3_prefix.pop(skill_name, None)
        logger.info("Saved skill to disk: %s", skill_name)
        return {"path": str(skill_dir)}

    def _save_to_s3(self, skill_name: str, md: str, tools: list) -> dict:
        s3 = self._get_s3()
        if not s3:
            return {"error": "S3 not configured"}

        prefix = f"{self.s3_prefix}{skill_name}/"
        s3.put_object(Bucket=self.s3_bucket, Key=f"{prefix}SKILL.md", Body=md.encode("utf-8"))
        if tools:
            s3.put_object(Bucket=self.s3_bucket, Key=f"{prefix}tools.json", Body=json.dumps(tools).encode("utf-8"))
        else:
            try:
                s3.delete_object(Bucket=self.s3_bucket, Key=f"{prefix}tools.json")
            except Exception:
                pass

        self._skill_source[skill_name] = "s3"
        self._skill_s3_prefix[skill_name] = prefix
        logger.info("Saved skill to S3: s3://%s/%s", self.s3_bucket, prefix)
        return {"s3_path": f"s3://{self.s3_bucket}/{prefix}"}

    def load_skill_from_directory(self, skill_path: str) -> dict:
        path = Path(skill_path)
        if not (path / "SKILL.md").exists():
            raise FileNotFoundError(f"No SKILL.md found in {path}")
        skill = Skill.from_file(str(path))
        current = self.plugin.get_available_skills()
        updated = [s for s in current if s.name != skill.name]
        updated.append(skill)
        self.plugin.set_available_skills(updated)
        self._skill_source[skill.name] = "local"
        self._skill_package_root[skill.name] = path
        self._skill_s3_prefix.pop(skill.name, None)
        logger.info("Loaded skill: %s", skill.name)
        return {"name": skill.name, "description": skill.description}

    def load_skill_programmatic(self, name: str, description: str, instructions: str, tools: list[str] | None = None) -> dict:
        if tools:
            tool_list = ", ".join(f"`{t}`" for t in tools)
            instructions = f"{instructions}\n\n## Available Tools\nWhen this skill is active, you have access to these tools: {tool_list}. Use them as needed to fulfill requests."
            self._skill_tools[name] = tools
        else:
            self._skill_tools.pop(name, None)

        skill = Skill(name=name, description=description, instructions=instructions)
        current = self.plugin.get_available_skills()
        updated = [s for s in current if s.name != skill.name]
        updated.append(skill)
        self.plugin.set_available_skills(updated)
        self._skill_source[name] = "memory"
        self._skill_package_root.pop(name, None)
        self._skill_s3_prefix.pop(name, None)
        self._dirty.add(name)
        logger.info("Loaded programmatic skill: %s", name)
        return {"name": skill.name, "description": skill.description}

    def materialize_runtime_skill_package(
        self,
        *,
        name: str,
        skill_md: str,
        supporting_files: dict[str, bytes],
        package_id: str,
    ) -> tuple[Skill, Path]:
        """Materialize one package and return its Skill without global mutation."""
        package_token = hashlib.sha256(package_id.encode("utf-8")).hexdigest()[:24]
        package_root = Path("/tmp/mediaworks-skill-packages") / package_token
        if package_root.exists():
            shutil.rmtree(package_root)
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "SKILL.md").write_text(skill_md, encoding="utf-8")

        try:
            for raw_path, content in supporting_files.items():
                relative = PurePosixPath(raw_path)
                if (
                    relative.is_absolute()
                    or raw_path == "SKILL.md"
                    or not relative.parts
                    or any(part in ("", ".", "..") for part in relative.parts)
                ):
                    raise ValueError(f"Unsafe Skill package path: {raw_path}")
                destination = package_root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

            skill = Skill.from_file(str(package_root))
            if skill.name != name:
                raise ValueError(
                    f"Runtime package name '{skill.name}' does not match expected name '{name}'"
                )
            skill = self._bundle_references(skill, package_root)
            return skill, package_root
        except Exception:
            shutil.rmtree(package_root, ignore_errors=True)
            raise

    @staticmethod
    def cleanup_runtime_package(package_root: Path) -> None:
        runtime_root = Path("/tmp/mediaworks-skill-packages")
        if runtime_root in package_root.parents:
            shutil.rmtree(package_root, ignore_errors=True)

    def load_skill_package_programmatic(
        self,
        *,
        name: str,
        skill_md: str,
        supporting_files: dict[str, bytes],
        package_id: str,
    ) -> dict:
        """Legacy global loader; request paths should use isolated plugins."""
        skill, package_root = self.materialize_runtime_skill_package(
            name=name,
            skill_md=skill_md,
            supporting_files=supporting_files,
            package_id=package_id,
        )
        current = self.plugin.get_available_skills()
        self.plugin.set_available_skills([s for s in current if s.name != name] + [skill])
        self._skill_tools.pop(name, None)
        self._skill_source[name] = "memory"
        self._skill_package_root[name] = package_root
        self._skill_s3_prefix.pop(name, None)
        self._dirty.add(name)
        logger.info("Loaded runtime Skill package: %s files=%d", name, len(supporting_files) + 1)
        return {"name": skill.name, "description": skill.description}

    def unload_skill(self, skill_name: str) -> bool:
        current = self.plugin.get_available_skills()
        updated = [s for s in current if s.name != skill_name]
        if len(updated) == len(current):
            return False
        self.plugin.set_available_skills(updated)
        self._skill_source.pop(skill_name, None)
        package_root = self._skill_package_root.pop(skill_name, None)
        runtime_root = Path("/tmp/mediaworks-skill-packages")
        if package_root and runtime_root in package_root.parents:
            shutil.rmtree(package_root, ignore_errors=True)
        self._skill_s3_prefix.pop(skill_name, None)
        self._dirty.discard(skill_name)
        logger.info("Unloaded skill: %s", skill_name)
        return True

    def reload_all(self) -> list[dict]:
        """Reload skills from both local disk and S3."""
        skills: list = []
        self._skill_source.clear()
        self._skill_tools.clear()
        self._skill_package_root.clear()
        self._skill_s3_prefix.clear()

        skills.extend(self._load_from_disk())
        if self.s3_bucket:
            skills.extend(self._load_from_s3())

        self._shared_skills = list(skills)
        self.plugin.set_available_skills(list(skills))
        self._dirty.clear()
        logger.info("Reloaded %d skills (local + S3)", len(skills))
        return self.list_skills()

    @property
    def s3_configured(self) -> bool:
        return bool(self.s3_bucket)
