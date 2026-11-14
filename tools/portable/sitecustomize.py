"""Installed only into the private runtime of a Windows folder build."""
import json
import os
import sys


def configure():
    root = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    manifest = os.path.join(root, "portable-build.json")
    if not os.path.isfile(manifest):
        return
    data = os.path.join(root, "portable-data")
    model = os.path.join(root, "models")
    hub = os.path.join(model, "hub")
    temp = os.path.join(data, "temp")
    try:
        os.makedirs(temp, exist_ok=True)
    except OSError:
        # CPython normally continues after sitecustomize import errors.
        # Exit before a dependency can fall back to the user's global caches.
        raise SystemExit("Emiya Voice needs a writable folder. Move the whole folder and try again.")
    env = {
        "HF_HOME": model, "HF_HUB_CACHE": hub, "HUGGINGFACE_HUB_CACHE": hub,
        "HF_ASSETS_CACHE": os.path.join(model, "assets"), "HF_XET_CACHE": os.path.join(model, "xet"),
        "HF_MODULES_CACHE": os.path.join(model, "modules"), "HF_TOKEN_PATH": os.path.join(model, "token"),
        "HF_HUB_DISABLE_TELEMETRY": "1", "PKUSEG_HOME": os.path.join(model, "pkuseg"),
        "XDG_CACHE_HOME": os.path.join(data, "cache"), "TORCH_HOME": os.path.join(data, "torch"),
        "TORCH_EXTENSIONS_DIR": os.path.join(data, "extensions"),
        "TORCHINDUCTOR_CACHE_DIR": os.path.join(data, "inductor"),
        "NUMBA_CACHE_DIR": os.path.join(data, "numba"), "CUDA_CACHE_PATH": os.path.join(data, "cuda"),
        "LIBROSA_DATA_DIR": os.path.join(data, "librosa"),
        "MPLCONFIGDIR": os.path.join(data, "matplotlib"), "TEMP": temp, "TMP": temp, "TMPDIR": temp,
    }
    with open(manifest, encoding="utf-8") as file:
        if json.load(file)["model_revisions"]:
            env["HF_HUB_OFFLINE"] = "1"
    os.environ.update(env)
    # pywebview's private-mode temp directory is created under the process-local TEMP above.
    # A machine-level user-data-folder override would otherwise take precedence over it.
    os.environ.pop("WEBVIEW2_USER_DATA_FOLDER", None)
    sys.dont_write_bytecode = True


configure()
