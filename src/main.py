import asyncio
import hashlib
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
from typing import Any, Dict, Tuple
import urllib.request
import uuid

import scrypted_sdk
from scrypted_sdk import ScryptedDeviceBase, VideoCamera, ResponseMediaStreamOptions, RequestMediaStreamOptions, Settings, Setting, ScryptedInterface, ScryptedDeviceType, DeviceProvider, DeviceCreator, DeviceCreatorSettings, Readme, TTYSettings


def linux_data_home() -> str:
    if 'XDG_DATA_HOME' in os.environ:
        return os.environ['XDG_DATA_HOME']
    return os.path.expanduser('~/.local/share')


async def run_and_stream_output(cmd: str, env: Dict[str, str] = {}, return_pid: bool = False) -> Tuple[asyncio.Future, int]:
    p = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=dict(os.environ, **env))

    async def read_streams():
        async def stream_stdout():
            async for line in p.stdout:
                print(line.decode('utf-8'))
        async def stream_stderr():
            async for line in p.stderr:
                print(line.decode('utf-8'))

        await asyncio.gather(stream_stdout(), stream_stderr(), p.wait())

    if return_pid:
        return (asyncio.ensure_future(read_streams()), p.pid)
    await read_streams()


async def run_self_cleanup_subprocess(cmd: str, env: Dict[str, str] = {}, kill_proc: str = None, proc_id: str = None) -> None:
    """Launch an instance of Python which monitors the subprocess and kills it if the parent process dies."""
    exe = sys.executable

    if platform.system() == 'Windows':
        cmd = f"\"{X11CameraPlugin.CYGWIN_LAUNCHER}\" \"{cmd}\""

    args = [
        "-u",
        "-S",
        X11CameraPlugin.RUN_SEPARATELY_SCRIPT,
        cmd,
        json.dumps(env),
        kill_proc or 'None',
        proc_id or 'None',
        X11CameraPlugin.MONITOR_FILE if platform.system() == 'Windows' else 'None'
    ]

    script_env = os.environ.copy()
    script_env['SCRYPTED_X11_PIDFILE_DIR'] = X11CameraPlugin.VOLUME_FILES
    p = await asyncio.create_subprocess_exec(exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, env=script_env)

    async def read_streams():
        async def stream_stdout():
            async for line in p.stdout:
                print(line.decode('utf-8'))
        async def stream_stderr():
            async for line in p.stderr:
                print(line.decode('utf-8'))

        await asyncio.gather(stream_stdout(), stream_stderr(), p.wait())

    await read_streams()


async def run_cleanup_subprocess(kill_proc: str) -> None:
    """Launches an instance of Python to clean up dangling processes from a previous plugin instance."""
    exe = sys.executable
    args = [
        "-u",
        "-S",
        X11CameraPlugin.CLEANUP_SEPARATELY_SCRIPT,
        kill_proc
    ]

    script_env = os.environ.copy()
    script_env['SCRYPTED_X11_PIDFILE_DIR'] = X11CameraPlugin.VOLUME_FILES
    p = await asyncio.create_subprocess_exec(exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, env=script_env)

    async def read_streams():
        async def stream_stdout():
            async for line in p.stdout:
                print(line.decode('utf-8'))
        async def stream_stderr():
            async for line in p.stderr:
                print(line.decode('utf-8'))

        await asyncio.gather(stream_stdout(), stream_stderr(), p.wait())

    await read_streams()


def copy_file_to(path: str, dest: str, make_executable: bool = False) -> None:
    if platform.system() != "Windows":
        shutil.copyfile(path, dest)
        if make_executable:
            os.chmod(dest, 0o755)
    else:
        # read file
        with open(path, 'rb') as f:
            data = f.read()

        # launch tee as subprocess
        subprocess.Popen(f'"{X11CameraPlugin.CYGWIN_LAUNCHER}" "tee {dest}"', stdin=subprocess.PIPE, stdout=subprocess.PIPE, shell=True).communicate(data)

        if make_executable:
            # make executable
            subprocess.Popen(f'"{X11CameraPlugin.CYGWIN_LAUNCHER}" "chmod 755 {dest}"', shell=True).communicate()


