"""install.sh on other distributions, played: a stand-in /etc/os-release, and stand-ins for each
family's package tool (apt's dpkg-query and apt-get, rpm and dnf or zypper, pacman), sudo and
systemctl, which write down what they're asked; the packages "installed" are the test's list.
Never the computer's own package manager, and never root."""

import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL = os.path.join(os.path.dirname(HERE), "install.sh")

# Stand-ins: each writes its name and arguments to $LOG; the query tools answer from $INSTALLED
# (a package per line).
STUBS = {
    "dpkg-query": 'echo "dpkg-query $*" >> "$LOG"; for p; do :; done\n'
                  'grep -qx "$p" "$INSTALLED" && printf "ii " && exit 0; exit 1',
    "rpm": 'case "$1" in -E) echo 43; exit 0;; esac\nfor p; do :; done\ngrep -qx "$p" "$INSTALLED"',
    "pacman": 'echo "pacman $*" >> "$LOG"\n'
              'if [ "$1" = -T ]; then shift; for p; do grep -qx "$p" "$INSTALLED" || echo "$p"; done; exit 0; fi',
    "apt-get": 'echo "apt-get $*" >> "$LOG"',
    "apt-cache": 'for p; do :; done\n! grep -qx "unknown:$p" "$INSTALLED"',
    "dnf": 'echo "dnf $*" >> "$LOG"',
    "zypper": 'echo "zypper $*" >> "$LOG"\n[ "$2" != repos ]',
    "sudo": 'echo "sudo $*" >> "$LOG"; "$@"',
    # Downloads: the test's file ($ASSET) when it says they work ($CURL_OK, $GH_OK).
    "curl": 'echo "curl $*" >> "$LOG"\n[ -n "$CURL_OK" ] || exit 22\n'
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && cp "$ASSET" "$2"; shift; done',
    "wget": 'echo "wget $*" >> "$LOG"; exit 8',
    "gh": 'echo "gh $*" >> "$LOG"\n[ -n "$GH_OK" ] || exit 1\n'
          'while [ $# -gt 0 ]; do [ "$1" = --output ] && cp "$ASSET" "$2"; shift; done',
    "systemctl": 'echo "systemctl $*" >> "$LOG"\n'
                 'case "$1" in list-unit-files) echo "$2 enabled";; is-active) grep -qx "service:$3" "$INSTALLED";; esac',
}

