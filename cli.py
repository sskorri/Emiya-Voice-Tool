#!/usr/bin/env python3
"""Command-line interface for the voice tool.

    python cli.py load <voice_id> <reference.wav> [--backend NAME] [--exaggeration 0.5] [--force]
    python cli.py speak <voice_id> "<text>" [--out outputs/x.wav] [--language en] [--cfg-weight 0.5]
    python cli.py list
    python cli.py info <voice_id>
    python cli.py backends

`load` and `speak` exit 1 with a one-line message when a voice or option is invalid
(ValueError), another program holds the voice engine (EngineBusy) or the model fails to
load (BackendLoadError).
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import config
from engine.engine import VoiceEngine
from engine.errors import BackendLoadError, EngineBusy


def cmd_load(args, engine):
    ref = Path(args.wav)
    if not ref.exists():
        sys.exit(f"Reference clip not found: {ref}")
    try:
        engine.load_voice(
            args.voice_id,
            ref,
            backend=args.backend,
            exaggeration=args.exaggeration,
            language=args.language,
            force=args.force,
        )
    except (ValueError, EngineBusy, BackendLoadError) as e:
        sys.exit(str(e))
    verb = "Re-registered" if args.force else "Registered"
    print(f"{verb} '{args.voice_id}' on backend '{args.backend or config.DEFAULT_BACKEND}'")


def cmd_speak(args, engine):
    try:
        wav = engine.speak(
            args.text,
            args.voice_id,
            language_id=args.language,
            cfg_weight=args.cfg_weight,
        )
    except (ValueError, EngineBusy, BackendLoadError) as e:
        sys.exit(str(e))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(wav)
    print(f"Wrote {out} ({len(wav):,} bytes)")


def cmd_list(args, engine):
    voices = engine.list_voices()
    if not voices:
        print("No voices registered yet. Run: python cli.py load <name> <wav>")
        return
    for v in voices:
        info = engine.voice_info(v)
        backend = info["backend"] if info else "?"
        print(f"  {v}  [{backend}]")


def cmd_info(args, engine):
    info = engine.voice_info(args.voice_id)
    if info is None:
        sys.exit(f"Voice '{args.voice_id}' is not registered.")
    print(json.dumps({args.voice_id: info}, indent=2))


def cmd_backends(args, engine):
    selectable = engine.list_backends()
    loaded = engine.list_loaded_backends()
    print("Selectable backends (loaded on demand):")
    for name in selectable:
        marks = []
        if name == config.DEFAULT_BACKEND:
            marks.append("default")
        if name in loaded:
            marks.append("resident")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        print(f"  {name}{suffix}")


def build_parser():
    parser = argparse.ArgumentParser(description="Voice tool CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="Register a voice from a reference clip")
    p_load.add_argument("voice_id", help="Name for this voice, e.g. 'friend'")
    p_load.add_argument("wav", help="Path to the reference WAV clip")
    p_load.add_argument(
        "--backend",
        choices=list(config.ENABLED_BACKENDS),
        default=None,
        help=f"Which model to use (default: {config.DEFAULT_BACKEND})",
    )
    p_load.add_argument("--exaggeration", type=float, default=config.DEFAULT_EXAGGERATION)
    p_load.add_argument("--language", default=config.DEFAULT_LANGUAGE)
    p_load.add_argument(
        "--force", action="store_true",
        help="Re-bake conditionals even if a cached .pt already exists "
             "(use after swapping in a new reference clip)",
    )
    p_load.set_defaults(func=cmd_load)

    p_speak = sub.add_parser("speak", help="Generate speech with a registered voice")
    p_speak.add_argument("voice_id")
    p_speak.add_argument("text")
    p_speak.add_argument("--out", default=str(config.OUTPUTS_DIR / "output.wav"))
    p_speak.add_argument("--language", default=None,
                         help="Language for this generation (default: the voice's registered language)")
    p_speak.add_argument("--cfg-weight", dest="cfg_weight", type=float, default=config.DEFAULT_CFG_WEIGHT)
    p_speak.set_defaults(func=cmd_speak)

    p_list = sub.add_parser("list", help="List registered voices")
    p_list.set_defaults(func=cmd_list)

    p_info = sub.add_parser("info", help="Show stored config for a registered voice")
    p_info.add_argument("voice_id")
    p_info.set_defaults(func=cmd_info)

    p_backends = sub.add_parser("backends", help="List which backends loaded this session")
    p_backends.set_defaults(func=cmd_backends)

    return parser


def main():
    # The engine's warnings and notes ("Unloading backend ... to free VRAM.") print on stderr as plain lines.
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
    logging.getLogger("engine").setLevel(logging.INFO)
    args = build_parser().parse_args()
    engine = VoiceEngine(config)
    args.func(args, engine)


if __name__ == "__main__":
    main()
