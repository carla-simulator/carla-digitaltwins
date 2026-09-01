"""``python -m twinmodel ...`` -> :func:`twinmodel.cli.main`."""
import sys

from .cli import main

sys.exit(main())