async def get_extra_paths() -> list[str]:
    extra_paths = []
    state = scrypted_sdk.systemManager.getSystemState()
    for id in state.keys():
        device: TTYSettings = scrypted_sdk.systemManager.getDeviceById(id)
        try:
            tty_settings = await device.getTTYSettings()
            extra_paths.extend(tty_settings.get('paths', []))
        except:
            pass
    return extra_paths


class X11Camera(ScryptedDeviceBase, VideoCamera, Settings):
    def __init__(self, nativeId: str, parent: 'X11CameraPlugin'):
        super().__init__(nativeId)  # nativeId: <uuid>-<initial display num>
        self.parent = parent
        self.stream_initialized = asyncio.ensure_future(self.init_stream())

    async def init_stream(self) -> bool:
        await self.parent.initialized

        if not self.exe:
            self.print("Terminal executable not configured, stream will not start.")
            return False

        fontmanager = await self.parent.getDevice('fontmanager')
        await fontmanager.fonts_loaded

        async def run_stream():
            await asyncio.sleep(3)

            exe = self.exe
            env = {
                "LANG": "en_US.UTF-8",
            }
            xterm_tweaks = ""

            extra_paths = await get_extra_paths()
            args = self.args

            if platform.system() == "Windows":
                exe = subprocess.check_output([X11CameraPlugin.CYGWIN_LAUNCHER, f"cygpath '{exe}'"]).decode().strip()
                exe = f"'{exe}'"
                xterm_tweaks = f"+tb +sb -fullscreen -geometry {self.display_dimensions}"

            path = os.environ.get('PATH', '')
            if platform.system() == 'Darwin':
                path = f'/opt/X11/bin:/opt/homebrew/opt/gnu-getopt/bin:/usr/local/opt/gnu-getopt/bin:{path}'
            for extra_path in extra_paths:
                if platform.system() == "Windows":
                    extra_path = subprocess.check_output([X11CameraPlugin.CYGWIN_LAUNCHER, f"cygpath '{extra_path}'"]).decode().strip()
                path = f'{extra_path}:{path}'
            if path.endswith(':'):
                path = path[:-1]
            env['PATH'] = path

            if platform.system() == 'Linux':
                env['XDG_DATA_HOME'] = linux_data_home()

            fontselection = ''
            if fontmanager.fonts_supported:
                font = self.xterm_font
                if font not in fontmanager.list_fonts():
                    font = 'Default'
                if font != 'Default':
                    fontselection = f'-fa \'{font}\''

            crash_count = 0
            display_num = self.virtual_display_num
            while True:
                subprocess_task = asyncio.create_task(
                    run_self_cleanup_subprocess(f'{X11CameraPlugin.XVFB_RUN} -n {display_num} -s \'-screen 0 {self.display_dimensions}x24\' -f {X11CameraPlugin.XAUTH}{display_num} xterm {xterm_tweaks} {fontselection} -en UTF-8 -maximized -e {exe} {args}',
                                                env=env, kill_proc='Xvfb', proc_id=f"{display_num}")
                )
                sleep_task = asyncio.create_task(asyncio.sleep(15))

                done, pending = await asyncio.wait([subprocess_task, sleep_task], return_when=asyncio.FIRST_COMPLETED)
                if sleep_task in done and subprocess_task in pending:
                    self.print(f"Xvfb {display_num} appears to be running")
                    crash_count = 0
                    await subprocess_task

                crash_count += 1
                if crash_count > 5:
                    self.print(f"Xvfb {display_num} could not start for {crash_count} times, requesting full plugin restart...")
                    await scrypted_sdk.deviceManager.requestRestart()
                    await asyncio.sleep(3600)

                self.print(f"Xvfb {display_num} crashed, restarting in 5s...")
                await asyncio.sleep(5)

        asyncio.create_task(run_stream())
        return True

    @property
    def exe(self) -> str:
        if self.storage:
            return self.storage.getItem('exe') or ''
        return ''

    @property
    def args(self) -> str:
        if self.storage:
            return self.storage.getItem('args') or ''
        return ''

    @property
    def virtual_display_num(self) -> int:
        if self.storage:
            return self.storage.getItem('virtual_display_num') or int(self.nativeId.split('-')[-1])
        return int(self.nativeId.split('-')[-1])

    @property
    def display_dimensions(self) -> str:
        if self.storage:
            return self.storage.getItem('display_dimensions') or '1024x720'
        return '1024x720'

    @property
    def xterm_font(self) -> str:
        """For best results, ensure that FontManager.fonts_loaded is awaited before calling this property."""
        if self.storage:
            return self.storage.getItem('xterm_font') or 'Default'
        return 'Default'

    async def getSettings(self) -> list[Setting]:
        settings = [
            {
                "key": "exe",
                "title": "Executable",
                "description": "The executable to run in the virtual display. Absolute paths only.",
                "type": "string",
                "value": self.exe,
            },
            {
                "key": "args",
                "title": "Arguments",
                "description": "The arguments to pass to the executable.",
                "type": "string",
                "value": self.args,
            },
            {
                "key": "display_dimensions",
                "title": "Virtual Display Dimensions",
                "description": "The X11 virtual display dimensions to use. Format: WIDTHxHEIGHT.",
                "type": "string",
                "value": self.display_dimensions,
            },
            {
                "key": "virtual_display_num",
                "title": "Virtual Display Number",
                "description": "The X11 virtual display number to use.",
                "type": "number",
                "value": self.virtual_display_num,
            },
        ]

        fontmanager = await self.parent.getDevice('fontmanager')
        if fontmanager.fonts_supported:
            await fontmanager.fonts_loaded
            settings.append({
                "key": "xterm_font",
                "title": "Xterm Font",
                "description": "The Xterm font to use. Monospace fonts are recommended. Download additional fonts in the font manager page.",
                "type": "string",
                "value": self.xterm_font,
                "choices": fontmanager.list_fonts(),
            })

        return settings

    async def putSetting(self, key: str, value: str) -> None:
        if key == "x11_restart":
            # private setting intended for use by plugins that package apps
            print("Another plugin requested restart...")
            await scrypted_sdk.deviceManager.requestRestart()
            return

        self.storage.setItem(key, value)
        await self.onDeviceEvent(ScryptedInterface.Settings.value, None)
        self.print("Settings updated, will restart...")
        await scrypted_sdk.deviceManager.requestRestart()

    async def getVideoStreamOptions(self) -> list[ResponseMediaStreamOptions]:
        return [
            {
                "id": "default",
                "name": "Virtual Display",
                "container": "x11grab",
                "video": {
                    "codec": "rawvideo",
                },
                "audio": None,
                "source": "synthetic",
                "tool": "ffmpeg",
                "userConfigurable": False,
            }
        ]

    async def getVideoStream(self, options: RequestMediaStreamOptions = None) -> scrypted_sdk.MediaObject:
        if not await self.stream_initialized:
            raise Exception("Terminal executable not configured, stream cannot start")

        display_num = self.virtual_display_num
        ffmpeg_input = {
            "inputArguments": [
                "-f", "x11grab",
                "-framerate", "15",
                "-draw_mouse", "0",
                "-i", f":{display_num}",
            ],
            "env": {
                "XAUTHORITY": X11CameraPlugin.XAUTH + str(display_num),
            },
            "h264EncoderArguments": [
                "-c:v", "libx264" if platform.system() != "Windows" else "libopenh264",
                "-preset", "ultrafast",
                "-bf", "0",
                "-r", "15",
                "-g", "60",
            ]
        }

        if platform.system() == 'Darwin':
            if os.path.exists('/opt/homebrew/bin/ffmpeg'):
                ffmpeg_input['ffmpegPath'] = '/opt/homebrew/bin/ffmpeg'
            elif os.path.exists('/usr/local/bin/ffmpeg'):
                ffmpeg_input['ffmpegPath'] = '/usr/local/bin/ffmpeg'
        elif platform.system() == 'Windows':
            ffmpeg_input['ffmpegPath'] = await self.parent.cygwin_ffmpeg

        return await scrypted_sdk.mediaManager.createFFmpegMediaObject(ffmpeg_input)


