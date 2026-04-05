# fastcontainer/fastcontainer/__main__.py
"""
Entry point so you can run the package directly with:

    python -m fastcontainer.fastcontainer /disk/containers ./prepare.yaml
"""

from .cli import main


if __name__ == "__main__":
    main()
