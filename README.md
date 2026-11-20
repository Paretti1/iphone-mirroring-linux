# iPhone Mirroring Linux

Mirror your iPhone in a compact desktop window on Fedora and other Linux distributions. Standard AirPlay brings the screen and sound over Wi-Fi. Optional control lets you click, swipe, scroll, and type from your computer.

| Mode | Video and audio | Mouse and keyboard |
| --- | --- | --- |
| AirPlay | Wi-Fi from Control Center, with sound | Optional USB control through a signed WebDriverAgent runner |
| AirPlay with an existing WebDriverAgent | Wi-Fi from Control Center, with sound | Wi-Fi while your explicitly selected WebDriverAgent is running |
| Developer Wi-Fi viewer | Direct Wi-Fi in the desktop app | Direct Wi-Fi with iOS 27+, Developer Mode, and one-time pairing |

The desktop AirPlay window includes rotation, a floating pin, draggable resize edges and corners, remembered geometry where the desktop supports it, screenshots, mute, and a single running instance. The phone image stays in proportion inside the resized window. Its design follows the upstream project's compact phone-shaped window. The separate developer Wi-Fi viewer uses iOS's developer display and input services. Apple’s [macOS iPhone Mirroring](https://support.apple.com/en-us/120421) uses its own Apple Account integration. AirPlay on Linux requires you to start Screen Mirroring on the iPhone, and pauses when the iPhone locks.

## Install on Linux

Use Python 3.11 or newer. Install the packages for your distribution from the list below, then run the installer from a regular user account. The installer adds **iPhone Mirroring Linux** to your application menu and puts the terminal commands in `~/.local/bin` by default.

### Fedora Workstation and other RPM-based Fedora editions

```bash
sudo dnf install git python3 python3-pip portaudio libxcb xcb-util-cursor \
  xcb-util-image xcb-util-keysyms xcb-util-renderutil xcb-util-wm \
  libxkbcommon-x11 libX11-xcb xorg-x11-server-Xwayland xdg-user-dirs
```

For wired control, also install `usbmuxd`. For the optional VNC viewer, install `tigervnc`. Fedora Workstation normally has the PipeWire ALSA compatibility package available as `pipewire-alsa`.

### Fedora Silverblue and Kinoite

Run the same source installer on the host for desktop integration. If a native library is missing, layer only the needed package on the host with `rpm-ostree install` and reboot. For wired control, layer `usbmuxd` on the host so it can see the physical phone. A default Toolbox container is suitable for development and the loopback test. For regular mirroring, run the application in your desktop session on the host.

### Debian 12+, Ubuntu 24.04+, and recent Linux Mint

```bash
sudo apt install git python3 python3-venv libportaudio2 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
  libxcb-xkb1 libxkbcommon-x11-0 libx11-xcb1 xwayland xdg-user-dirs
```

For wired control, install `usbmuxd`. For VNC, install `tigervnc-viewer`.

### Arch Linux and EndeavourOS

```bash
sudo pacman -S --needed git python python-pip portaudio libxcb xcb-util-cursor \
  xcb-util-image xcb-util-keysyms xcb-util-renderutil xcb-util-wm \
  libxkbcommon-x11 libx11 xorg-xwayland xdg-user-dirs
```

For wired control, install `usbmuxd`. For VNC, install `tigervnc`.

### Download and launch

You can clone or download this public repository without a GitHub account. To clone it, run the following commands after installing the packages for your distribution.

```bash
git clone https://github.com/Paretti1/iphone-mirroring-linux.git
cd iphone-mirroring-linux
./install.sh --control
```

For a ZIP download, choose **Code → Download ZIP** or [download the ZIP directly](https://github.com/Paretti1/iphone-mirroring-linux/archive/refs/heads/main.zip). Extract the ZIP, open a terminal in the extracted folder, and run `./install.sh --control`.

Use `./install.sh` for AirPlay mirroring alone. `--control` and `--usb` both install the optional dependencies for Wi-Fi and USB input. To update a Git clone, run `git pull` and rerun the installer. For a ZIP installation, download and extract a fresh copy, then rerun the installer from that folder. If Python package wheels are unavailable for your distribution’s default interpreter, select an installed Python 3.12 or 3.13, for example `PYTHON=python3.12 ./install.sh --control`. Run the commands by full path under `~/.local/bin` if your shell does not include that directory.

## AirPlay quick start

1. Launch **iPhone Mirroring Linux** from your application menu.
2. Connect the iPhone and PC to the same trusted network. A PC on wired Ethernet works if it shares the iPhone’s local network.
3. On the unlocked iPhone, open **Control Center → Screen Mirroring** and choose the name displayed in the desktop window.
4. To disconnect, select **Stop Mirroring** on the iPhone.

The app opens an AirPlay receiver on the LAN. Use a trusted network or restrict it to one interface with `iphone-mirroring-linux --bind PC_IPV4_ADDRESS`. If VPNs, guest Wi-Fi, or client isolation block discovery, connect both devices to the same local network where they can reach each other.

### Fedora firewall

The default ports are UDP **5353** for mDNS, TCP **7000–7001** for AirPlay control/video, and UDP **7002–7004** for AirPlay timing/audio. `--port N` moves the five AirPlay ports to `N` through `N+4`. If firewalld blocks discovery or streaming, scope rules to the active zone for your trusted network and the iPhone’s current IPv4 address. These example rules are runtime-only. Replace `home` and the example address with your own values.

```bash
sudo firewall-cmd --get-active-zones
ZONE=home
PHONE_IP=192.168.1.23
sudo firewall-cmd --zone="$ZONE" --add-service=mdns
sudo firewall-cmd --zone="$ZONE" --add-rich-rule="rule family=ipv4 source address=$PHONE_IP port port=7000-7001 protocol=tcp accept"
sudo firewall-cmd --zone="$ZONE" --add-rich-rule="rule family=ipv4 source address=$PHONE_IP port port=7002-7004 protocol=udp accept"
```

For one-time wireless developer pairing, the phone also needs TCP **7005** to reach the Linux computer. If necessary, temporarily allow that port from the phone.

```bash
sudo firewall-cmd --zone="$ZONE" --add-rich-rule="rule family=ipv4 source address=$PHONE_IP port port=7005 protocol=tcp accept" --timeout=10m
```

The desktop app uses a password-protected local connection for its Wi-Fi viewer. The optional web and VNC viewers also listen on loopback.

## Wireless control on iOS 27+

This is the path for scrolling and tapping over Wi-Fi with no cable attached during use. A phone with **Developer Mode** already available can also pair without a cable. If Developer Mode is hidden, you may need to reveal it once by connecting a trusted USB cable and running `iphone-mirroring-control reveal-dev-mode`. Check your version under **Settings → General → About**.

1. On the iPhone, turn on **Settings → Privacy & Security → Developer Mode** and confirm after the restart.
2. On the Linux computer, run `iphone-mirroring-control wireless-pair`. On the iPhone, open **Settings → Developer → Paired Macs**, select the Linux computer, and enter the six-digit code from the terminal. Keep both devices on the same trusted Wi-Fi.
3. Open **iPhone Mirroring Linux** from your application menu and click **Connect over Wi-Fi**. The app reuses the existing pairing and mounts the Developer Disk Image as needed. The first mount may need internet access.
4. Click to tap, drag to swipe, and use the mouse wheel to scroll. Click a text field before typing. **Ctrl+H** goes Home. Use the right-click menu or **Ctrl+U** to disconnect.

If you already created the pairing over USB, you can go straight to step 3. Stop an existing browser or VNC session before switching to the desktop app. The desktop viewer is silent on Linux. It limits the displayed stream to a 1440-pixel long edge to reduce local video traffic, and targets up to 60 display updates per second. Actual frame rate depends on the device, decoder, and network. The Linux software decoder keeps processing video while requesting recovery frames. Reconnect attempts apply to failed connections or viewer processes.

After a successful Wi-Fi connection, the app remembers to reconnect on the next launch. Temporary viewer failures retry up to three times. Use **Control**, **Ctrl+U**, or **Disconnect Wi-Fi** to deliberately disconnect and turn off automatic connection.

### Pair once over USB

If the phone does not discover the computer in Paired Macs, connect the unlocked iPhone once, tap **Trust**, and run:

```bash
~/.local/bin/iphone-mirroring-control wireless-pair --usb
```

This saves the developer pairing on the computer. Unplug the cable and launch the app to connect over Wi-Fi. Keep Developer Mode enabled. Changing Wi-Fi networks does not itself erase the pairing. At the new location, both devices must be on a network that lets them communicate directly. Guest, school, hotel, or isolated networks can block discovery. Resetting device trust or removing the pairing requires pairing again.

### Update an existing installation

Close the app and any terminal viewer first. In a Git checkout, run:

```bash
git pull --ff-only
./install.sh --control
~/.local/bin/iphone-mirroring-linux --wireless
```

For a ZIP installation, extract a new copy from **Code → Download ZIP**, open a terminal in that extracted folder, and run the last two commands above. The installer updates the existing application and keeps the pairing and settings. A pairing already used successfully with the previous version can be reused.

To pick a device when there are several pairing records, launch the desktop app with `iphone-mirroring-linux --wireless --device-id YOUR_PAIRED_ID`. The command-line browser and VNC viewers are still available through `iphone-mirroring-control wireless` and `iphone-mirroring-control wireless --vnc`.

The Wi-Fi viewer uses [pymobiledevice3’s developer display and input services](https://doronz88.github.io/pymobiledevice3/master/guides/screen-mirroring/) and [PIN pairing](https://doronz88.github.io/pymobiledevice3/master/cli/remote/#pair-host).

## USB control and existing Wi-Fi WebDriverAgent

This is the alternate input path for the AirPlay desktop window. Screen and audio still travel through AirPlay. Control uses Appium’s signed WebDriverAgent (WDA) runner, the trusted USB connection, and Developer Mode on iOS 16 and newer.

1. Install `usbmuxd`, plug in one iPhone, unlock it, and tap **Trust** on the phone. If the daemon does not start on plug-in, check `systemctl status usbmuxd` and run `sudo systemctl start usbmuxd` if needed.
2. Run `iphone-mirroring-control reveal-dev-mode`, then enable and confirm Developer Mode on the phone.
3. Sign and install a real-device [WebDriverAgentRunner](https://github.com/appium/WebDriverAgent) matching your iOS version. Appium documents [signing and provisioning in Xcode](https://appium.github.io/appium-xcuitest-driver/latest/preparation/real-device-config/). To get the unsigned upstream runner as an IPA, use `iphone-mirroring-control get-wda`. If you already have a signed IPA, you can install it using the packaged `pymobiledevice3 apps install` command in the app’s virtual environment. Linux tools such as [iloader](https://iloader.app/) can sign and install IPAs, but WDA’s XCTest runner also needs compatible provisioning. That signing combination has not been verified here.
4. If prompted, trust the developer profile under **Settings → General → VPN & Device Management**, and enable UI Automation under **Settings → Developer** if your iOS version offers that setting.
5. Run `iphone-mirroring-control doctor`, then toggle control with **Ctrl+U** in the desktop window. The app mounts the appropriate Developer Disk Image, starts WDA, and forwards its port to loopback. iOS 17.4+ normally uses a userspace tunnel. For iOS 17.0–17.3, see the [pymobiledevice3 tunnel instructions](https://doronz88.github.io/pymobiledevice3/master/guides/ios17-tunnels/) for the separate privileged `tunneld` service.

For a WDA instance you already started by a trusted means, explicitly target its endpoint. This mode works without a cable during use if the WDA runner stays active after you unplug it. WDA endpoints can accept unauthenticated input, so keep them on a private network.

```bash
iphone-mirroring-linux --wda-url http://IPHONE_IPV4_ADDRESS:8100 --control
```

A USB-started XCTest runner usually ends when the underlying test connection closes. On older iOS versions, this project does not start a cable-free control session unless an active WDA endpoint is supplied. A free signing profile may expire and need renewal.

| Helper | Purpose |
| --- | --- |
| `doctor` | Check USB detection, Developer Mode, installed runner, and WDA status |
| `reveal-dev-mode` | Reveal the Developer Mode switch on a trusted USB device |
| `get-wda [--out PATH]` | Download the official runner and package it as an unsigned IPA |
| `start [--wda-url URL]` | Start a USB WDA session or attach to an explicit endpoint until Ctrl+C |
| `tap X Y`, `home`, `type TEXT` | Send a test command to an existing WDA endpoint with `--url URL` |
| `wireless-pair [--name NAME]` | Pair a compatible iPhone over Wi-Fi |
| `wireless [--device-id ID] [--vnc or --audio]` | Start the local, password-protected, interactive viewer |

## Desktop controls

Drag any outside edge or corner to resize the window. The app uses the desktop's
native resize interaction when available, with a manual fallback. The image fits
the available space without stretching, and the toolbar stays accessible.

Right-click → **Wi-Fi video quality** offers **Fast (720 px)**, **Smooth (1080 px,
default)**, and **Sharp (1440 px)**, measured along the image's long edge. Reconnect
Wi-Fi after changing quality. Smooth reduces local raw-video bandwidth by about
44% compared with Sharp. It targets fluid playback on laptop CPUs; 60 FPS is not
guaranteed. Video is presented on the next event-loop turn, and Wi-Fi wheel input
uses a 12 ms batch instead of the USB backend's 120 ms batch.

### Can the iPhone stay asleep or locked?

The current Linux developer display backend does not provide app control while
the iPhone is asleep or locked. Its capture pipeline stops when iOS sleeps the
display, as documented in the [upstream keep-awake implementation](https://github.com/doronz88/pymobiledevice3/blob/v11.61.0/pymobiledevice3/remote/core_device/screen_stream.py#L3076).
Keep the iPhone awake and unlocked during a session. The app does not
bypass the lock screen or provide Apple's separate locked-device Continuity
mirroring mode. Reducing brightness is possible on the phone, but it does not
turn the display off or lock the device.

| Input | Result |
| --- | --- |
| Drag an outside edge or corner | Resize the window while preserving the phone image's proportions |
| Drag the window or top bar | Move the window. With USB/WDA control active, use the top bar or Alt+drag |
| Double-click in mirroring-only mode | Switch between large and normal sizes |
| Ctrl+wheel, Ctrl+Plus, Ctrl+Minus | Resize the mirror |
| Right-click | Open the controls menu, including size presets, pin, receiver name, and log |
| Ctrl+M | Mute or unmute AirPlay sound |
| Ctrl+S | Save the current full-resolution frame as a PNG |
| Ctrl+U | Disconnect the Wi-Fi viewer or toggle USB input control |
| Ctrl+H | Go Home when input control is ready |
| Ctrl+J | Open App Switcher with USB control |
| Ctrl+Up, Ctrl+Down | Change the iPhone volume when control is ready |
| Ctrl+Q | Quit |

With WDA enabled, use click for tap, double-click for double tap, hold for long press, drag for swipe, and the wheel to scroll. Type with the window focused. WDA swipes are sent after release. For live dragging, use the iOS 27+ wireless viewer.

On Fedora GNOME, the desktop app uses XWayland when available for window position and pinning. For native Wayland, launch with `QT_QPA_PLATFORM=wayland iphone-mirroring-linux`. Native Wayland uses compositor-managed positioning. Pinning and window activation depend on the compositor.

## Settings and logs

If Wi-Fi input works but the desktop picture freezes, close the app and run
`~/.local/bin/iphone-mirroring-linux --wireless --diagnose`. Keep moving the
iPhone screen for 20 seconds, then close the app. This mode logs installed versions,
incoming packet counts, decoded frames, decoder queue depth, and desktop received
and presented frames every five seconds, including while video is stalled. It
does not save the phone's video or input. Extract the counters with:

```bash
grep 'WIRELESS_DIAG' "${XDG_STATE_HOME:-$HOME/.local/state}/iphone-mirroring-linux/iphone_mirror.log" | tail -35
```

These counters distinguish device or transport stalls from decoder and desktop
display stalls. A synthetic HEVC test cannot verify a physical iPhone's stream.

The desktop starts its installed video backend in isolated Python mode. Launching
the app from an older source checkout cannot silently load that checkout's backend.
Diagnostic mode also logs the backend's own version and module path at startup.

Wireless video uses separate HEVC decode and preview workers. The decoder keeps
predictive reference frames intact while a single preview slot replaces obsolete
decoded images when resizing or display is slower than the phone. Scaling goes
directly from YUV to preview-sized BGRA; the asyncio display notification is also
limited to the newest image. Compressed video is capped at 12 queued access units.
If decoding itself overloads, recovery resumes at a keyframe instead of building
an unlimited delayed queue. Diagnostics include queue age, skipped previews, and
compressed drops. Actual FPS still depends on the CPU, device, and network.

- Settings are in `${XDG_CONFIG_HOME:-~/.config}/iphone-mirroring-linux/settings.json`. The file includes `name`, `height`, `width`, `fps`, `audio`, and `always_on_top`. Quit before editing. The default request is 1080p at 60 fps. The actual stream depends on the phone and network.
- Logs are in `${XDG_STATE_HOME:-~/.local/state}/iphone-mirroring-linux/`. The **Open Log** menu item opens the app log.
- Screenshots are saved in **Pictures → iPhone Mirroring Linux** using the desktop’s configured Pictures folder.
- Receiver identity and device pairing records are local. Keep them private.

## How it works

- The desktop app announces an AirPlay receiver over mDNS with Zeroconf. RTSP negotiates a session with the iPhone after you choose it in Control Center.
- The inherited AirPlay compatibility layer handles the sender's legacy pairing and stream key exchange. PyAV and FFmpeg decode H.264 video and AAC or ALAC audio. Qt presents video frames and PortAudio plays sound through the Linux audio stack.
- The optional USB backend starts an authorized XCTest runner with pymobiledevice3. The desktop translates mouse and keyboard actions into WDA commands sent over a local USB forward or a manually specified endpoint.
- The optional iOS 27+ viewer uses verified developer pairing, a Wi-Fi tunnel, the iPhone’s HEVC display stream, and input through its developer HID services. The app embeds the stream in its Qt window using a password-protected loopback connection.
- iPhone video and input stay between the device and your computer. Protected apps may show a black screen during mirroring.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[control]'
python tests/test_fairplay.py
python tests/test_wda.py
python tests/test_desktop_wireless.py
python tests/test_wireless_video.py
python tools/smoke_test.py
```

The smoke test uses Qt offscreen and a simulated sender. Manual device checks are needed for discovery from a real iPhone, audio playback, USB signing, direct Wi-Fi pairing, and physical input latency.

## Credits and license

Modified for Linux starting November 19, 2026. Based on [0xCodez’s iPhone Mirror for Windows](https://github.com/0xCodez/iphone-mirror-windows/tree/453a8db9fa5f4e5c8e5c43c62f6433a683501bd4). AirPlay work and test vectors draw on [UxPlay](https://github.com/FDH2/UxPlay), [RPiPlay](https://github.com/FD-/RPiPlay), [shairplay](https://github.com/juhovh/shairplay), [openairplay/airplay2-receiver](https://github.com/openairplay/airplay2-receiver), and [omarroth/doubletake](https://github.com/omarroth/doubletake). The optional controls use [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) and [WebDriverAgent](https://github.com/appium/WebDriverAgent).

[GPL-3.0](LICENSE). The inherited playfair module includes its [license](licenses/playfair-LICENSE.md). This is an independent project.
