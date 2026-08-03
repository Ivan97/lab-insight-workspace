"""Agent Skills discovered from the skill directory.

Skills are folders holding a SKILL.md with YAML frontmatter, per the Agent
Skills specification. Discovery, progressive disclosure and frontmatter parsing
come from deepagents' SkillsMiddleware rather than being reimplemented here.

SkillsMiddleware only advertises skills; reading a skill body needs the
filesystem tools, so FilesystemMiddleware is mounted alongside it. Those tools
are rooted at the skill directory, which keeps file reads, writes and deletes
inside it.

`execute` is deliberately part of the default tool set: a skill that ships
scripts has to be able to run them, which is the point of skills depending on
the host OS. It is not sandboxed -- rooting the backend constrains the file
tools' paths, not what a shell command can reach -- so it can be turned off
with SKILL_EXECUTE_ENABLED=false where that trade is not wanted.
"""

import os
from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware

from .config import ROOT_DIR

# Reading and searching are always available; a skill body cannot be loaded
# without them.
READ_ONLY_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})
WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete"})
EXECUTE_TOOL = "execute"


def skill_dir() -> Path:
    override = os.getenv("SKILL_DIR")
    return Path(override) if override else ROOT_DIR / "skill"


def execute_enabled() -> bool:
    return os.getenv("SKILL_EXECUTE_ENABLED", "true").strip().lower() not in {
        "false",
        "0",
        "no",
    }


def allowed_tool_names() -> frozenset[str]:
    allowed = READ_ONLY_TOOLS | WRITE_TOOLS
    return allowed | {EXECUTE_TOOL} if execute_enabled() else allowed


def discovered_skills() -> list[str]:
    """Skill names on disk, for the health endpoint. Cheap directory scan."""
    root = skill_dir()
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir() if (child / "SKILL.md").is_file())


def skill_middleware() -> list[object]:
    """Middleware granting the agent its skills, or nothing if none exist.

    Mounting the filesystem tools when there is no skill directory would hand
    the model file access it has no reason to hold.
    """
    root = skill_dir()
    if not root.is_dir() or not discovered_skills():
        return []
    backend = FilesystemBackend(root_dir=str(root))
    filesystem = FilesystemMiddleware(backend=backend)
    allowed = allowed_tool_names()
    filesystem.tools = [tool for tool in filesystem.tools if tool.name in allowed]
    return [filesystem, SkillsMiddleware(backend=backend, sources=["./"])]
