# Deprecated

Files moved out of the live tree on 2026-08-25. Nothing here is referenced by
running code — each item was checked against `.py` imports, `config.yaml`
dotted paths, `setup.sh`, and the docs before being moved.

Structure mirrors the original layout, so anything can be restored by moving it
back along the same relative path.

| Path | Why |
|---|---|
| `services/tts copy.py` | Broken draft of `services/tts.py` — every definition is accidentally indented one level inside the import block. Zero references. |
| `services/sentence_chunker copy.py` | Pre-edit snapshot of `services/sentence_chunker.py` containing "AFTER (proposed …)" comments; those changes were merged into the real file. Lacks the sanitizer integration the live version has. |
| `tools/test_stremio_shrinking.py` | Undocumented one-off (every sibling script is documented in CLAUDE.md). Its assertion — `_launch_uri` called once despite multiple provider failures — is now covered by `tests/test_stremio_service.py:360-376`. |
| `download_jarvis.py` | Hardcoded output path is missing the `- codex` segment of the repo root, so it would write to the wrong directory. Fetches a wake-word model that is no longer the active backend. |
| `models/en/en_GB/jarvis/high/jarvis-high.onnx(.json)` | Orphaned wake-word asset. The active model is the Porcupine `.ppn` referenced at `config.yaml:12`. |
| `models/.cache/huggingface/` | `hf_hub_download` cache bookkeeping left behind by `download_jarvis.py`. |
| `sounds/activate/` | Superseded duplicate (2 files) of `sounds/california_activations/` (48 files), which is what `core/audio_pipeline.py:45` actually loads. |
| `debug/surfshark/<11 dated folders>` | 14 MB of screenshots from one calibration session on 2026-03-12. The conclusions are recorded in CLAUDE.md ("Current Surfshark Route Calibration") and `surfshark_routes.json`. |

## Not moved, deliberately

- **`debug/surfshark/` itself** stays in the live tree — `config.yaml:288` sets it
  as `surfshark_debug_capture_dir`, the live capture root for
  `tools/debug_surfshark_sequence.py`. Only the dated subfolders were moved.
- **`models/en_GB-alan-medium.onnx`** stays — referenced by `config.yaml` as the
  Piper TTS model.

## Considered and kept

These looked unreferenced but were judged still useful:

- `generate_activation_phrases.py`, `generate_bootup_sounds.py` — asset *source*
  scripts. Being uncalled by code is normal for a generator; removing them would
  discard the ability to regenerate the sound sets.
- `California_en_raspberry-pi_v4_0_0.ppn` — unreferenced only because config
  points at the Windows variant. The Pi is the stated deploy target.
- `setup.sh` — absent from the docs tree, but it is the Linux/Pi bootstrap path.
- `debug/test_groq_connection.py` — undocumented, but functional and covers a
  live credential/network path that unit tests cannot.

## Note on recoverability

Only the Python files were ever tracked by git, and of those, only
`services/tts copy.py`, `services/sentence_chunker copy.py`, and
`download_jarvis.py` — `tools/test_stremio_shrinking.py` was untracked.

Everything else here has **no git history and cannot be restored** if this folder
is deleted: `.gitignore` covers `*.wav`, `*.onnx`, and `models/`, and the entire
`debug/` tree was never committed. Treat the screenshots and model files as the
only copies that exist.
