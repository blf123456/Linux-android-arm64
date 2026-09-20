"""Exercise archive restoration after the original build outputs are removed."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("kernel-cache.sh")


@unittest.skipUnless(os.name == "posix" and shutil.which("zstd"), "Requires Linux tar and zstd")
class KernelCacheTest(unittest.TestCase):
    def test_roundtrip_and_reject_changed_manifest_or_archive(self):
        for version in ("6.6-Android15", "5.10-Android12"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                kernel = root / version
                artifacts = root / "artifacts"
                artifacts.mkdir()
                manifest = artifacts / "manifest.xml"
                manifest.write_text("<manifest>fixed project revisions</manifest>\n")
                legacy = version == "5.10-Android12"
                if legacy:
                    output = kernel / "out" / version / "common/common"
                    host = kernel / "build/build-tools/sysroot/usr/include"
                    host.mkdir(parents=True)
                    (host / "test.h").write_text("host header\n")
                else:
                    binary = kernel / "out/bazel/execroot/workspace/bazel-out/bin"
                    output = binary / "common/kernel_aarch64"
                    prepare = binary / "common/kernel_aarch64_modules_prepare"
                    prepare.mkdir(parents=True)
                    (prepare / "modules_prepare_outdir.tar.gz").write_bytes(b"prepare fixture")
                    (kernel / "bazel-bin").symlink_to(binary, target_is_directory=True)
                output.mkdir(parents=True)
                (output / ".config").write_text("CONFIG_CFI_CLANG=y\n")
                (output / "Module.symvers").write_text("original symbols\n")
                # A Bazel output can point to a different action's generated file.
                shared = kernel / "out/shared-generated-header"
                shared.write_bytes(b"unchanged generated contents\x00\xff")
                (output / "generated.h").symlink_to(shared)
                (output / "generated.h").chmod(0o444)
                env = {**os.environ, "KERNELS_ROOT": str(root), "KERNEL_VERSION": version,
                       "ARTIFACT_DIR": str(artifacts)}

                def run(operation, success=True):
                    result = subprocess.run(["bash", str(SCRIPT), operation], env=env,
                                            capture_output=True, text=True, timeout=30)
                    if success:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

                run("save")
                shutil.rmtree(kernel / "out")
                if legacy:
                    shutil.rmtree(kernel / "build/build-tools")
                run("restore")
                self.assertEqual((output / "generated.h").read_bytes(), b"unchanged generated contents\x00\xff")
                self.assertEqual((output / ".config").read_text(), "CONFIG_CFI_CLANG=y\n")
                if legacy:
                    self.assertEqual((host / "test.h").read_text(), "host header\n")
                else:
                    self.assertTrue((kernel / "bazel-bin/common/kernel_aarch64/.config").is_file())
                original_manifest = manifest.read_bytes()
                manifest.write_text("<manifest>different project revision</manifest>\n")
                run("restore", success=False)
                manifest.write_bytes(original_manifest)
                archive = kernel / "module-environment-cache/environment.tar.zst"
                with archive.open("ab") as handle:
                    handle.write(b"damaged cache")
                run("restore", success=False)


if __name__ == "__main__":
    unittest.main()