class X11CameraPlugin(ScryptedDeviceBase, DeviceProvider, DeviceCreator):
    VOLUME_FILES = os.path.join(os.environ['SCRYPTED_PLUGIN_VOLUME'], 'files')
    CYGWIN_INSTALL_DONE = os.path.join(VOLUME_FILES, 'cygwin_install_done')
    CYGWIN_PORTABLE_INSTALLER = os.path.join(VOLUME_FILES, 'cygwin-portable-installer.cmd')
    CYGWIN_LAUNCHER = os.path.join(VOLUME_FILES, 'cygwin-portable.cmd')
    MONITOR_FILE = os.path.join(VOLUME_FILES, f"monitor.{os.getpid()}")

    FILES = "/tmp/.scrypted_x11" if platform.system() == "Windows" else VOLUME_FILES
    XAUTH = f"{FILES}/Xauthority"
    XVFB_RUN = f"{FILES}/xvfb-run"

    CYGWIN_PORTABLE_INSTALLER_SRC = os.path.join(os.environ['SCRYPTED_PLUGIN_VOLUME'], 'zip', 'unzipped', 'fs', 'cygwin-portable-installer.cmd')
    XVFB_RUN_SRC = os.path.join(os.environ['SCRYPTED_PLUGIN_VOLUME'], 'zip', 'unzipped', 'fs', 'xvfb-run')
    RUN_SEPARATELY_SCRIPT = os.path.join(os.environ['SCRYPTED_PLUGIN_VOLUME'], 'zip', 'unzipped', 'run_separately.py')
    CLEANUP_SEPARATELY_SCRIPT = os.path.join(os.environ['SCRYPTED_PLUGIN_VOLUME'], 'zip', 'unzipped', 'cleanup_separately.py')

    def __init__(self, nativeId: str = None) -> None:
        super().__init__(nativeId)

        self.fontmanager = None
        self.cameras = {}
        self.initialized = asyncio.ensure_future(self.initialize())
        self.cygwin_ffmpeg = asyncio.ensure_future(self.get_cygwin_ffmpeg())

    async def get_logger(self) -> Any:
        return await scrypted_sdk.systemManager.api.getLogger(self.nativeId)

    async def alert(self, msg) -> None:
        logger = await self.get_logger()
        await logger.log('a', msg)

    async def initialize(self) -> None:
        await self.discover_devices()

        try:
            os.makedirs(X11CameraPlugin.VOLUME_FILES, exist_ok=True)

            installation = os.environ.get('SCRYPTED_INSTALL_ENVIRONMENT')
            if installation in ('docker', 'lxc', 'lxc-docker'):
                await run_and_stream_output('apt-get update')
                await run_and_stream_output('apt-get install -y xvfb xterm xfonts-base fontconfig')
            elif platform.system() == 'Windows':
                shutil.copyfile(X11CameraPlugin.CYGWIN_PORTABLE_INSTALLER_SRC, X11CameraPlugin.CYGWIN_PORTABLE_INSTALLER)

                with open(X11CameraPlugin.CYGWIN_PORTABLE_INSTALLER, 'r') as f:
                    data = f.read()
                installer_md5 = hashlib.md5(data.encode()).hexdigest()
                needs_install = True
                try:
                    with open(X11CameraPlugin.CYGWIN_INSTALL_DONE, 'r') as f:
                        if f.read() == installer_md5:
                            needs_install = False
                except:
                    pass

                if needs_install:
                    await run_and_stream_output(f'"{X11CameraPlugin.CYGWIN_PORTABLE_INSTALLER}"')
                    with open(X11CameraPlugin.CYGWIN_INSTALL_DONE, 'w') as f:
                        f.write(installer_md5)
            else:
                if platform.system() == 'Linux':
                    needed = []
                    if shutil.which('Xvfb') is None:
                        needed.append('xvfb')
                    if shutil.which('xterm') is None:
                        needed.append('xterm')
                        needed.append('xfonts-base')

                    if needed:
                        needed.sort()
                        await self.alert(f"Please manually install the following and restart the plugin: {needed}")
                        raise Exception(f"Please manually install the following and restart the plugin: {needed}")
                elif platform.system() == 'Darwin':
                    needed = []
                    if not os.path.exists('/usr/local/bin/ffmpeg') and \
                        not os.path.exists('/opt/homebrew/bin/ffmpeg'):
                        needed.append('ffmpeg')
                    if shutil.which('xterm') is None and not os.path.exists('/opt/X11/bin/xterm'):
                        needed.append('xquartz')
                    if not os.path.exists('/opt/homebrew/opt/gnu-getopt/bin/getopt') and \
                        not os.path.exists('/usr/local/opt/gnu-getopt/bin/getopt'):
                        needed.append('gnu-getopt')

                    if needed:
                        needed.sort()
                        await self.alert(f"Please manually install the following and restart the plugin: {needed}")
                        raise Exception(f"Please manually install the following and restart the plugin: {needed}")
                else:
                    raise Exception("This plugin only supports Linux, MacOS, and Windows.")

            try:
                await run_cleanup_subprocess('Xvfb')
            except:
                import traceback
                traceback.print_exc()
                pass
            if platform.system() == "Windows":
                try:
                    await run_cleanup_subprocess('cygserver')
                except:
                    pass

            if platform.system() != "Windows":
                pathlib.Path(X11CameraPlugin.XAUTH).unlink(missing_ok=True)
                pathlib.Path(X11CameraPlugin.FILES).mkdir(parents=True, exist_ok=True)
            else:
                subprocess.Popen(f'"{X11CameraPlugin.CYGWIN_LAUNCHER}" "rm -rf {X11CameraPlugin.FILES}"', shell=True).communicate()
                subprocess.Popen(f'"{X11CameraPlugin.CYGWIN_LAUNCHER}" "mkdir -p {X11CameraPlugin.FILES}"', shell=True).communicate()
            copy_file_to(X11CameraPlugin.XVFB_RUN_SRC, X11CameraPlugin.XVFB_RUN, make_executable=True)

            if platform.system() == "Windows":
                # clean up old monitors
                try:
                    for file in os.listdir(X11CameraPlugin.VOLUME_FILES):
                        if file.startswith('monitor.'):
                            os.remove(os.path.join(X11CameraPlugin.VOLUME_FILES, file))
                except:
                    pass

                async def periodic_monitor(proc):
                    while True:
                        try:
                            with open(X11CameraPlugin.MONITOR_FILE+f".{proc}", 'w') as f:
                                f.write('')
                        except:
                            pass
                        await asyncio.sleep(3)
                asyncio.create_task(periodic_monitor('Xvfb'))
                asyncio.create_task(periodic_monitor('cygserver'))

                async def run_cygserver():
                    await run_and_stream_output(f'"{X11CameraPlugin.CYGWIN_LAUNCHER}" "cygserver-config -n"')
                    while True:
                        await run_self_cleanup_subprocess('/usr/sbin/cygserver', kill_proc='cygserver')
                        print("cygserver crashed, restarting in 5s...")
                        await asyncio.sleep(5)
                asyncio.create_task(run_cygserver())
        except:
            import traceback
            traceback.print_exc()
            await asyncio.sleep(3600)
            os._exit(1)

    async def discover_devices(self) -> None:
        await scrypted_sdk.deviceManager.onDeviceDiscovered({
            "nativeId": "fontmanager",
            "name": "Font Manager",
            "type": ScryptedDeviceType.API.value,
            "interfaces": [
                ScryptedInterface.Settings.value,
                ScryptedInterface.Readme.value,
            ],
        })

    async def get_cygwin_ffmpeg(self) -> str:
        assert platform.system() == 'Windows'
        await self.initialized
        return subprocess.check_output([X11CameraPlugin.CYGWIN_LAUNCHER, "cygpath -w $(which ffmpeg)"]).decode().strip()

    async def get_next_virtual_display_num(self) -> int:
        await self.initialized
        saved = self.storage.getItem('next_virtual_display_num')
        if saved:
            self.storage.setItem('next_virtual_display_num', str(int(saved) + 1))
            return int(saved)
        self.storage.setItem('next_virtual_display_num', '101')
        return 100

    async def getDevice(self, nativeId: str) -> Any:
        await self.initialized
        if nativeId == 'fontmanager':
            if not self.fontmanager:
                self.fontmanager = FontManager(nativeId, self)
            return self.fontmanager
        else:
            if nativeId not in self.cameras:
                self.cameras[nativeId] = X11Camera(nativeId, self)
            return self.cameras[nativeId]

    async def createDevice(self, settings: DeviceCreatorSettings) -> str:
        nativeId = f"{str(uuid.uuid4().hex)}-{await self.get_next_virtual_display_num()}"
        name = settings.get("name", "New X11 Virtual Camera")
        await scrypted_sdk.deviceManager.onDeviceDiscovered({
            'nativeId': nativeId,
            'name': name,
            'interfaces': [
                ScryptedInterface.VideoCamera.value,
                ScryptedInterface.Settings.value,
            ],
            'type': ScryptedDeviceType.Camera.value,
        })
        await self.getDevice(nativeId)
        return nativeId

    async def getCreateDeviceSettings(self) -> list[Setting]:
        return [
            {
                'title': 'Virtual Camera Name',
                'key': 'name',
            }
        ]


