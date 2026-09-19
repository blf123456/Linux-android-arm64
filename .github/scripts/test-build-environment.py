"""Run the real Bazel/Legacy dispatch against small fake build tools, never a phone."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")


def shell_path(path):
    value = path.resolve().as_posix()
    return "/" + value[0].lower() + value[2:] if os.name == "nt" else value


class BuildEnvironmentTest(unittest.TestCase):
    def test_all_version_arguments_match_the_author_script(self):
        harness = r'''
source "$1"
KERNELS_ROOT=/author-kernels
DRIVER_SRC=/unused-driver-directory
clean_driver_build() { :; }
package_driver() { :; }
ls() { :; }
log_info() { :; }
log_warn() { :; }
log_error() { :; }
log_title() { :; }
build_kernel() { printf 'CALL'; printf ' <%s>' "$@"; printf '\n'; }
build_legacy_kernel() { printf 'LEGACY\n'; }
main < <(printf 'n\n')
'''
        with tempfile.TemporaryDirectory() as directory:
            # Do not execute the upstream script's entry point on the host.
            upstream = ROOT / ".github/reference/build_all.upstream.sh"
            source = upstream.read_text(encoding="utf-8").rsplit('main "$@"', 1)[0]
            fixture = Path(directory) / "upstream-functions.sh"
            fixture.write_text(source, encoding="utf-8", newline="\n")
            calls = []
            for path in (fixture, ROOT / "build_all.sh"):
                result = subprocess.run([BASH, "-c", harness, "test", shell_path(path)],
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls.append([line for line in result.stdout.splitlines()
                              if line.startswith(("CALL", "LEGACY"))])
            self.assertEqual(len(calls[0]), 7)
            self.assertEqual(calls[0], calls[1])

    def run_build(self, version, *, fail=False, partial_cache=False, compiler=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kernel = root / "kernels" / version
            driver = root / "driver"
            tools = root / "bin"
            for path in (kernel / "common", driver, tools):
                path.mkdir(parents=True)
            legacy = version == "5.10-Android12"
            clang = kernel / ("prebuilts-master/clang/host/linux-x86/clang-r416183b" if legacy
                              else "prebuilts/clang/host/linux-x86/clang-r510928") / "bin/clang"
            clang.parent.mkdir(parents=True)
            if compiler:
                clang.write_text("#!/bin/bash\necho test-clang\n")
                clang.chmod(0o755)
            output = kernel / (f"out/{version}/common/common" if legacy else "prepared")
            if partial_cache:
                output.mkdir(parents=True)
                (output / "Module.symvers").write_text("existing kernel symbols\n")
                # A directory alone used to be accepted as a prepared kernel.
                (kernel / "bazel-bin/common/kernel_aarch64").mkdir(parents=True)
            prepare = kernel / ("build/build.sh" if legacy else "tools/bazel")
            prepare.parent.mkdir(parents=True, exist_ok=True)
            prepare.write_text("""#!/bin/bash
set -eu
printf '%s\\n' "$*" >> "$TEST_ROOT/prepare.log"
mkdir -p "$TEST_OUT"
printf 'CONFIG_MODVERSIONS=y\\nCONFIG_LTO_CLANG_THIN=y\\n' > "$TEST_OUT/.config"
printf 'original kernel symbols\\n' > "$TEST_OUT/Module.symvers"
if [ "$TEST_LEGACY" = 0 ]; then
    mkdir -p bazel-bin/common/kernel_aarch64_modules_prepare
    rmdir bazel-bin/common/kernel_aarch64 2>/dev/null || true
    ln -s "$TEST_OUT" bazel-bin/common/kernel_aarch64
    tar -C "$TEST_OUT" -czf bazel-bin/common/kernel_aarch64_modules_prepare/modules_prepare_outdir.tar.gz .config
fi
""", newline="\n")
            prepare.chmod(0o755)
            make = tools / "make"
            make.write_text("""#!/bin/bash
set -eu
printf '%s\\n' "$*" >> "$TEST_ROOT/make.log"
case " $* " in *' modules_prepare '*) exit 0 ;; esac
for arg; do case "$arg" in O=*) output=${arg#O=} ;; esac; done
test ! -e "$output/Module.symvers"
if [ "$TEST_FAIL" = 1 ]; then exit 42; fi
printf 'fresh module\\n' > "$DRIVER_SRC/lsdriver.ko"
""", newline="\n")
            make.chmod(0o755)
            env = {**os.environ, "KERNELS_ROOT": shell_path(root / "kernels"),
                   "DRIVER_SRC": shell_path(driver), "STRIP_CHOICE": "n",
                   "TEST_ROOT": shell_path(root), "TEST_OUT": shell_path(output),
                   "TEST_LEGACY": str(int(legacy)), "TEST_FAIL": str(int(fail))}
            # Set PATH inside Bash to avoid Windows/MSYS PATH translation issues.
            command = 'export PATH="$TEST_ROOT/bin:$PATH"; source "$1"; build_selected_kernel "$2"'
            result = subprocess.run([BASH, "-c", command, "test", shell_path(ROOT / "build_all.sh"), version],
                                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            return result, {name: (root / name).read_text() if (root / name).exists() else ""
                            for name in ("prepare.log", "make.log")}, \
                (output / "Module.symvers").read_text() if (output / "Module.symvers").exists() else "", \
                (driver / (version + ".ko")).exists()

    def test_bazel_prepares_both_targets_and_restores_symvers(self):
        result, logs, symvers, module = self.run_build("6.6-Android15")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("//common:kernel_aarch64 //common:kernel_aarch64_modules_prepare", logs["prepare.log"])
        self.assertIn("KBUILD_MODPOST_WARN=1 CONFIG_EXTENDED_MODVERSIONS=n", logs["make.log"])
        self.assertNotIn("gki_defconfig", logs["make.log"])
        self.assertEqual(symvers, "original kernel symbols\n")
        self.assertTrue(module)

    def test_partial_bazel_cache_is_rebuilt(self):
        result, logs, _, module = self.run_build("6.6-Android15", partial_cache=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("//common:kernel_aarch64_modules_prepare", logs["prepare.log"])
        self.assertTrue(module)

    def test_legacy_uses_build_sh_before_modules_prepare(self):
        result, logs, symvers, module = self.run_build("5.10-Android12")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(logs["prepare.log"])
        self.assertIn("modules_prepare", logs["make.log"])
        self.assertIn("HOSTCC=clang", logs["make.log"])
        self.assertEqual(symvers, "original kernel symbols\n")
        self.assertTrue(module)

    def test_failed_make_restores_symbols_and_does_not_publish_module(self):
        for version in ("6.6-Android15", "5.10-Android12"):
            with self.subTest(version=version):
                result, _, symvers, module = self.run_build(version, fail=True)
                self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
                self.assertEqual(symvers, "original kernel symbols\n")
                self.assertFalse(module)

    def test_missing_author_compiler_fails_before_build(self):
        result, logs, _, module = self.run_build("6.6-Android15", compiler=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(logs["prepare.log"], "")
        self.assertFalse(module)


if __name__ == "__main__":
    unittest.main()
