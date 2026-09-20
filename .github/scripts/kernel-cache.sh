#!/usr/bin/env bash
# Cache only module build inputs, not Bazel's action cache/server/sandboxes.
set -euo pipefail
operation="$1"
kernel_dir="$(realpath "$KERNELS_ROOT/$KERNEL_VERSION")"
cache_dir="$kernel_dir/module-environment-cache"
cd "$kernel_dir"

case "$operation" in
    restore)
        [[ -s "$cache_dir/environment.tar.zst" ]] || exit 0
        (cd "$cache_dir" && sha256sum -c SHA256SUMS)
        cmp "$ARTIFACT_DIR/manifest.xml" "$cache_dir/manifest.xml"
        tar --zstd -xf "$cache_dir/environment.tar.zst"
        echo 'Restored the exact kernel outputs; the driver will be rebuilt.'
        ;;
    save)
        mkdir -p "$cache_dir"
        if [[ "$KERNEL_VERSION" == 5.10-Android12 ]]; then
            # Legacy also creates the hermetic host sysroot/path during build.sh.
            paths=("out/$KERNEL_VERSION/common/common" build/build-tools)
            test -s "${paths[0]}/.config"
            test -s "${paths[0]}/Module.symvers"
        else
            # Keep the exact output path (including Bazel's workspace identity)
            # because Kbuild's Makefile/.cmd files can contain absolute paths.
            output=$(realpath bazel-bin/common/kernel_aarch64)
            prepare=$(realpath bazel-bin/common/kernel_aarch64_modules_prepare)
            [[ "$output" == "$kernel_dir/"* && "$prepare" == "$kernel_dir/"* ]]
            paths=("${output#"$kernel_dir/"}" "${prepare#"$kernel_dir/"}")
            execroot="${output%%/bazel-out/*}"
            [[ "$execroot" != "$output" ]]
            printf '%s\n' "${execroot#"$kernel_dir/"}" > "$cache_dir/execroot.txt"
            links=(bazel-bin)
            exclusions=()
            # Android 13 can leave source pointing to a removed sandbox. Kbuild
            # uses the explicit common/ source path; preserve this link as-is.
            if [[ -L "$output/source" ]]; then
                exclusions+=("--exclude=${paths[0]}/source")
                links+=("${paths[0]}/source")
            fi
            # Dereference Bazel output symlinks in the archive so it is complete
            # without retaining all intermediate kernel objects and sandboxes.
        fi
        if [[ "$KERNEL_VERSION" == 5.10-Android12 ]]; then
            # Preserve host tool symlinks: their ELF RUNPATH is relative to the
            # executable in prebuilts, not the build/build-tools/path shortcut.
            tar --zstd -cf "$cache_dir/environment.tar.zst" "${paths[@]}"
        else
            tar --dereference "${exclusions[@]}" --zstd -cf "$cache_dir/environment.tar.zst" "${paths[@]}"
        fi
        if [[ "$KERNEL_VERSION" != 5.10-Android12 ]]; then
            # Keep bazel-bin as a link, rather than archiving every Bazel target.
            tar -cf "$cache_dir/links.tar" "${links[@]}"
        fi
        cp "$ARTIFACT_DIR/manifest.xml" "$cache_dir/manifest.xml"
        (cd "$cache_dir" && sha256sum environment.tar.zst manifest.xml > SHA256SUMS
            if [[ -f links.tar ]]; then sha256sum links.tar execroot.txt >> SHA256SUMS; fi)
        du -h "$cache_dir/environment.tar.zst" | tee "$ARTIFACT_DIR/kernel-cache-size.txt"
        ;;
    *) echo "Usage: $0 restore|save" >&2; exit 2 ;;
esac
if [[ "$operation" == restore && -f "$cache_dir/links.tar" ]]; then
    tar -xf "$cache_dir/links.tar"
    # New musl host tools search upward from the generated executable for
    # execroot/prebuilts. Sources/toolchains were synced at the same revisions.
    execroot=$(cat "$cache_dir/execroot.txt")
    ln -s "$kernel_dir/prebuilts" "$kernel_dir/$execroot/prebuilts"
fi
