#!/usr/bin/env python3
"""
Download a pre-built OpenWakeWord model OR train a custom one for "hey mechus".

QUICK START — download a pre-built model (no PyTorch needed, <1 minute):
    python3 scripts/train_wake_model.py --download --out models/hey_mycroft.onnx

Available pre-built wake phrases (set --model-name):
    hey_mycroft   →  say "hey mycroft"   (default)
    alexa         →  say "alexa"
    hey_rhasspy   →  say "hey rhasspy"
    hey_jarvis    →  say "hey jarvis"

CUSTOM "hey mechus" model (requires PyTorch, takes 5-15 minutes):
    pip install torch torchinfo
    python3 scripts/train_wake_model.py --phrase "hey mechus" --out models/hey_mechus.onnx

After running, activate in docker-compose.zoom.yml:
    - MECHUS_WAKE_BACKEND=openwakeword
    - MECHUS_WAKE_MODEL_PATH=/app/models/<model_filename>
    - MECHUS_WAKE_PHRASE=<the phrase you chose>
    Then: ./run.sh deploy
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Get an OpenWakeWord model for Mechus")
    parser.add_argument("--phrase", default="hey mechus", help="Custom wake phrase (for custom training only)")
    parser.add_argument("--out", default="models/hey_mechus.onnx", help="Output model path")
    parser.add_argument("--download", action="store_true", help="Download a pre-built model (fast, no PyTorch)")
    parser.add_argument("--model-name", default="hey_mycroft", help="Pre-built model name (default: hey_mycroft)")
    parser.add_argument("--n-samples", type=int, default=2000, help="Synthetic training samples (custom only, default: 2000)")
    args = parser.parse_args()

    try:
        import openwakeword  # noqa: F401
    except ImportError:
        print("ERROR: openwakeword is not installed.")
        print("Run: pip install openwakeword")
        sys.exit(1)

    out_dir = os.path.dirname(args.out) if os.path.dirname(args.out) else "."
    os.makedirs(out_dir, exist_ok=True)

    if args.download:
        phrase_hint = _download_prebuilt(args.model_name, args.out)
    else:
        _train_custom(args.phrase, args.out, args.n_samples)
        phrase_hint = args.phrase

    print()
    print("=" * 60)
    print("Done! Add these to docker-compose.zoom.yml environment:")
    print(f"  - MECHUS_WAKE_BACKEND=openwakeword")
    print(f"  - MECHUS_WAKE_MODEL_PATH=/app/models/{os.path.basename(args.out)}")
    print(f"  - MECHUS_WAKE_PHRASE={phrase_hint}")
    print()
    print("Also add to Dockerfile.zoom (before the CMD line):")
    print(f"  COPY models/{os.path.basename(args.out)} /app/models/{os.path.basename(args.out)}")
    print()
    print("Then run: ./run.sh deploy")
    print("=" * 60)


def _download_prebuilt(model_name: str, out_path: str) -> str:
    """Download a pre-built OpenWakeWord model using the library's own downloader."""
    import openwakeword
    from openwakeword.utils import download_models  # type: ignore

    # Validate model name
    available = list(openwakeword.MODELS.keys())
    if model_name not in available:
        print(f"Unknown model '{model_name}'. Available: {', '.join(available)}")
        sys.exit(1)

    print(f"Downloading pre-built model: {model_name}")
    print("This will take ~30 seconds...")
    download_models([model_name])

    # Find the downloaded .onnx file
    models_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
    src = None
    for f in os.listdir(models_dir):
        if model_name in f and f.endswith(".onnx"):
            src = os.path.join(models_dir, f)
            break

    if not src:
        print(f"ERROR: Downloaded model .onnx not found in {models_dir}")
        print(f"Files present: {os.listdir(models_dir)}")
        sys.exit(1)

    if os.path.abspath(src) != os.path.abspath(out_path):
        shutil.copy2(src, out_path)
    print(f"Model saved to: {out_path}")

    # Return the actual phrase to use (strip version suffix)
    phrase_map = {
        "hey_mycroft": "hey mycroft",
        "alexa": "alexa",
        "hey_rhasspy": "hey rhasspy",
        "hey_jarvis": "hey jarvis",
    }
    return phrase_map.get(model_name, model_name.replace("_", " "))


def _train_custom(phrase: str, out_path: str, n_samples: int) -> None:
    """Train a custom wake word model using TTS-generated synthetic data."""
    try:
        import torch  # noqa: F401
        import torchinfo  # noqa: F401
    except ImportError as e:
        print(f"ERROR: Custom training requires PyTorch and torchinfo: {e}")
        print()
        print("Install them first:")
        print("  pip install torch torchinfo")
        print()
        print("Or download a pre-built model instead (no PyTorch needed):")
        print(f"  python3 {sys.argv[0]} --download --out {out_path}")
        sys.exit(1)

    print(f"Training custom model for phrase: '{phrase}'")
    print(f"Samples: {n_samples} (takes 5-15 minutes on CPU)")
    print()

    from openwakeword.train import train  # type: ignore
    out_dir = os.path.dirname(out_path) if os.path.dirname(out_path) else "."
    train(target_phrase=phrase, output_dir=out_dir, n_samples=n_samples)

    trained_name = phrase.replace(" ", "_") + ".onnx"
    trained_path = os.path.join(out_dir, trained_name)
    if os.path.exists(trained_path) and os.path.abspath(trained_path) != os.path.abspath(out_path):
        os.rename(trained_path, out_path)
    print(f"Model saved to: {out_path}")


if __name__ == "__main__":
    main()
