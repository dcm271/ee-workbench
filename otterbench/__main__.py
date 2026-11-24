import sys
import warnings

# pyvisa-py warns about optional backends that aren't installed.
warnings.filterwarnings("ignore", module=r"pyvisa_py(\..*)?")

from .gui import main  # noqa: E402

sys.exit(main())
