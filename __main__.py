import sys
import warnings

# pyvisa-py / gpib-ctypes warn about optional backends that aren't installed.
warnings.filterwarnings("ignore", module=r"(gpib_ctypes|pyvisa_py)(\..*)?")

from .gui import main  # noqa: E402

sys.exit(main())