class DownloaderBase(ScryptedDeviceBase):
    def __init__(self, nativeId: str = None):
        super().__init__(nativeId)

    def downloadFile(self, url: str, filename: str):
        try:
            filesPath = os.path.join(os.environ['SCRYPTED_PLUGIN_VOLUME'], 'files')
            fullpath = os.path.join(filesPath, filename)
            srcPath = os.path.join(filesPath, filename + '.src')
            if os.path.isfile(srcPath):
                with open(srcPath, 'r') as f:
                    src = f.read()
                if src == url and os.path.isfile(fullpath):
                    return fullpath
            tmp = fullpath + '.tmp'
            self.print("Creating directory for", tmp)
            os.makedirs(os.path.dirname(fullpath), exist_ok=True)
            self.print("Downloading", url)
            response = urllib.request.urlopen(url)
            if response.getcode() is not None and response.getcode() < 200 or response.getcode() >= 300:
                raise Exception(f"Error downloading")
            read = 0
            with open(tmp, "wb") as f:
                while True:
                    data = response.read(1024 * 1024)
                    if not data:
                        break
                    read += len(data)
                    self.print("Downloaded", read, "bytes")
                    f.write(data)
            os.rename(tmp, fullpath)
            with open(srcPath, 'w') as f:
                f.write(url)
            return fullpath
        except:
            self.print("Error downloading", url)
            import traceback
            traceback.print_exc()
            raise


