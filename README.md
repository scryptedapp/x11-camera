# X11 Virtual Camera

This plugin allows for the creation of virtual camera devices that stream output from X11 virtual desktop environments. Each device created by this plugin is a virtual display running a configurable terminal program under `xterm`. Terminal sizes and fonts can be configured for each virtual display. Several dependency packages are required to set up the virtual camera devices, listed below.

For Scrypted installs on **Docker** or **LXC**, the required dependencies are the same as on local Linux, however will be installed automatically at plugin launch.

For local Scrypted installs on **Linux**, several system packages must be manually installed: `xvfb`, `xterm`, `xfonts-base`. The optional dependency `fontconfig` can be installed to enable changing fonts.

For local Scrypted installs on **MacOS**, several brew packages must be manually installed: `xquartz`, `gnu-getopt`, `ffmpeg`.

For local Scrypted installs on **Windows**, Cygwin will be automatically installed to handle the virtual X11 display.

## Advanced usage: Hardware-accelerated encoding

By default, this plugin requests that the Rebroadcast plugin use the FFmpeg arguments `-c:v libx264 -preset ultrafast -bf 0 -r 15 -g 60` for encoding H264 video from the virtual X11 display (`libopenh264` is used on Windows instead of `libx264`). To enable hardware acceleration, copy the above into the "FFmpeg Output Prefix" settings for the stream, replacing `libx264` with the hardware-accelerated encoder for your platform. Note that for Windows, the encoder must be one supported within Cygwin.