"""Skills subpackage — install/delete/display for `.claude/skills/`.

Public API re-exported here so cli.py can do a single
`from .skills import install_skills, ...` instead of reaching into submodules.
"""
from .display import fmt_custom_entry, read_skill_description, render_skills_table
from .install import (
    copy_custom_skills,
    copy_manual_skills,
    copy_skill_dir,
    delete_project_skills,
    install_skills,
    prune_installed_skills,
)
from .skills_sh import install_skills_sh, verify_skill_md

__all__ = [
    "copy_custom_skills",
    "copy_manual_skills",
    "copy_skill_dir",
    "delete_project_skills",
    "fmt_custom_entry",
    "install_skills",
    "install_skills_sh",
    "prune_installed_skills",
    "read_skill_description",
    "render_skills_table",
    "verify_skill_md",
]