class FontManager(DownloaderBase, Settings, Readme):
    FONT_DIR_PATTERN = os.path.join(linux_data_home(), 'fonts') if platform.system() == 'Linux' else '~/.fonts'
    LOCAL_FONT_DIR = os.path.expanduser(FONT_DIR_PATTERN)
    CYGWIN_FONT_DIR = '~/.local/share/fonts'

    def __init__(self, nativeId: str, parent: X11CameraPlugin):
        super().__init__(nativeId)
        self.parent = parent
        self.fonts_cache = None
        self.fonts_loaded = asyncio.ensure_future(self.load_fonts())

    @property
    def fonts_supported(self) -> bool:
        installation = os.environ.get('SCRYPTED_INSTALL_ENVIRONMENT')
        if installation in ('docker', 'lxc', 'lxc-docker'):
            return True
        if platform.system() == 'Linux':
            return shutil.which('fc-list') is not None
        if platform.system() == 'Darwin':
            return os.path.exists('/opt/X11/bin/fc-list')
        if platform.system() == 'Windows':
            try:
                subprocess.check_output([X11CameraPlugin.CYGWIN_LAUNCHER, "which fc-list"]).decode().strip()
                return True
            except:
                pass
        return False

    def list_fonts(self) -> list[str]:
        """For best results, ensure that FontManager.fonts_loaded is awaited before calling this function."""
        if not self.fonts_supported:
            return []

        if self.fonts_cache is not None:
            return self.fonts_cache

        fonts = []

        env = os.environ.copy()
        if platform.system() == 'Linux':
            env['XDG_DATA_HOME'] = linux_data_home()

        fc_list_cmd = [X11CameraPlugin.CYGWIN_LAUNCHER, 'fc-list : family'] if platform.system() == 'Windows' else \
            ['fc-list' if platform.system() == 'Linux' else '/opt/X11/bin/fc-list', ':', 'family']
        try:
            # list font families with fc-list
            out = subprocess.check_output(fc_list_cmd, env=env).decode().strip()
            for line in out.splitlines():
                font = line.strip()
                if font:
                    fonts.append(font)
        except:
            self.print("Could not enumerate fonts with fc-list")
            pass
        fonts.sort()
        fonts = ['Default'] + fonts
        self.fonts_cache = fonts
        return fonts

    async def load_fonts(self) -> None:
        await self.parent.initialized
        if platform.system() == 'Windows':
            subprocess.Popen(f'"{X11CameraPlugin.CYGWIN_LAUNCHER}" "mkdir -p {FontManager.CYGWIN_FONT_DIR}"', shell=True).communicate()
        else:
            os.makedirs(FontManager.LOCAL_FONT_DIR, exist_ok=True)
        try:
            urls = self.font_urls
            for url in urls:
                filename = url.split('/')[-1]
                fullpath = self.downloadFile(url, filename)
                if platform.system() == 'Windows':
                    target = f"{FontManager.CYGWIN_FONT_DIR}/{filename}"
                    copy_file_to(fullpath, target)
                else:
                    target = os.path.join(FontManager.LOCAL_FONT_DIR, filename)
                    shutil.copyfile(fullpath, target)
                if await self.validate_font(target):
                    self.print("Installed", target)
                else:
                    self.print("Could not validate", target)
        except:
            import traceback
            traceback.print_exc()

    async def validate_font(self, path) -> bool:
        fc_list_cmd = [X11CameraPlugin.CYGWIN_LAUNCHER, f'fc-validate {path}'] if platform.system() == 'Windows' else \
            ['fc-validate' if platform.system() == 'Linux' else '/opt/X11/bin/fc-validate', path]
        try:
            process = await asyncio.create_subprocess_exec(*fc_list_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            stdout = stdout.decode().strip()
            stderr = stderr.decode().strip()
            if stdout:
                self.print(stdout)
            if stderr:
                self.print(stderr)
            if process.returncode != 0:
                return False
            return True
        except:
            return False

    @property
    def font_urls(self) -> list[str]:
        if self.storage:
            urls = self.storage.getItem('font_urls')
            if urls:
                return json.loads(urls)
        return []

    async def getSettings(self) -> list[Setting]:
        await self.parent.initialized
        if not self.fonts_supported:
            return []

        return [
            {
                "key": "font_urls",
                "title": "Font URLs",
                "description": f"List of URLs to download fonts from. Fonts will be downloaded to {FontManager.CYGWIN_FONT_DIR if platform.system() == 'Windows' else FontManager.LOCAL_FONT_DIR}.",
                "value": self.font_urls,
                "multiple": True,
            },
        ]

    async def putSetting(self, key: str, value: str) -> None:
        self.storage.setItem(key, json.dumps(value))
        await self.onDeviceEvent(ScryptedInterface.Settings.value, None)
        self.print("Settings updated, will restart...")
        await scrypted_sdk.deviceManager.requestRestart()

    async def getReadmeMarkdown(self) -> str:
        await self.parent.initialized
        if not self.fonts_supported:
            return f"""
# Font Manager

Fonts are not supported on this platform.
"""

        fontdir = FontManager.CYGWIN_FONT_DIR if platform.system() == 'Windows' else FontManager.LOCAL_FONT_DIR
        return f"""
# Font Manager

List fonts to download and install in the local font directory. Fonts will be installed to `{fontdir}`.
"""


def create_scrypted_plugin():
    return X11CameraPlugin()