DISTROS = {
    "apt": 'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 26.04"\n',
    "dnf": 'ID=fedora\nPRETTY_NAME="Fedora Linux 42"\n',
    "pacman": 'ID=endeavouros\nID_LIKE=arch\nPRETTY_NAME="EndeavourOS"\n',
    "zypper": 'ID=opensuse-tumbleweed\nID_LIKE="opensuse suse"\nPRETTY_NAME="openSUSE Tumbleweed"\n',
    "unknown": 'ID=gentoo\nPRETTY_NAME="Gentoo Linux"\n',
}


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stubs = os.path.join(self.tmp.name, "stubs")
        os.makedirs(self.stubs)
        for name, body in STUBS.items():
            path = os.path.join(self.stubs, name)
            with open(path, "w") as file:
                file.write("#!/bin/sh\n" + body + "\n")
            os.chmod(path, 0o755)
        # The graphics: none, unless a test says (an Intel card, say).
        self.drm = os.path.join(self.tmp.name, "drm")
        os.makedirs(self.drm)
        self.log = os.path.join(self.tmp.name, "log")
        self.installed = os.path.join(self.tmp.name, "installed")
        open(self.log, "w").close()

    def run_install(self, family: str, installed: list[str], *args: str, script: str = INSTALL) -> str:
        with open(self.installed, "w") as file:
            file.write("".join(f"{p}\n" for p in installed))
        release = os.path.join(self.tmp.name, "os-release")
        with open(release, "w") as file:
            file.write(DISTROS[family])
        env = dict(os.environ, PATH=f"{self.stubs}:/usr/bin:/bin", LOG=self.log, INSTALLED=self.installed,
                   HANDSET_OS_RELEASE=release, HANDSET_DRM=self.drm, HOME=os.path.join(self.tmp.name, "home"),
                   XDG_DATA_HOME=os.path.join(self.tmp.name, "home", "data"))
        done = subprocess.run(["sh", script, *args], env=env, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def asked(self) -> list[str]:
        with open(self.log) as file:
            return [line.strip() for line in file if not line.startswith(("dpkg-query", "pacman -T", "systemctl"))]

    def packages(self, family: str) -> list[str]:
        listed = subprocess.run(["sh", "-c", f'. "$0" >/dev/null 2>&1; packages {family}', INSTALL],
                                capture_output=True, text=True, env=dict(os.environ, HANDSET_LIB="1"))
        return [p.split("|")[0] for p in listed.stdout.split()]  # of two names, the first

    def test_only_whats_missing_once_through_sudo(self):
        for family, install in (("apt", "apt-get install -y"), ("dnf", "dnf install -y"),
                                ("pacman", "pacman -S --needed --noconfirm"),
                                ("zypper", "zypper --non-interactive install")):
            with self.subTest(family):
                open(self.log, "w").close()
                everything = self.packages(family)
                self.assertIn("git", everything)
                there = [p for p in everything if p not in ("git", "usbmuxd")] + ["service:bluetooth",
                                                                                  "service:avahi-daemon"]
                said = self.run_install(family, there, "--check", "--deps-only")
                self.assertIn("usbmuxd git", said)  # in the list's order
                self.assertEqual(self.asked(), [])  # --check: nothing done
                self.run_install(family, there, "--yes", "--deps-only")
                self.assertIn(f"sudo {install} usbmuxd git", self.asked())
                if family == "apt":
                    self.assertIn("sudo apt-get update -q", self.asked())

    def test_the_graphics_cards_video_driver(self):
        os.makedirs(os.path.join(self.drm, "card1", "device"))
        with open(os.path.join(self.drm, "card1", "device", "vendor"), "w") as file:
            file.write("0x8086\n")  # Intel
        everything = self.packages("apt") + ["service:bluetooth", "service:avahi-daemon"]
        self.run_install("apt", everything, "--yes", "--deps-only")
        self.assertIn("sudo apt-get install -y intel-media-va-driver", self.asked())

    def decoders(self, family: str, decoders: str, installed: list[str] = ()) -> str:
        """install.sh's video check alone (its functions read, as the tests read them)."""
        with open(self.installed, "w") as file:
            file.write("".join(f"{p}\n" for p in installed))
        release = os.path.join(self.tmp.name, "os-release")
        with open(release, "w") as file:
            file.write(DISTROS[family])
        env = dict(os.environ, PATH=f"{self.stubs}:/usr/bin:/bin", LOG=self.log, INSTALLED=self.installed,
                   HANDSET_OS_RELEASE=release, HANDSET_DRM=self.drm, HANDSET_LIB="1",
                   HANDSET_DECODERS=decoders.replace(";", "\n"))
        done = subprocess.run(["sh", "-c", '. "$0"; YES=1; decoders', INSTALL], env=env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)
        return done.stdout

    def test_video_decoders_there(self):
        said = self.decoders("apt", "H.264 vah264dec;HEVC avdec_h265")
        self.assertIn("Android phones' with vah264dec, iPhones' with avdec_h265", said)
        self.assertEqual(self.asked(), [])

    def test_fedoras_codecs_from_rpm_fusion(self):
        os.makedirs(os.path.join(self.drm, "card1", "device"))
        with open(os.path.join(self.drm, "card1", "device", "vendor"), "w") as file:
            file.write("0x8086\n")
        # Its openh264 a stand-in: no decoder for either phone.
        said = self.decoders("dnf", "H.264 openh264dec;HEVC -", installed=["noopenh264"])
        self.assertIn("nothing here decodes H.264", said)
        self.assertIn("nothing here decodes HEVC", said)
        asked = self.asked()
        self.assertIn("sudo dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-43.noarch.rpm "
                      "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-43.noarch.rpm", asked)
        self.assertIn("sudo dnf swap -y ffmpeg-free ffmpeg --allowerasing", asked)
        self.assertIn("sudo dnf install -y intel-media-driver", asked)

    def test_opensuses_codecs_from_packman(self):
        said = self.decoders("zypper", "H.264 openh264dec;HEVC -", installed=["noopenh264"])
        self.assertIn("Packman", said)
        asked = self.asked()
        self.assertIn("sudo zypper --non-interactive addrepo -cfp 90 https://ftp.gwdg.de/pub/linux/misc/packman/suse/"
                      "openSUSE_Tumbleweed/Essentials/ packman-essentials", asked)
        self.assertIn("sudo zypper --non-interactive dist-upgrade --from packman-essentials --allow-vendor-change",
                      asked)

    def test_other_distributions_only_told(self):
        said = self.decoders("pacman", "H.264 avdec_h264;HEVC -")
        self.assertIn("Install GStreamer's libav plugin", said)
        self.assertEqual(self.asked(), [])

    def test_either_of_two_names(self):
        everything = [p for p in self.packages("apt") if p != "android-udev-rules"] + ["service:bluetooth",
                                                                                     "service:avahi-daemon"]
        # Neither of adb's USB rules' names: the one this release has.
        self.run_install("apt", everything + ["unknown:android-udev-rules"], "--yes", "--deps-only")
        self.assertIn("sudo apt-get install -y android-sdk-platform-tools-common", self.asked())
        open(self.log, "w").close()
        said = self.run_install("apt", everything + ["android-udev-rules"], "--yes", "--deps-only")
        self.assertIn("everything Handset needs is installed", said)  # either will do

    def helper(self, *, works: str = "curl", sha: str | None = None, least: str = "2.17", same_source: bool = True,
               check: bool = False) -> tuple[str, int]:
        """install.sh's ready-built helper step alone, with a pin file of the test's own."""
        asset = os.path.join(self.tmp.name, "asset")
        with open(asset, "wb") as file:
            file.write(b"\x7fELF the helper")
        lib = dict(os.environ, PATH=f"{self.stubs}:/usr/bin:/bin", LOG=self.log, HANDSET_LIB="1")
        source = subprocess.run(["sh", "-c", '. "$0"; helper_source_hash', INSTALL], env=lib, capture_output=True,
                                text=True).stdout.strip()
        machine = os.uname().machine
        pin = os.path.join(self.tmp.name, "prebuilt")
        with open(pin, "w") as file:
            file.write("# MACHINE SOURCE SHA256 LEAST URL\n"
                       f"{machine} {source if same_source else '0' * 64} {sha or self.sha256(asset)} {least} "
                       "https://github.com/Officialbabs/handset/releases/download/v0.1.0/handset-ios-x86_64-linux\n")
        home = os.path.join(self.tmp.name, "home")
        env = dict(lib, HOME=home, HANDSET_PREBUILT=pin, ASSET=asset, CHECK="1" if check else "",
                   CURL_OK="1" if works == "curl" else "", GH_OK="1" if works == "gh" else "")
        done = subprocess.run(["sh", "-c", '. "$0"; CHECK=$CHECK_WANTED; prebuilt', INSTALL],
                              env=dict(env, CHECK_WANTED=env["CHECK"]), capture_output=True, text=True, timeout=60)
        self.installed_helper = os.path.join(home, ".local", "lib", "handset", "handset-ios")
        return done.stdout, done.returncode

    @staticmethod
    def sha256(path: str) -> str:
        import hashlib

        with open(path, "rb") as file:
            return hashlib.sha256(file.read()).hexdigest()

    def test_the_ready_built_helper(self):
        said, ok = self.helper()
        self.assertEqual(ok, 0, said)
        self.assertIn("ready-built, its checksum as published", said)
        self.assertTrue(os.access(self.installed_helper, os.X_OK))
        said, ok = self.helper()  # again: it's here already
        self.assertIn("ready-built, here", said)

    def test_through_gh_while_the_repository_is_private(self):
        said, ok = self.helper(works="gh")
        self.assertEqual(ok, 0, said)
        self.assertIn("gh release download v0.1.0 --repo Officialbabs/handset --pattern handset-ios-x86_64-linux "
                      f"--output {self.installed_helper}.part --clobber", self.asked())

    def test_built_instead(self):
        for case, kwargs in (("not what was published", {"sha": "0" * 64}),
                             ("nothing came", {"works": "nothing"}),
                             ("another source", {"same_source": False}),
                             ("an older C library", {"least": "99.0"})):
            with self.subTest(case):
                open(self.log, "w").close()
                said, ok = self.helper(**kwargs)
                self.assertEqual(ok, 1, said)
                self.assertFalse(os.path.exists(self.installed_helper))
                self.assertFalse(os.path.exists(self.installed_helper + ".part"))
                if case in ("another source", "an older C library"):
                    self.assertEqual(self.asked(), [])  # not even fetched

    def test_check_fetches_nothing(self):
        said, ok = self.helper(check=True)
        self.assertIn("ready-built, to be fetched", said)
        self.assertEqual(self.asked(), [])

    def test_all_there_nothing_asked(self):
        everything = self.packages("dnf") + ["service:bluetooth", "service:avahi-daemon"]
        said = self.run_install("dnf", everything, "--yes", "--deps-only")
        self.assertIn("everything Handset needs is installed", said)
        self.assertEqual(self.asked(), [])

    def test_services_left_off_switched_on(self):
        said = self.run_install("pacman", self.packages("pacman"), "--yes", "--deps-only")
        self.assertIn("bluetooth avahi-daemon", said)
        self.assertIn("sudo systemctl enable --now bluetooth", self.asked())
        self.assertIn("sudo systemctl enable --now avahi-daemon", self.asked())

    def test_no_one_to_ask_nothing_installed(self):
        said = self.run_install("apt", [], "--deps-only")  # no terminal, no --yes
        self.assertIn("Left as they are", said)
        self.assertEqual(self.asked(), [])

    def test_a_distribution_it_doesnt_know(self):
        said = self.run_install("unknown", [], "--yes", "--deps-only")
        self.assertIn("Gentoo Linux isn't one this script knows", said)
        self.assertEqual(self.asked(), [])

    @unittest.skipUnless(shutil.which("git"), "no git")
    def test_fetching_itself_when_run_alone(self):
        # A Handset of the test's own to fetch: this install.sh, and what marks a Handset folder.
        repository = os.path.join(self.tmp.name, "repository")
        os.makedirs(os.path.join(repository, "handset"))
        os.makedirs(os.path.join(repository, "bin"))
        shutil.copy(INSTALL, repository)
        for path in ("handset/__main__.py", "bin/handset"):
            open(os.path.join(repository, path), "w").close()
        git = ["git", "-C", repository, "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(git[:3] + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "Handset"], check=True)
        alone = os.path.join(self.tmp.name, "alone")
        os.makedirs(alone)
        shutil.copy(INSTALL, alone)
        everything = self.packages("apt") + ["service:bluetooth", "service:avahi-daemon"]
        os.environ["HANDSET_REPOSITORY"] = repository
        try:
            said = self.run_install("apt", everything, "--yes", "--deps-only", script=os.path.join(alone, "install.sh"))
        finally:
            del os.environ["HANDSET_REPOSITORY"]
        source = os.path.join(self.tmp.name, "home", "data", "handset", "source")
        self.assertIn(f"Fetching Handset into {source}", said)
        self.assertTrue(os.path.isfile(os.path.join(source, "handset", "__main__.py")))
        self.assertIn("everything Handset needs is installed", said)  # then run from there

if __name__ == "__main__":
    unittest.main()
