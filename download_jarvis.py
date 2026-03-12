import subprocess
import sys

# Install huggingface_hub if needed
subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub"])

from huggingface_hub import hf_hub_download

output_dir = r"C:\Users\Migue\Downloads\california-project\california\models"

files = [
    "en/en_GB/jarvis/high/jarvis-high.onnx",
    "en/en_GB/jarvis/high/jarvis-high.onnx.json",
]

for filepath in files:
    filename = filepath.split("/")[-1]
    print(f"Downloading {filename}...")
    path = hf_hub_download(
        repo_id="jgkawell/jarvis",
        filename=filepath,
        local_dir=output_dir,
        local_dir_use_symlinks=False,
    )
    print(f"Saved: {path}")

print("Done!")