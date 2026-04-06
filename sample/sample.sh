# Can also add --prune to delete intermediate layers
uv sync && \
sudo .venv/bin/fastcontainer build -q /disk/fastcontainer ./sample/sample.yaml -p default

