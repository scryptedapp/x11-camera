import os
import pathlib
import platform
import sys


def get_pip_target() -> str:
    python_version = (
        "python%s" % str(sys.version_info[0]) + "." + str(sys.version_info[1])
    )
    python_versioned_directory = "%s-%s-%s" % (
        python_version,
        platform.system(),
        platform.machine(),
    )
    SCRYPTED_PYTHON_VERSION = os.environ.get("SCRYPTED_PYTHON_VERSION")
    python_versioned_directory += "-" + SCRYPTED_PYTHON_VERSION
    volume_dir = str(os.getenv("SCRYPTED_VOLUME") or pathlib.Path.home() / ".scrypted" / "volume")
    plugins_volume = str(pathlib.Path(volume_dir) / "plugins")
    plugin_volume = str(pathlib.Path(plugins_volume) / "@scrypted/x11-camera")

    pip_target = os.path.join(plugin_volume, python_versioned_directory)
    return pip_target

sys.path.append(get_pip_target())


import psutil


PIDFILE_DIR = os.getenv("SCRYPTED_X11_PIDFILE_DIR")


if __name__ == "__main__":
    proc_name = sys.argv[1].strip()

    # search for all files matching this proc
    for file in os.listdir(PIDFILE_DIR):
        if file.startswith(f"{proc_name}.") and file.endswith(".pid"):
            pidfile = os.path.join(PIDFILE_DIR, file)
            try:
                with open(pidfile) as f:
                    pid = int(f.read())
                    p = psutil.Process(pid)
                    for child in p.children(recursive=True):
                        if child.name() == proc_name or child.name() == f"{proc_name}.exe":
                            child.kill()
                    p.kill()
            except:
                pass
            finally:
                try:
                    os.remove(pidfile)
                except:
                    pass
                try:
                    print(f"{proc_name} stopped")
                except:
                    pass