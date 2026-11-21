"""README content: Extension_Parameters and Artifact_References are documented.

Requirements 14.2 and 14.4. The expected items are derived from the code and from ``dev.yaml``,
so adding a parameter or a function without documenting it fails here and names the gap.
"""

from pathlib import Path

from tokenization_infra.core.config import CONFIG_DIR, REPO_ROOT, load_stage
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names

README = REPO_ROOT / "README.md"
SENTINEL_PROJECT = "sentinelproject"
SENTINEL_STAGE = "sentinelstage"


def _readme_text(path: Path = README) -> str:
    return path.read_text(encoding="utf-8")


def _parameter_suffixes() -> dict[ExtensionKey, str]:
    prefix = f"/{SENTINEL_PROJECT}/{SENTINEL_STAGE}/"
    names = extension_parameter_names(SENTINEL_PROJECT, SENTINEL_STAGE)
    assert all(name.startswith(prefix) for name in names.values())
    return {key: name.removeprefix(prefix) for key, name in names.items()}


def _artifact_key_paths() -> list[str]:
    cfg = load_stage("dev", CONFIG_DIR)
    return [f"functions.{fn.value}.artifact" for fn in cfg.functions]


def test_readme_documents_every_extension_parameter_suffix() -> None:
    """Requirement 14.2: every suffix from ``extension_parameter_names`` appears in the README."""
    text = _readme_text()
    suffixes = _parameter_suffixes()
    assert len(suffixes) == len(ExtensionKey)

    missing = sorted(
        f"{key.name} ({suffix})" for key, suffix in suffixes.items() if suffix not in text
    )

    assert not missing, f"README.md is missing Extension_Parameter suffixes: {missing}"


def test_readme_documents_every_artifact_reference_key_path() -> None:
    """Requirement 14.4: every ``functions.<id>.artifact`` key path appears in the README."""
    text = _readme_text()
    key_paths = _artifact_key_paths()
    assert key_paths, "dev.yaml defines no functions"

    missing = [path for path in key_paths if path not in text]

    assert not missing, f"README.md is missing Artifact_Reference key paths: {missing}"
