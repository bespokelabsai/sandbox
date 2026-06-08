# PyTorch (CPU build) with torchvision and torchaudio.
FROM python:3.11-slim

# CPU-only wheels from the PyTorch index keep the image small and avoid
# pulling CUDA, which sandboxes don't have a GPU for by default.
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch torchvision torchaudio
