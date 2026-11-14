"""Assemble an offline Windows one-folder distribution from this project's installed venv."""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "tools" / "portable"
ALL_MODELS = {
    "ResembleAI/chatterbox": [
        "Cangjie5_TC.json", "conds.pt", "grapheme_mtl_merged_expanded_v1.json", "s3gen.pt",
        "s3gen.safetensors", "t3_cfg.safetensors", "t3_mtl23ls_v2.safetensors",
        "tokenizer.json", "ve.pt", "ve.safetensors",
    ],
    "ResembleAI/chatterbox-turbo": [
        "added_tokens.json", "conds.pt", "merges.txt", "s3gen_meanflow.safetensors",
        "special_tokens_map.json", "t3_turbo_v1.safetensors", "tokenizer_config.json",
        "ve.safetensors", "vocab.json",
    ],
}


def copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def runtime_packages() -> list[metadata.Distribution]:
    names = {"chatterbox-tts", "setuptools"}  # editable source and pkg_resources loader
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.partition("#")[0].strip()
        if not line or line.startswith("-e"):
            continue
        name = re.match(r"^[A-Za-z0-9_.-]+", line).group()
        dist = metadata.distribution(name)
        if "==" in line and dist.version != line.partition("==")[2]:
            raise ValueError(f"{name} installed at {dist.version}, expected {line}")
        names.add(name)
    return [metadata.distribution(name) for name in sorted(names)]


def copy_runtime(out: Path) -> list[str]:
    base = Path(sys.base_prefix)
    site = Path(sys.prefix) / "Lib" / "site-packages"
    dest = out / "runtime"
    for item in ("python.exe", "pythonw.exe", "python3.dll", "python311.dll", "LICENSE.txt",
                 "vcruntime140.dll", "vcruntime140_1.dll"):
        copy(base / item, dest / item)
    shutil.copytree(base / "DLLs", dest / "DLLs")
    shutil.copytree(base / "Lib", dest / "Lib",
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "test",
                                                  "idlelib", "tkinter", "turtledemo", "ensurepip"))
    (dest / "python311._pth").write_text(
        "Lib\nDLLs\nLib/site-packages\n..\nimport site\n", encoding="ascii")
    installed = []
    for dist in runtime_packages():
        name = dist.metadata["Name"]
        installed.append(f"{name}=={dist.version}")
        if not dist.files:
            raise ValueError(f"No wheel file list for {name}")
        for member in dist.files:
            rel = Path(str(member))
            # Only installed files owned by a pinned package. Drop pip's generated launchers,
            # editable host-root pointers, download provenance and the developer's bytecode.
            if (rel.is_absolute() or ".." in rel.parts or "__pycache__" in rel.parts
                    or rel.suffix.lower() in {".pyc", ".pth"} or rel.name in {"direct_url.json", "RECORD"}):
                continue
            src = site / rel
            if src.is_file():
                copy(src, dest / "Lib" / "site-packages" / rel)
    sources = ROOT / "chatterbox" / "src" / "chatterbox"
    shutil.copytree(sources, dest / "Lib" / "site-packages" / "chatterbox",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    copy(SUPPORT / "sitecustomize.py", dest / "Lib" / "sitecustomize.py")
    # Installed binary wheels own e.g. torch/lib/*.dll, their data files and license text.
    return installed


def copy_public_models(out: Path, hub: Path, pkuseg: Path) -> dict[str, str]:
    revisions = {}
    for repo, filenames in ALL_MODELS.items():
        cache_name = "models--" + repo.replace("/", "--")
        source = hub / cache_name
        revision = (source / "refs" / "main").read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError(f"Invalid local main revision for {repo}")
        revisions[repo] = revision
        dest = out / "models" / "hub" / cache_name
        copy(source / "refs" / "main", dest / "refs" / "main")
        snapshot = Path("snapshots") / revision
        for filename in filenames:
            copy(source / snapshot / filename, dest / snapshot / filename)
        if repo == "ResembleAI/chatterbox":
            # The multilingual tokenizer passes its snapshot as a nested HF cache_dir.
            nested = dest / snapshot / cache_name
            copy(source / "refs" / "main", nested / "refs" / "main")
            copy(source / snapshot / "Cangjie5_TC.json", nested / snapshot / "Cangjie5_TC.json")
    # spacy_pkuseg checks the downloaded zip even when the extracted model exists.
    for name in ("spacy_ontonotes.zip", "spacy_ontonotes/features.msgpack", "spacy_ontonotes/weights.npz"):
        copy(pkuseg / name, out / "models" / "pkuseg" / name)
    return revisions


def application_files() -> dict[Path, Path]:
    files = {}
    for filename in ("gui.py", "cli.py", "app.py", "config.py", "LICENSE"):
        files[Path(filename)] = ROOT / filename
    for module in ("ui", "engine"):
        for source in (ROOT / module).rglob("*"):
            rel = source.relative_to(ROOT)
            if ("tests" not in rel.parts and source.is_file()
                    and source.suffix in {".py", ".js", ".css", ".toml", ".ico", ".gif", ".png"}):
                files[rel] = source
    files[Path("licenses/Chatterbox.txt")] = ROOT / "chatterbox" / "LICENSE"
    files[Path("START HERE.md")] = ROOT / "docs" / "portable.md"
    files[Path("portable_check.py")] = SUPPORT / "check.py"
    return files


def copy_application(out: Path) -> None:
    for rel, source in application_files().items():
        copy(source, out / rel)


def build_launcher(out: Path) -> None:
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    icon = ROOT / "ui/themes/emiya/static/icon.ico"
    subprocess.run([str(csc), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
                    "/r:System.Windows.Forms.dll", f"/win32icon:{icon}",
                    f"/out:{out / 'Emiya Voice.exe'}", str(SUPPORT / "Launcher.cs")], check=True)


def build(out: Path, hub: Path, pkuseg: Path, include_models: bool) -> None:
    if os.name != "nt" or sys.version_info[:2] != (3, 11) or sys.maxsize < 2**32 or sys.prefix == sys.base_prefix:
        raise ValueError("Build with the project's Windows x64 Python 3.11 venv.")
    if out.exists():
        raise ValueError(f"Choose a new folder. Destination already exists: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.with_name(out.name + ".building-" + uuid.uuid4().hex[:8])
    stage.mkdir()
    print(f"Assembling into {stage}", flush=True)
    try:
        packages = copy_runtime(stage)
        models = copy_public_models(stage, hub, pkuseg) if include_models else {}
        copy_application(stage)
        build_launcher(stage)
        manifest = {"python": sys.version.split()[0], "platform": "win-x64",
                    "packages": packages, "model_revisions": models}
        (stage / "portable-build.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        stage.rename(out)
    except BaseException:
        print(f"Incomplete build left at {stage}", file=sys.stderr)
        raise
    print(f"Ready: {out / 'Emiya Voice.exe'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "Emiya Voice")
    parser.add_argument("--without-models", action="store_true", help="Build an empty model library")
    parser.add_argument("--hub", type=Path, default=Path(os.environ.get("HF_HUB_CACHE",
                        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")))
    parser.add_argument("--pkuseg", type=Path, default=Path(os.environ.get("PKUSEG_HOME", Path.home() / ".pkuseg")))
    args = parser.parse_args()
    build(args.out.resolve(), args.hub, args.pkuseg, not args.without_models)
