# Security review and operating boundaries

## Version 0.3.1 follow-up

- AirPlay limits each sender address to four control connections. It also times
  out incomplete media setup and requests or responses that stall mid-transfer.
- AirPlay validates stream descriptors and media packet metadata. The desktop
  retains only the newest pending preview when the UI is busy.
- The WDA client validates session IDs and screen dimensions. The IPA repacker
  streams members into the output and rejects duplicate entries, special files
  and files nested beneath archive symlinks.
- Logs are checked for ownership and hard links before they can be truncated.
  Screenshots are saved as private files, and viewer passwords are redacted
  from desktop logs.

The dependency versions and operating boundaries below also apply to 0.3.1.

## Version 0.3.0 review

Reviewed November 22, 2026. This release received a direct source review,
network/file-handling regression tests, and a `pip-audit` dependency check.
**Codex Security was not connected in the review session, so this is not a
Codex Security scan or a security certification.** No physical iPhone was
available for this review.

| Finding | Change | Regression evidence |
| --- | --- | --- |
| RTSP state-changing handlers did not require a completed pair-verify exchange | Require verified pairing before FairPlay setup, stream setup, and session changes; failed/restarted verification clears authentication state | Unverified requests receive 403; malformed/restarted handshakes revoke verification; legitimate simulated pairing and streaming still work |
| Idle unauthenticated clients could retain unbounded control connections | Limit control connections to eight; use a ten-second unauthenticated request deadline, ten-second body deadline, 16 KiB headers and 1 MiB bodies | Slow-peer, connection-count, duplicate-length and header-injection cases |
| Compressed video queue could retain hundreds of large packets | Bound the AirPlay queue to 32 packets and 32 MiB; limit decoded H.264/HEVC pictures to 16,777,216 pixels, including later format changes through libavcodec | Byte/count saturation checks and an actual oversized encoded H.264 picture rejected by FFmpeg |
| Viewer session passwords were visible in process arguments and VNC used only 32 bits of random hex | Pass private bridge passwords through a child environment variable consumed at startup; generate eight alphanumeric VNC characters, about 48 bits | Child argument inspection plus real local RFB authentication/video/input tests |
| State/USB logs inherited permissive file permissions and followed symlinks | Open logs with mode 0600, repair existing log permissions on open, and refuse symlinks with O_NOFOLLOW | Existing mode correction and symlink write rejection |
| WDA responses and runner download/archive sizes were unbounded; a predictable temporary filename could follow a symlink | Bound JSON responses to 2 MiB, downloads to 128 MiB, archive expansion to 512 MiB, entries to 20,000, and each member to 64 MiB; use an exclusive temporary file and atomic replacement; reject archive links escaping the app | Oversize response/download/archive, planted .part link, escaping archive link, and failed-download preservation tests |
| Python helpers could import modules from the launch directory/PYTHONPATH | Run installed device helpers with Python isolated mode | Process command assertions and installed-backend test from an old checkout |
| Network-supplied audio volume could overflow gain calculation | Reject non-finite numbers and clamp volume to the supported -144 through 0 dB range | NaN, infinity and extreme finite volume tests |

The installer now refreshes dependencies with `--upgrade-strategy eager`.
Minimum versions for PyAV, cryptography, Qt and zeroconf were raised to the
versions included in this review. This is a tested baseline, not a claim that
every earlier version has a known vulnerability.

### Dependency audit

[dependency-audit.json](security/dependency-audit.json) is the unmodified
`pip-audit` JSON for the Python 3.15 test installation, with optional control
dependencies installed. It contains 96 distributions: **95 checked third-party
packages had no known advisories**, and this unpublished project itself was
skipped by the advisory database. Source findings above are separate from that
database check. Native FFmpeg/Qt bundled components, system libraries, the iPhone
OS and downloaded WDA binaries are not exhaustively covered by this result.

To repeat against your own installed environment, use a separate audit venv:

```bash
python3 -m venv /tmp/iphone-mirror-audit
/tmp/iphone-mirror-audit/bin/pip install pip-audit
/tmp/iphone-mirror-audit/bin/pip-audit \
  --path ~/.local/share/iphone-mirroring-linux/venv/lib/python3.15/site-packages
```

Replace `python3.15` and the data path with your installed Python/XDG location.
Advisories change over time; the committed report describes only this installation
at review time. Keep the OS and dependencies updated.

### Validation

`tests/test_security.py` exercises the security boundaries and older-iOS mode.
The full suite also covers WDA control, the desktop RFB client, synthetic HEVC
video with simultaneous input, resize/toolbar behavior, discovery lifecycle, and
FairPlay reference vectors. `tools/smoke_test.py` streams simulated encrypted
AirPlay video and audio twice through the actual desktop receiver, captures the
Qt window, and checks clean shutdown. These are simulated-device tests; they do
not establish compatibility, frame rate or latency on a particular physical phone.

## Network trust model

* **Legacy AirPlay is a trusted-LAN receiver.** Pair-verify proves possession of
  the sender-supplied signing key. There is no PIN prompt or trusted-sender
  allowlist in this legacy protocol implementation. Any reachable sender that
  completes this exchange may attempt a session. The check added in 0.3.0 closes
  the pre-verification handler path; it does not make arbitrary LAN senders trusted.
* Use `--bind PC_IPV4 --allow-phone PHONE_IPV4` and matching firewall rules to
  reduce exposure. The source-IP check gates the RTSP connection; media sockets
  already accept only the selected session's source IP. IP filtering is not
  cryptographic authentication and does not protect against a compromised LAN.
  Do not expose these ports to the internet or use an untrusted shared network.
* **Developer Wi-Fi** uses the iPhone's approved pairing. Its desktop bridge and
  optional web/VNC viewers bind to `127.0.0.1`. RFB authentication has a legacy
  eight-character limit and does not encrypt framebuffer traffic. Loopback
  confinement is essential; do not proxy or publish these services. Another
  process running as your own user, or root, can inspect the environment/memory.
* **WebDriverAgent** accepts input over HTTP. USB forwarding binds to loopback.
  A Wi-Fi `--wda-url` is an explicitly chosen trusted endpoint; the app does not
  discover or authenticate arbitrary WDA servers. A reachable WDA endpoint can
  let other network users control the phone. Use a private network, constrain
  reachability, and stop WDA after use.
* Pairing/settings and logs live in your XDG directories. Logs can contain device
  identifiers, network addresses and errors. Review/redact them before sharing.
  Screenshots intentionally save phone content to your Downloads directory.
* Runner downloads use HTTPS from Appium's GitHub release. They are not pinned to
  an independently verified digest or reproducible build. The IPA is unsigned;
  building/verifying and provisioning a compatible WDA runner remains your task.

## Reporting

For a sensitive issue, use GitHub's private vulnerability reporting feature if
enabled for this repository. Otherwise contact the repository owner through a
private channel before posting exploit details. Include the installed version,
affected mode, and a minimal reproduction with secrets/device identifiers removed.
