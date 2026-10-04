# CLIP weights for materials_hub image search (P3)

Place `ViT-B-32.pt` in this directory (~350MB), or set `VITUAL_CLIP_WEIGHTS` to the file path.

Download (OpenAI CLIP ViT-B/32):

```bash
# cwd = materials_hub/models/clip
curl -L -o ViT-B-32.pt "https://openaipublic.azureedge.net/clip/models/40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af/ViT-B-32.pt"
```

Or via socks5 if needed:

```bash
curl --socks5-hostname 127.0.0.1:10808 -L -o ViT-B-32.pt "https://openaipublic.azureedge.net/clip/models/40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af/ViT-B-32.pt"
```

Requires `subtitle_pipeline/.venv` with `open_clip_torch` (and torch). Then:

```bash
cd materials_hub
python cli.py imgembed --status          # probe
python cli.py imgembed --limit 20        # build index
python cli.py imgsearch --text "kitchen" # text→image
python cli.py imgsearch <id> --mode clip # image→image
```

Force online download through open_clip (not recommended if proxy flaky):

```bash
set VITUAL_CLIP_ALLOW_DOWNLOAD=1
python cli.py imgembed --status
```
