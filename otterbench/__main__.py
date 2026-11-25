import sys
import warnings

# pyvisa-py warns about optional backends that aren't installed.
warnings.filterwarnings("ignore", module=r"pyvisa_py(\..*)?")


def run() -> int:
    from .about import safe_output
    safe_output()
    from .gui import main
    return main()


if __name__ == "__main__":
    sys.exit(run())
